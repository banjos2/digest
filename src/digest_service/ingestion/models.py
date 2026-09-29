from django.core.exceptions import ValidationError
from django.db import models

from digest_service.catalog.models import Source
from digest_service.core.models import ValidatedModel


class SourceProbeRun(ValidatedModel):
    source = models.ForeignKey(Source, on_delete=models.PROTECT, related_name="probe_runs")
    status = models.CharField(
        max_length=12,
        choices=[("running", "Выполняется"), ("success", "Доступен"), ("failed", "Ошибка")],
        default="running",
    )
    http_status = models.PositiveSmallIntegerField(null=True, blank=True)
    final_url = models.URLField(max_length=2000, blank=True)
    content_type = models.CharField(max_length=120, blank=True)
    response_bytes = models.PositiveIntegerField(default=0)
    item_count = models.PositiveIntegerField(default=0)
    dated_item_count = models.PositiveIntegerField(default=0)
    article_attempted_count = models.PositiveIntegerField(default=0)
    article_success_count = models.PositiveIntegerField(default=0)
    latency_ms = models.PositiveIntegerField(default=0)
    error_code = models.CharField(max_length=80, blank=True)
    started_at = models.DateTimeField()
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Проверка источника"
        verbose_name_plural = "Проверки источников"
        ordering = ["-started_at"]

    def __str__(self):
        return f"{self.source_id} · {self.status}"


class SourceFetchState(ValidatedModel):
    source = models.OneToOneField(
        Source, on_delete=models.PROTECT, related_name="fetch_state", primary_key=True
    )
    etag = models.CharField(max_length=500, blank=True)
    last_modified = models.CharField(max_length=500, blank=True)
    last_external_id = models.CharField(max_length=200, blank=True)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    last_success_at = models.DateTimeField(null=True, blank=True)
    last_status = models.CharField(
        max_length=30,
        default="never",
        choices=[
            ("never", "Не запускался"),
            ("success", "Успешно"),
            ("not_modified", "Без изменений"),
            ("failed", "Ошибка"),
        ],
    )
    last_http_status = models.PositiveSmallIntegerField(null=True, blank=True)
    last_error_code = models.CharField(max_length=80, blank=True)
    consecutive_failures = models.PositiveIntegerField(default=0)

    class Meta:
        verbose_name = "Состояние получения"
        verbose_name_plural = "Состояния получения"

    def __str__(self):
        return f"{self.source_id}: {self.last_status}"


class IngestionRun(ValidatedModel):
    source = models.ForeignKey(Source, on_delete=models.PROTECT, related_name="ingestion_runs")
    status = models.CharField(
        max_length=20,
        choices=[
            ("running", "Выполняется"),
            ("success", "Успешно"),
            ("not_modified", "Без изменений"),
            ("failed", "Ошибка"),
        ],
        default="running",
    )
    requested_limit = models.PositiveSmallIntegerField(default=10)
    discovered_count = models.PositiveIntegerField(default=0)
    created_count = models.PositiveIntegerField(default=0)
    new_version_count = models.PositiveIntegerField(default=0)
    unchanged_count = models.PositiveIntegerField(default=0)
    rejected_count = models.PositiveIntegerField(default=0)
    error_code = models.CharField(max_length=80, blank=True)
    started_at = models.DateTimeField()
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Запуск получения"
        verbose_name_plural = "Запуски получения"
        ordering = ["-started_at"]

    def __str__(self):
        return f"{self.source_id} · {self.started_at:%Y-%m-%d %H:%M}"


class Publication(ValidatedModel):
    source = models.ForeignKey(Source, on_delete=models.PROTECT, related_name="publications")
    external_id = models.CharField(max_length=1000)
    canonical_url = models.URLField(max_length=2000)
    title = models.CharField(max_length=1000)
    published_at = models.DateTimeField(null=True, blank=True)
    first_seen_at = models.DateTimeField()
    last_seen_at = models.DateTimeField()

    class Meta:
        verbose_name = "Публикация"
        verbose_name_plural = "Публикации"
        constraints = [
            models.UniqueConstraint(fields=["source", "external_id"], name="unique_source_item")
        ]
        indexes = [models.Index(fields=["source", "-published_at"], name="publication_source_time")]

    def __str__(self):
        return self.title


class PublicationVersion(ValidatedModel):
    publication = models.ForeignKey(Publication, on_delete=models.PROTECT, related_name="versions")
    number = models.PositiveIntegerField()
    content_hash = models.CharField(max_length=64)
    title = models.CharField(max_length=1000)
    body = models.TextField(blank=True)
    language = models.CharField(max_length=12)
    content_scope = models.CharField(
        max_length=30,
        choices=[
            ("full_article", "Полный извлечённый текст"),
            ("feed_excerpt", "Фрагмент из ленты"),
            ("feed_title", "Только заголовок ленты"),
            ("page_extract", "Текст страницы"),
            ("telegram_post", "Публичный пост Telegram"),
        ],
    )
    fetched_at = models.DateTimeField()
    expires_at = models.DateTimeField()
    purged_at = models.DateTimeField(null=True, blank=True)
    purge_reason = models.CharField(max_length=80, blank=True)
    extraction_metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        verbose_name = "Версия публикации"
        verbose_name_plural = "Версии публикаций"
        ordering = ["publication", "number"]
        constraints = [
            models.UniqueConstraint(
                fields=["publication", "number"], name="unique_publication_version_number"
            ),
            models.UniqueConstraint(
                fields=["publication", "content_hash"], name="unique_publication_content_hash"
            ),
        ]
        indexes = [models.Index(fields=["expires_at"], name="publication_version_expiry")]

    def clean(self):
        if not self.body.strip() and not self.purged_at:
            raise ValidationError({"body": "Пустой текст не сохраняется."})
        if self.purged_at and self.body:
            raise ValidationError({"body": "После очистки исходный текст должен быть пуст."})
        if self.fetched_at and self.expires_at and self.expires_at <= self.fetched_at:
            raise ValidationError({"expires_at": "Срок хранения должен быть позже получения."})

    def __str__(self):
        return f"{self.publication_id} · v{self.number}"

    def purge_content(self, *, at, reason="retention_expired"):
        if self.purged_at:
            return False
        self.body = ""
        self.purged_at = at
        self.purge_reason = reason
        self.extraction_metadata = {"purged": True, "reason": reason}
        self.save()
        return True
