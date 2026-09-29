from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from digest_service.core.models import ValidatedModel


class TelegramUpdateReceipt(ValidatedModel):
    update_id = models.BigIntegerField(unique=True)
    payload_hash = models.CharField(max_length=64)
    update_kind = models.CharField(max_length=30, blank=True)
    status = models.CharField(
        max_length=15,
        default="processing",
        choices=[
            ("processing", "Обрабатывается"),
            ("processed", "Обработано"),
            ("ignored", "Пропущено"),
        ],
    )
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-update_id"]
        verbose_name = "Полученное обновление Telegram"
        verbose_name_plural = "Полученные обновления Telegram"

    def __str__(self):
        return f"update {self.update_id} · {self.status}"


class BotReply(ValidatedModel):
    receipt = models.OneToOneField(
        TelegramUpdateReceipt, on_delete=models.PROTECT, related_name="reply"
    )
    chat_id = models.BigIntegerField()
    callback_query_id = models.CharField(max_length=200, blank=True)
    edit_message_id = models.BigIntegerField(null=True, blank=True)
    text = models.TextField()
    keyboard = models.JSONField(default=list, blank=True)
    parse_mode = models.CharField(max_length=20, default="HTML")
    status = models.CharField(
        max_length=15,
        default="pending",
        choices=[
            ("pending", "Ожидает"),
            ("in_flight", "Передан Telegram"),
            ("sent", "Отправлен"),
            ("retry_wait", "Ожидает повтора"),
            ("failed", "Ошибка"),
            ("unknown", "Результат неизвестен"),
        ],
    )
    attempt_count = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now)
    in_flight_at = models.DateTimeField(null=True, blank=True)
    telegram_message_id = models.CharField(max_length=100, blank=True)
    error_code = models.CharField(max_length=100, blank=True)

    class Meta:
        ordering = ["created_at"]
        verbose_name = "Ответ бота"
        verbose_name_plural = "Ответы бота"

    def clean(self):
        if not self.text.strip():
            raise ValidationError({"text": "Ответ не может быть пустым."})
        if not isinstance(self.keyboard, list):
            raise ValidationError({"keyboard": "Клавиатура должна быть списком строк."})
        if self.next_attempt_at and timezone.is_naive(self.next_attempt_at):
            raise ValidationError({"next_attempt_at": "Дата должна содержать часовой пояс."})
        if self.in_flight_at and timezone.is_naive(self.in_flight_at):
            raise ValidationError({"in_flight_at": "Дата должна содержать часовой пояс."})
        if self.edit_message_id is not None and not self.callback_query_id:
            raise ValidationError(
                {"edit_message_id": "Редактирование сообщения доступно только для callback-ответа."}
            )

    def __str__(self):
        return f"{self.chat_id} · update {self.receipt.update_id} · {self.status}"
