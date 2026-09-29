from urllib.parse import urlsplit

from django.core.exceptions import ValidationError
from django.db import models

from digest_service.core.models import ValidatedModel


class OriginGroup(ValidatedModel):
    id = models.SlugField(primary_key=True, max_length=100)
    name = models.CharField("Название", max_length=240)

    class Meta:
        verbose_name = "Группа происхождения"
        verbose_name_plural = "Группы происхождения"

    def __str__(self):
        return self.name


class RetentionPolicy(ValidatedModel):
    id = models.SlugField(primary_key=True, max_length=100)
    name = models.CharField("Название", max_length=240)
    definition = models.JSONField("Правило хранения")

    class Meta:
        verbose_name = "Профиль хранения"
        verbose_name_plural = "Профили хранения"

    def __str__(self):
        return self.name


class PublisherProfile(ValidatedModel):
    id = models.SlugField(primary_key=True, max_length=100)
    name = models.CharField("Редакция или автор", max_length=240)
    origin_group = models.ForeignKey(
        OriginGroup, on_delete=models.PROTECT, verbose_name="Происхождение"
    )
    role = models.CharField("Роль", max_length=50)
    default_language = models.CharField("Язык", max_length=12)
    notes = models.TextField("Примечания", blank=True)
    research_metadata = models.JSONField("Сведения исследования", default=dict, blank=True)

    class Meta:
        verbose_name = "Профиль редакции"
        verbose_name_plural = "Профили редакций"

    def __str__(self):
        return self.name


class Collection(ValidatedModel):
    id = models.SlugField(primary_key=True, max_length=100)
    name_ru = models.CharField("Название RU", max_length=240)
    name_en = models.CharField("Название EN", max_length=240)
    description_ru = models.TextField("Описание RU", blank=True)
    description_en = models.TextField("Описание EN", blank=True)
    kind = models.CharField(
        "Тип",
        max_length=30,
        default="news",
        choices=[
            ("news", "Новости"),
            ("analysis_digest", "Аналитика"),
            ("video_digest", "Видео — следующая версия"),
        ],
    )
    release_target = models.CharField(
        "Версия",
        max_length=20,
        default="v1",
        choices=[
            ("v1", "Первая версия"),
            ("later_version", "Следующая версия"),
        ],
    )
    is_active = models.BooleanField("Активна", default=False)
    sort_order = models.PositiveIntegerField("Порядок", default=100)
    routing_keywords = models.JSONField(
        "Ключевые слова маршрутизации",
        default=list,
        blank=True,
        help_text="Список слов или фраз для автоматического назначения событий в подборку.",
    )
    sources = models.ManyToManyField(
        "Source", through="SourceCollection", related_name="collections"
    )

    class Meta:
        ordering = ["sort_order", "id"]
        verbose_name = "Подборка"
        verbose_name_plural = "Подборки"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(is_active=False)
                | (models.Q(release_target="v1") & ~models.Q(kind="video_digest")),
                name="active_collection_requires_v1_text",
            )
        ]

    def clean(self):
        if not isinstance(self.routing_keywords, list) or any(
            not isinstance(value, str) or not value.strip() for value in self.routing_keywords
        ):
            raise ValidationError(
                {"routing_keywords": "Укажите список непустых строк."}
            )
        if self.is_active and (self.release_target != "v1" or self.kind == "video_digest"):
            raise ValidationError(
                {"is_active": "В первой версии доступны только текстовые подборки v1."}
            )

    def __str__(self):
        return self.name_ru


class Source(ValidatedModel):
    id = models.SlugField(primary_key=True, max_length=120)
    profile = models.ForeignKey(PublisherProfile, on_delete=models.PROTECT, verbose_name="Редакция")
    origin_group = models.ForeignKey(
        OriginGroup, on_delete=models.PROTECT, verbose_name="Происхождение"
    )
    kind = models.CharField(
        "Тип", max_length=20, choices=[("website", "Сайт"), ("telegram", "Telegram")]
    )
    url = models.URLField("Адрес", max_length=2000, unique=True)
    input_language = models.CharField("Язык", max_length=12)
    retention_policy = models.ForeignKey(
        RetentionPolicy, on_delete=models.PROTECT, verbose_name="Хранение"
    )
    is_active = models.BooleanField("Активен", default=False)
    access_review_status = models.CharField(
        "Проверка использования",
        max_length=20,
        default="pending",
        choices=[
            ("pending", "Не проверено"),
            ("approved", "Способ подтверждён"),
            ("denied", "Недоступен"),
        ],
    )
    access_review_note = models.TextField("Способ использования и основание", blank=True)
    collection_check_status = models.CharField(
        "Проверка сборщика",
        max_length=30,
        default="not_connected",
        choices=[
            ("not_connected", "Не подключён"),
            ("verified", "Проверен"),
            ("failed", "Ошибка"),
        ],
    )
    preferred_adapter = models.CharField("Адаптер", max_length=100, blank=True)
    preferred_feed_url = models.URLField("RSS", max_length=2000, blank=True)
    technical_status = models.CharField("Результат исследования", max_length=80, blank=True)
    research_metadata = models.JSONField("Сведения исследования", default=dict, blank=True)

    class Meta:
        verbose_name = "Источник"
        verbose_name_plural = "Источники"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(is_active=False)
                | (
                    models.Q(access_review_status="approved", collection_check_status="verified")
                    & ~models.Q(access_review_note="")
                    & ~models.Q(preferred_adapter="")
                ),
                name="source_activation_requires_review",
            )
        ]

    def clean(self):
        if self.is_active and (
            self.access_review_status != "approved"
            or self.collection_check_status != "verified"
            or not self.access_review_note.strip()
            or not self.preferred_adapter.strip()
        ):
            raise ValidationError(
                {
                    "is_active": "Для активации нужны проверенный сборщик, адаптер и подтверждённый способ использования с основанием."
                }
            )
        if self.is_active and self.kind == "telegram":
            parsed = urlsplit(self.url)
            channel_name = parsed.path.strip("/")
            if self.preferred_adapter != "telethon_public_channel":
                raise ValidationError(
                    {
                        "is_active": "Для Telegram-источника требуется изолированный Telethon-адаптер."
                    }
                )
            if parsed.hostname not in {"t.me", "www.t.me"} or not channel_name or "/" in channel_name:
                raise ValidationError(
                    {"url": "Поддерживается только публичный адрес канала вида https://t.me/name."}
                )

    def __str__(self):
        return f"{self.profile.name} · {self.get_kind_display()}"


class SourceCollection(ValidatedModel):
    source = models.ForeignKey(Source, on_delete=models.PROTECT, verbose_name="Источник")
    collection = models.ForeignKey(Collection, on_delete=models.PROTECT, verbose_name="Подборка")
    is_active = models.BooleanField("Включён в подборку", default=False)
    priority = models.CharField(
        "Приоритет",
        max_length=20,
        choices=[("pilot", "Пилот"), ("reserve", "Резерв")],
        default="reserve",
    )
    usage = models.CharField("Назначение", max_length=80, default="topic_filtered_news")
    topic_filter = models.TextField("Тематический фильтр", blank=True)
    editorial_note = models.TextField("Примечание", blank=True)

    class Meta:
        verbose_name = "Связь источника и подборки"
        verbose_name_plural = "Источники в подборках"
        constraints = [
            models.UniqueConstraint(
                fields=["source", "collection"], name="unique_source_collection"
            )
        ]

    def __str__(self):
        return f"{self.collection}: {self.source}"
