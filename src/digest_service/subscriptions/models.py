import uuid

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from digest_service.catalog.models import Collection
from digest_service.core.models import ValidatedModel
from digest_service.scheduling.models import ScheduleRevision

LANGUAGE_CHOICES = [("ru", "Русский"), ("en", "English")]


class SubscriberGeneration(ValidatedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    telegram_user_id = models.PositiveBigIntegerField("Telegram user ID")
    telegram_chat_id = models.PositiveBigIntegerField("Telegram chat ID")
    generation = models.PositiveIntegerField(default=1)
    is_current = models.BooleanField(default=True)
    interface_language = models.CharField(max_length=2, choices=LANGUAGE_CHOICES, default="ru")
    digest_language = models.CharField(max_length=2, choices=LANGUAGE_CHOICES, default="ru")
    setup_status = models.CharField(
        max_length=12,
        default="incomplete",
        choices=[("incomplete", "Не завершена"), ("complete", "Завершена")],
    )
    subscription_status = models.CharField(
        max_length=12,
        default="inactive",
        choices=[
            ("inactive", "Неактивна"),
            ("active", "Активна"),
            ("paused", "Пауза"),
        ],
    )
    access_status = models.CharField(
        max_length=12,
        default="allowed",
        choices=[("allowed", "Разрешён"), ("blocked", "Заблокирован")],
    )
    chat_reachability = models.CharField(
        max_length=12,
        default="unknown",
        choices=[
            ("unknown", "Неизвестно"),
            ("reachable", "Доступен"),
            ("unreachable", "Недоступен"),
        ],
    )
    dialog_state = models.CharField(
        max_length=30,
        default="choose_language",
        choices=[
            ("none", "Нет"),
            ("choose_language", "Выбор языка"),
            ("choose_collections", "Выбор подборок"),
            ("choose_schedule", "Выбор частоты"),
            ("review_preferences", "Проверка настроек"),
            ("awaiting_feedback", "Ожидание отзыва"),
            ("confirm_data_erasure", "Подтверждение удаления"),
        ],
    )
    privacy_state = models.CharField(
        max_length=20,
        default="normal",
        choices=[
            ("normal", "Обычное"),
            ("erasure_pending", "Удаление начато"),
            ("erased", "Обезличено"),
        ],
    )
    automatic_delivery_after = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Поколение подписчика"
        verbose_name_plural = "Подписчики"
        constraints = [
            models.UniqueConstraint(
                fields=["telegram_user_id", "generation"],
                name="unique_subscriber_generation",
            ),
            models.UniqueConstraint(
                fields=["telegram_user_id"],
                condition=models.Q(is_current=True),
                name="unique_current_subscriber",
            ),
            models.CheckConstraint(
                condition=models.Q(setup_status="complete")
                | models.Q(subscription_status="inactive"),
                name="incomplete_subscriber_is_inactive",
            ),
            models.CheckConstraint(
                condition=~models.Q(privacy_state="erasure_pending", subscription_status="active"),
                name="erasure_pending_is_not_active",
            ),
        ]

    def clean(self):
        if self.setup_status == "incomplete" and self.subscription_status != "inactive":
            raise ValidationError("Незавершённую подписку нельзя включить или поставить на паузу.")
        if self.privacy_state == "erasure_pending" and self.subscription_status == "active":
            raise ValidationError("Во время удаления данных автоматическая подписка запрещена.")

    @property
    def current_preferences(self):
        return self.preference_revisions.filter(is_current=True).first()

    def __str__(self):
        return f"{self.telegram_user_id} · поколение {self.generation}"


class PreferenceRevision(ValidatedModel):
    subscriber = models.ForeignKey(
        SubscriberGeneration, on_delete=models.PROTECT, related_name="preference_revisions"
    )
    revision = models.PositiveIntegerField()
    interface_language = models.CharField(max_length=2, choices=LANGUAGE_CHOICES)
    digest_language = models.CharField(max_length=2, choices=LANGUAGE_CHOICES)
    schedule_revision = models.ForeignKey(
        ScheduleRevision, on_delete=models.PROTECT, related_name="subscriber_preferences"
    )
    effective_from = models.DateTimeField(default=timezone.now)
    is_current = models.BooleanField(default=True)

    class Meta:
        ordering = ["subscriber", "revision"]
        verbose_name = "Редакция настроек"
        verbose_name_plural = "Редакции настроек"
        constraints = [
            models.UniqueConstraint(
                fields=["subscriber", "revision"], name="unique_preference_revision"
            ),
            models.UniqueConstraint(
                fields=["subscriber"],
                condition=models.Q(is_current=True),
                name="unique_current_preferences",
            ),
        ]

    def clean(self):
        if self.effective_from and timezone.is_naive(self.effective_from):
            raise ValidationError({"effective_from": "Дата должна содержать часовой пояс."})
        if self.subscriber_id and not self.subscriber.is_current:
            raise ValidationError("Настройки можно сохранить только для текущего поколения.")

    def save(self, *args, **kwargs):
        if self.pk:
            old = type(self).objects.get(pk=self.pk)
            immutable = [
                "subscriber_id",
                "revision",
                "interface_language",
                "digest_language",
                "schedule_revision_id",
                "effective_from",
            ]
            if any(getattr(old, field) != getattr(self, field) for field in immutable):
                raise ValidationError("Сохранённые настройки неизменяемы; создайте новую редакцию.")
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.subscriber} · r{self.revision}"


