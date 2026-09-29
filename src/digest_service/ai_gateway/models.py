from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from digest_service.core.models import ValidatedModel


class AIBudgetPeriod(ValidatedModel):
    period_start = models.DateField(unique=True)
    limit_usd = models.DecimalField(max_digits=12, decimal_places=6, default=Decimal("0"))
    reserved_usd = models.DecimalField(max_digits=12, decimal_places=6, default=Decimal("0"))
    spent_usd = models.DecimalField(max_digits=12, decimal_places=6, default=Decimal("0"))

    class Meta:
        ordering = ["-period_start"]
        verbose_name = "Бюджет AI за месяц"
        verbose_name_plural = "Бюджеты AI по месяцам"

    def clean(self):
        if min(self.limit_usd, self.reserved_usd, self.spent_usd) < 0:
            raise ValidationError("Суммы бюджета не могут быть отрицательными.")

    @property
    def available_usd(self):
        return self.limit_usd - self.reserved_usd - self.spent_usd

    def __str__(self):
        return f"{self.period_start:%Y-%m}: ${self.spent_usd} / ${self.limit_usd}"


class AIRequest(ValidatedModel):
    operation = models.CharField(
        max_length=30,
        choices=[
            ("draft_ru", "Черновик RU"),
            ("translate_en", "Перевод EN"),
            ("group_events", "Объединение событий"),
        ],
    )
    provider = models.CharField(max_length=80)
    model = models.CharField(max_length=120)
    input_hash = models.CharField(max_length=64)
    instruction_version = models.CharField(max_length=40, default="editorial-v1")
    status = models.CharField(
        max_length=20,
        default="running",
        choices=[
            ("running", "Выполняется"),
            ("success", "Успешно"),
            ("failed", "Ошибка"),
            ("budget_rejected", "Отклонено бюджетом"),
        ],
    )
    reserved_cost_usd = models.DecimalField(max_digits=12, decimal_places=6, default=0)
    actual_cost_usd = models.DecimalField(max_digits=12, decimal_places=6, default=0)
    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    provider_request_id = models.CharField(max_length=200, blank=True)
    error_code = models.CharField(max_length=80, blank=True)
    started_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-started_at"]
        verbose_name = "Запрос к AI"
        verbose_name_plural = "Запросы к AI"
        indexes = [models.Index(fields=["operation", "input_hash"], name="ai_request_input")]

    def __str__(self):
        return f"{self.operation} · {self.provider}/{self.model} · {self.status}"
