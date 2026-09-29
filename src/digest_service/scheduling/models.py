import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils import timezone

from digest_service.core.models import ValidatedModel


class Schedule(ValidatedModel):
    id = models.SlugField(primary_key=True, max_length=100)
    name_ru = models.CharField("Название RU", max_length=200)
    name_en = models.CharField("Название EN", max_length=200)
    timezone = models.CharField("Часовой пояс", max_length=80, default="Europe/Moscow")
    recurrence_kind = models.CharField(
        "Повторение",
        max_length=10,
        default="daily",
        choices=[("daily", "Ежедневно"), ("weekly", "Еженедельно")],
    )
    local_times = models.JSONField("Время отправки", default=list)
    iso_weekdays = models.JSONField("Дни недели", default=list, blank=True)
    preparation_lead_minutes = models.PositiveSmallIntegerField("Подготовка за, мин", default=10)
    late_delivery_minutes = models.PositiveSmallIntegerField(
        "Допустимое опоздание, мин", default=60
    )
    visible_to_new_subscribers = models.BooleanField("Показывать новым подписчикам", default=False)
    delivery_enabled = models.BooleanField("Рассылка разрешена", default=False)
    effective_from = models.DateTimeField("Действует с", null=True, blank=True)
    sort_order = models.PositiveIntegerField("Порядок", default=100)
    revision = models.PositiveIntegerField("Редакция", default=1, editable=False)

    class Meta:
        ordering = ["sort_order", "id"]
        verbose_name = "Расписание"
        verbose_name_plural = "Расписания"

    def clean(self):
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError, TypeError):
            raise ValidationError({"timezone": "Неизвестный часовой пояс IANA."}) from None
        if not isinstance(self.local_times, list) or not 1 <= len(self.local_times) <= 24:
            raise ValidationError({"local_times": "Нужно от 1 до 24 времён отправки."})
        if any(
            not isinstance(t, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", t)
            for t in self.local_times
        ):
            raise ValidationError({"local_times": "Время задаётся в формате ЧЧ:ММ."})
        if len(set(self.local_times)) != len(self.local_times):
            raise ValidationError({"local_times": "Времена отправки не должны повторяться."})
        if not isinstance(self.iso_weekdays, list) or any(
            type(d) is not int or d not in range(1, 8) for d in self.iso_weekdays
        ):
            raise ValidationError({"iso_weekdays": "Дни задаются числами от 1 (пн) до 7 (вс)."})
        if len(set(self.iso_weekdays)) != len(self.iso_weekdays):
            raise ValidationError({"iso_weekdays": "Дни недели не должны повторяться."})
        if self.recurrence_kind == "weekly" and not self.iso_weekdays:
            raise ValidationError({"iso_weekdays": "Выберите хотя бы один день недели."})
        if self.recurrence_kind == "daily" and self.iso_weekdays:
            raise ValidationError(
                {"iso_weekdays": "Для ежедневного режима отдельные дни не задаются."}
            )
        if self.effective_from and timezone.is_naive(self.effective_from):
            raise ValidationError({"effective_from": "Дата должна содержать часовой пояс."})
        self.local_times = sorted(self.local_times)
        self.iso_weekdays = sorted(self.iso_weekdays)

    def as_rule(self):
        fields = [
            "name_ru",
            "name_en",
            "timezone",
            "recurrence_kind",
            "local_times",
            "iso_weekdays",
            "preparation_lead_minutes",
            "late_delivery_minutes",
            "visible_to_new_subscribers",
            "delivery_enabled",
            "sort_order",
        ]
        return {
            **{key: getattr(self, key) for key in fields},
            "effective_from": self.effective_from.isoformat() if self.effective_from else None,
        }

    def save(self, *args, **kwargs):
        if kwargs.get("update_fields"):
            raise ValueError("Save the whole schedule to preserve its revision snapshot.")
        self.full_clean()
        with transaction.atomic():
            previous = type(self).objects.select_for_update().filter(pk=self.pk).first()
            if previous and previous.revision != self.revision:
                raise ValidationError("Расписание изменено другим оператором. Обновите страницу.")
            changed = previous is None or previous.as_rule() != self.as_rule()
            if previous and changed:
                self.revision = previous.revision + 1
            result = super().save(*args, **kwargs)
            if changed:
                ScheduleRevision.objects.create(
                    schedule=self, number=self.revision, rule=self.as_rule()
                )
            return result

    def __str__(self):
        return self.name_ru

    @property
    def weekday_names(self):
        names = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
        return ", ".join(names[day - 1] for day in self.iso_weekdays)

    def preview(self, count=5, after=None):
        """Preview only: no jobs or outgoing messages are created."""
        if not 1 <= count <= 20:
            raise ValueError("count must be between 1 and 20")
        self.full_clean()
        after = after or timezone.now()
        if timezone.is_naive(after):
            raise ValueError("after must be timezone-aware")
        anchor = max(after, self.effective_from) if self.effective_from else after
        zone = ZoneInfo(self.timezone)
        start = anchor.astimezone(zone).date() - timedelta(days=8)
        candidates = []
        for offset in range(170):
            day = start + timedelta(days=offset)
            if self.recurrence_kind == "weekly" and day.isoweekday() not in self.iso_weekdays:
                continue
            for value in self.local_times:
                naive = datetime.combine(day, datetime.strptime(value, "%H:%M").time())
                # Skip nonexistent local times; choose the earlier instant in an autumn fold.
                instants = set()
                for fold in (0, 1):
                    instant = naive.replace(tzinfo=zone, fold=fold).astimezone(UTC)
                    if instant.astimezone(zone).replace(tzinfo=None) == naive:
                        instants.add(instant)
                if instants:
                    candidates.append(min(instants))
        candidates.sort()
        rows = []
        for index, instant in enumerate(candidates):
            if instant <= after or (self.effective_from and instant < self.effective_from):
                continue
            lead = timedelta(minutes=self.preparation_lead_minutes)
            rows.append(
                {
                    "scheduled_at": instant,
                    "local_time": instant.astimezone(zone),
                    "window_start": candidates[index - 1] - lead if index else None,
                    "window_end": instant - lead,
                }
            )
            if len(rows) == count:
                return rows
        raise ValueError("Not enough occurrences within the preview horizon")


class ScheduleRevision(ValidatedModel):
    schedule = models.ForeignKey(Schedule, on_delete=models.PROTECT, related_name="revisions")
    number = models.PositiveIntegerField("Редакция")
    rule = models.JSONField("Правило")

    class Meta:
        verbose_name = "Редакция расписания"
        verbose_name_plural = "Редакции расписаний"
        constraints = [
            models.UniqueConstraint(fields=["schedule", "number"], name="unique_schedule_revision")
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError(
                "Редакция расписания неизменяема; измените действующее расписание."
            )
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.schedule_id} · {self.number}"


class ScheduleSlot(ValidatedModel):
    schedule_revision = models.ForeignKey(
        ScheduleRevision, on_delete=models.PROTECT, related_name="slots"
    )
    scheduled_at = models.DateTimeField("Время отправки")
    window_start = models.DateTimeField("Начало окна")
    window_end = models.DateTimeField("Конец окна")
    preparation_at = models.DateTimeField("Начать подготовку")
    delivery_deadline = models.DateTimeField("Крайний срок доставки")
    state = models.CharField(
        "Состояние",
        max_length=15,
        default="planned",
        choices=[
            ("planned", "Запланирован"),
            ("preparing", "Подготавливается"),
            ("prepared", "Подготовлен"),
            ("closed", "Закрыт"),
            ("failed", "Ошибка"),
        ],
    )
    preparation_summary = models.JSONField("Результат подготовки", default=dict, blank=True)

    class Meta:
        ordering = ["scheduled_at"]
        verbose_name = "Слот расписания"
        verbose_name_plural = "Слоты расписаний"
        constraints = [
            models.UniqueConstraint(
                fields=["schedule_revision", "scheduled_at"], name="unique_schedule_slot"
            )
        ]

    def clean(self):
        values = [
            self.window_start,
            self.window_end,
            self.preparation_at,
            self.scheduled_at,
            self.delivery_deadline,
        ]
        if any(value and timezone.is_naive(value) for value in values):
            raise ValidationError("Все даты слота должны содержать часовой пояс.")
        if all(values) and not (
            self.window_start <= self.window_end
            and self.preparation_at <= self.scheduled_at <= self.delivery_deadline
        ):
            raise ValidationError("Границы слота заданы в неверном порядке.")

    def __str__(self):
        return f"{self.schedule_revision} · {self.scheduled_at}"