class SubscriptionCollection(ValidatedModel):
    preference_revision = models.ForeignKey(
        PreferenceRevision, on_delete=models.PROTECT, related_name="selected_collections"
    )
    collection = models.ForeignKey(
        Collection, on_delete=models.PROTECT, related_name="subscriber_selections"
    )

    class Meta:
        ordering = ["collection__sort_order", "collection_id"]
        verbose_name = "Выбранная подборка"
        verbose_name_plural = "Выбранные подборки"
        constraints = [
            models.UniqueConstraint(
                fields=["preference_revision", "collection"],
                name="unique_preference_collection",
            )
        ]

    def save(self, *args, **kwargs):
        if self.pk:
            old = type(self).objects.get(pk=self.pk)
            if (old.preference_revision_id, old.collection_id) != (
                self.preference_revision_id,
                self.collection_id,
            ):
                raise ValidationError("Состав сохранённой редакции настроек неизменяем.")
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.preference_revision} · {self.collection}"


class PreferenceDraft(ValidatedModel):
    subscriber = models.OneToOneField(
        SubscriberGeneration, on_delete=models.CASCADE, related_name="preference_draft"
    )
    token = models.UUIDField(default=uuid.uuid4, editable=False)
    interface_language = models.CharField(max_length=2, choices=LANGUAGE_CHOICES)
    digest_language = models.CharField(max_length=2, choices=LANGUAGE_CHOICES)
    collection_ids = models.JSONField(default=list, blank=True)
    schedule_id = models.CharField(max_length=100, blank=True)
    expires_at = models.DateTimeField()

    class Meta:
        verbose_name = "Черновик настроек"
        verbose_name_plural = "Черновики настроек"

    def clean(self):
        if not isinstance(self.collection_ids, list):
            raise ValidationError({"collection_ids": "Подборки должны быть списком ID."})
        if any(not isinstance(item, str) or not item for item in self.collection_ids):
            raise ValidationError({"collection_ids": "Некорректный ID подборки."})
        if len(self.collection_ids) != len(set(self.collection_ids)):
            raise ValidationError({"collection_ids": "Подборки не должны повторяться."})
        if self.expires_at and timezone.is_naive(self.expires_at):
            raise ValidationError({"expires_at": "Дата должна содержать часовой пояс."})

    def __str__(self):
        return f"Черновик {self.subscriber}"


class ErasureRequest(ValidatedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    subscriber = models.ForeignKey(
        SubscriberGeneration,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="erasure_requests",
    )
    confirmation_token = models.UUIDField(default=uuid.uuid4, editable=False)
    status = models.CharField(
        max_length=24,
        default="pending_confirmation",
        choices=[
            ("pending_confirmation", "Ожидает подтверждения"),
            ("accepted", "Принято"),
            ("processing", "Выполняется"),
            ("completed", "Завершено"),
            ("failed", "Ошибка"),
            ("cancelled", "Отменено до подтверждения"),
        ],
    )
    confirmation_expires_at = models.DateTimeField()
    accepted_at = models.DateTimeField(null=True, blank=True)
    deadline_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    result = models.JSONField(default=dict, blank=True)
    error_code = models.CharField(max_length=100, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Запрос удаления"
        verbose_name_plural = "Запросы удаления данных"
        constraints = [
            models.UniqueConstraint(
                fields=["subscriber"],
                condition=models.Q(
                    status__in=["pending_confirmation", "accepted", "processing"]
                ),
                name="unique_active_erasure_request",
            )
        ]

    def clean(self):
        for field in (
            "confirmation_expires_at",
            "accepted_at",
            "deadline_at",
            "completed_at",
        ):
            value = getattr(self, field)
            if value and timezone.is_naive(value):
                raise ValidationError({field: "Дата должна содержать часовой пояс."})

    def __str__(self):
        return f"Удаление {self.id} · {self.status}"


class ErasureTombstone(ValidatedModel):
    subject_hash = models.CharField(primary_key=True, max_length=64)
    last_generation = models.PositiveIntegerField()
    completed_at = models.DateTimeField()
    expires_at = models.DateTimeField()

    class Meta:
        verbose_name = "Guard удаления"
        verbose_name_plural = "Guard удаления после восстановления"

    def clean(self):
        if timezone.is_naive(self.completed_at) or timezone.is_naive(self.expires_at):
            raise ValidationError("Даты guard должны содержать часовой пояс.")
        if self.expires_at <= self.completed_at:
            raise ValidationError("Guard должен истекать после завершения удаления.")

    def __str__(self):
        return f"Guard до {self.expires_at}"
