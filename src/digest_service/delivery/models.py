import hashlib
import json
import uuid

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from digest_service.core.models import ValidatedModel
from digest_service.scheduling.models import ScheduleRevision
from digest_service.subscriptions.models import PreferenceRevision, SubscriberGeneration


class Delivery(ValidatedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    subscriber_generation = models.ForeignKey(
        SubscriberGeneration, on_delete=models.PROTECT, related_name="deliveries"
    )
    preference_revision = models.ForeignKey(
        PreferenceRevision, on_delete=models.PROTECT, related_name="deliveries"
    )
    schedule_revision = models.ForeignKey(
        ScheduleRevision, on_delete=models.PROTECT, related_name="deliveries"
    )
    scheduled_at = models.DateTimeField()
    kind = models.CharField(
        max_length=15,
        choices=[
            ("automatic", "Автоматическая"),
            ("manual", "По запросу"),
            ("correction", "Исправление"),
        ],
    )
    language = models.CharField(max_length=2, choices=[("ru", "Русский"), ("en", "English")])
    status = models.CharField(
        max_length=15,
        default="pending",
        choices=[
            ("pending", "Ожидает"),
            ("sending", "Отправляется"),
            ("sent", "Отправлена"),
            ("failed", "Ошибка"),
            ("cancelled", "Отменена"),
            ("unknown", "Результат неизвестен"),
        ],
    )
    idempotency_key = models.CharField(max_length=64, unique=True)

    class Meta:
        ordering = ["-scheduled_at", "-created_at"]
        verbose_name = "Отправка"
        verbose_name_plural = "Отправки"
        constraints = [
            models.UniqueConstraint(
                fields=["subscriber_generation", "schedule_revision", "scheduled_at", "kind"],
                condition=models.Q(kind="automatic"),
                name="unique_automatic_delivery_slot",
            )
        ]

    def clean(self):
        if self.scheduled_at and timezone.is_naive(self.scheduled_at):
            raise ValidationError({"scheduled_at": "Дата должна содержать часовой пояс."})
        if self.preference_revision_id:
            if self.preference_revision.subscriber_id != self.subscriber_generation_id:
                raise ValidationError("Настройки относятся к другому поколению подписчика.")
            if self.preference_revision.schedule_revision_id != self.schedule_revision_id:
                raise ValidationError("Расписание отправки не совпадает с настройками.")
            if self.preference_revision.digest_language != self.language:
                raise ValidationError("Язык отправки не совпадает с настройками.")

    def __str__(self):
        return f"{self.subscriber_generation} · {self.scheduled_at} · {self.kind}"


class DeliveryManifest(ValidatedModel):
    delivery = models.OneToOneField(Delivery, on_delete=models.PROTECT, related_name="manifest")
    edition_revision_ids = models.JSONField(default=list)
    kept_occurrences = models.JSONField(default=list)
    omitted_occurrences = models.JSONField(default=list, blank=True)
    missing_collection_ids = models.JSONField(default=list, blank=True)
    content_hash = models.CharField(max_length=64, editable=False)
    frozen_at = models.DateTimeField(default=timezone.now)

    class Meta:
        verbose_name = "Манифест отправки"
        verbose_name_plural = "Манифесты отправок"

    def clean(self):
        if not isinstance(self.edition_revision_ids, list) or not self.edition_revision_ids:
            raise ValidationError("Манифест должен содержать хотя бы одну редакцию выпуска.")
        if not isinstance(self.kept_occurrences, list) or not self.kept_occurrences:
            raise ValidationError("Манифест должен содержать хотя бы одну историю.")
        if self.frozen_at and timezone.is_naive(self.frozen_at):
            raise ValidationError({"frozen_at": "Дата должна содержать часовой пояс."})

    def save(self, *args, **kwargs):
        payload = json.dumps(
            {
                "delivery_id": str(self.delivery_id),
                "edition_revision_ids": self.edition_revision_ids,
                "kept_occurrences": self.kept_occurrences,
                "omitted_occurrences": self.omitted_occurrences,
                "missing_collection_ids": self.missing_collection_ids,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        self.content_hash = hashlib.sha256(payload.encode()).hexdigest()
        if self.pk:
            old = type(self).objects.get(pk=self.pk)
            immutable = [
                "delivery_id",
                "edition_revision_ids",
                "kept_occurrences",
                "omitted_occurrences",
                "missing_collection_ids",
                "content_hash",
                "frozen_at",
            ]
            if any(getattr(old, field) != getattr(self, field) for field in immutable):
                raise ValidationError("Зафиксированный состав отправки неизменяем.")
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"Манифест {self.delivery_id}"


class DeliveryPart(ValidatedModel):
    delivery = models.ForeignKey(Delivery, on_delete=models.PROTECT, related_name="parts")
    part_number = models.PositiveSmallIntegerField()
    body = models.TextField()
    body_hash = models.CharField(max_length=64, editable=False)
    status = models.CharField(
        max_length=15,
        default="pending",
        choices=[
            ("pending", "Ожидает"),
            ("in_flight", "Передана Telegram"),
            ("sent", "Отправлена"),
            ("retry_wait", "Ожидает повтора"),
            ("failed", "Ошибка"),
            ("cancelled", "Отменена"),
            ("unknown", "Результат неизвестен"),
        ],
    )
    telegram_message_id = models.CharField(max_length=100, blank=True)
    next_attempt_at = models.DateTimeField(default=timezone.now)
    in_flight_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["delivery", "part_number"]
        verbose_name = "Часть отправки"
        verbose_name_plural = "Части отправки"
        constraints = [
            models.UniqueConstraint(fields=["delivery", "part_number"], name="unique_delivery_part")
        ]

    def clean(self):
        if not self.body.strip():
            raise ValidationError({"body": "Часть не может быть пустой."})
        from digest_service.editorial.edition_service import visible_utf16_units

        if visible_utf16_units(self.body) > 3900:
            raise ValidationError({"body": "Часть превышает проектный лимит Telegram."})
        if self.next_attempt_at and timezone.is_naive(self.next_attempt_at):
            raise ValidationError({"next_attempt_at": "Дата должна содержать часовой пояс."})
        if self.in_flight_at and timezone.is_naive(self.in_flight_at):
            raise ValidationError({"in_flight_at": "Дата должна содержать часовой пояс."})

    def save(self, *args, **kwargs):
        self.body_hash = hashlib.sha256(self.body.encode()).hexdigest()
        if self.pk:
            old = type(self).objects.get(pk=self.pk)
            if (old.delivery_id, old.part_number, old.body, old.body_hash) != (
                self.delivery_id,
                self.part_number,
                self.body,
                self.body_hash,
            ):
                raise ValidationError("Текст подготовленной части неизменяем.")
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.delivery_id} · часть {self.part_number}"


class DeliveryAttempt(ValidatedModel):
    part = models.ForeignKey(DeliveryPart, on_delete=models.PROTECT, related_name="attempts")
    attempt_number = models.PositiveSmallIntegerField()
    outcome = models.CharField(
        max_length=20,
        choices=[
            ("sent", "Отправлена"),
            ("temporary_failure", "Временная ошибка"),
            ("permanent_failure", "Постоянная ошибка"),
            ("unknown", "Результат неизвестен"),
        ],
    )
    error_code = models.CharField(max_length=100, blank=True)
    provider_request_id = models.CharField(max_length=200, blank=True)
    telegram_message_id = models.CharField(max_length=100, blank=True)
    attempted_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["part", "attempt_number"]
        verbose_name = "Попытка доставки"
        verbose_name_plural = "Попытки доставки"
        constraints = [
            models.UniqueConstraint(
                fields=["part", "attempt_number"], name="unique_delivery_attempt"
            )
        ]

    def clean(self):
        if self.outcome == "sent" and not self.telegram_message_id:
            raise ValidationError("Успешная попытка должна содержать Telegram message ID.")
        if self.outcome != "sent" and not self.error_code:
            raise ValidationError("Неуспешная попытка должна содержать код результата.")

    def __str__(self):
        return f"{self.part_id} · попытка {self.attempt_number}"
