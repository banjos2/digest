import hashlib
import json
import uuid

from django.core.exceptions import ValidationError
from django.db import models

from digest_service.catalog.models import Collection
from digest_service.core.models import ValidatedModel
from digest_service.ingestion.models import PublicationVersion
from digest_service.scheduling.models import ScheduleRevision, ScheduleSlot


class Event(ValidatedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    working_title = models.CharField(max_length=300)
    status = models.CharField(
        max_length=20,
        default="proposed",
        choices=[
            ("proposed", "Предложено"),
            ("review", "На проверке"),
            ("approved", "Подтверждено"),
            ("held", "Отложено"),
            ("merged", "Объединено"),
        ],
    )
    occurred_at = models.DateTimeField(null=True, blank=True)
    review_note = models.TextField(
        blank=True,
        help_text="Обязательно при ручном допуске события с одним происхождением.",
    )
    versions = models.ManyToManyField(
        PublicationVersion, through="EventMembership", related_name="events"
    )

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Событие"
        verbose_name_plural = "События"

    def __str__(self):
        return self.working_title

    def clean(self):
        if self.status == "approved" and not self.pk:
            raise ValidationError("Сначала сохраните событие и добавьте материалы.")
        if self.status == "approved" and self.pk:
            origin_count = self.memberships.values(
                "publication_version__publication__source__origin_group_id"
            ).distinct().count()
            if origin_count < 1:
                raise ValidationError("Нельзя подтвердить событие без материалов.")
            if origin_count < 2 and not self.review_note.strip():
                raise ValidationError(
                    {"review_note": "Объясните допуск события с одним происхождением."}
                )


class EventMembership(ValidatedModel):
    event = models.ForeignKey(Event, on_delete=models.PROTECT, related_name="memberships")
    publication_version = models.ForeignKey(
        PublicationVersion, on_delete=models.PROTECT, related_name="event_memberships"
    )
    role = models.CharField(
        max_length=20,
        default="corroborates",
        choices=[
            ("primary", "Основной материал"),
            ("corroborates", "Подтверждает"),
            ("context", "Контекст"),
            ("contradicts", "Противоречит"),
        ],
    )
    note = models.TextField(blank=True)

    class Meta:
        verbose_name = "Материал события"
        verbose_name_plural = "Материалы события"
        constraints = [
            models.UniqueConstraint(
                fields=["event", "publication_version"], name="unique_event_version"
            )
        ]

    def __str__(self):
        return f"{self.event_id} · {self.publication_version_id}"

    def save(self, *args, **kwargs):
        had_cards = (
            bool(self.event_id)
            and EventCardRevision.objects.filter(event_id=self.event_id).exists()
        )
        if self.pk and had_cards:
            old = type(self).objects.get(pk=self.pk)
            if (old.event_id, old.publication_version_id, old.role) != (
                self.event_id,
                self.publication_version_id,
                self.role,
            ):
                raise ValidationError(
                    "Связь, использованную редакцией, нельзя подменить задним числом."
                )
        super().save(*args, **kwargs)
        if had_cards:
            EventCardRevision.objects.filter(event_id=self.event_id, is_current=True).update(
                is_current=False
            )

    def delete(self, *args, **kwargs):
        if EventCardRevision.objects.filter(event_id=self.event_id).exists():
            raise ValidationError("Связь из истории редакций нельзя удалить.")
        return super().delete(*args, **kwargs)


class EventCollectionAssignment(ValidatedModel):
    event = models.ForeignKey(Event, on_delete=models.PROTECT, related_name="collection_assignments")
    collection = models.ForeignKey(
        Collection, on_delete=models.PROTECT, related_name="event_assignments"
    )
    is_active = models.BooleanField(default=True)
    assigned_by = models.CharField(
        max_length=20,
        default="editor",
        choices=[
            ("editor", "Редактор"),
            ("rule", "Правило"),
            ("ai", "AI-классификатор"),
        ],
    )
    rationale = models.TextField("Основание")

    class Meta:
        verbose_name = "Назначение события в подборку"
        verbose_name_plural = "Назначения событий в подборки"
        constraints = [
            models.UniqueConstraint(
                fields=["event", "collection"], name="unique_event_collection_assignment"
            )
        ]

    def clean(self):
        if self.is_active and not self.rationale.strip():
            raise ValidationError({"rationale": "Укажите основание назначения в подборку."})

    def __str__(self):
        return f"{self.collection}: {self.event}"


class GroupingSuggestion(ValidatedModel):
    first_version = models.ForeignKey(
        PublicationVersion, on_delete=models.PROTECT, related_name="grouping_suggestions_as_first"
    )
    second_version = models.ForeignKey(
        PublicationVersion, on_delete=models.PROTECT, related_name="grouping_suggestions_as_second"
    )
    score = models.DecimalField(max_digits=5, decimal_places=4)
    reasons = models.JSONField(default=dict)
    algorithm_version = models.CharField(max_length=40, default="lexical-v1")
    status = models.CharField(
        max_length=20,
        default="pending",
        choices=[
            ("pending", "Ожидает проверки"),
            ("accepted", "Подтверждено"),
            ("rejected", "Отклонено"),
        ],
    )

    class Meta:
        ordering = ["-score", "-created_at"]
        verbose_name = "Предложение объединения"
        verbose_name_plural = "Предложения объединения"
        constraints = [
            models.UniqueConstraint(
                fields=["first_version", "second_version", "algorithm_version"],
                name="unique_grouping_pair_version",
            ),
            models.CheckConstraint(
                condition=models.Q(first_version_id__lt=models.F("second_version_id")),
                name="ordered_grouping_pair",
            ),
        ]

    def clean(self):
        if self.first_version_id and self.second_version_id:
            if self.first_version_id >= self.second_version_id:
                raise ValidationError("Пара должна храниться в стабильном порядке.")
            if (
                self.first_version.publication.source_id
                == self.second_version.publication.source_id
            ):
                raise ValidationError("Версии одного источника не образуют межисточниковую пару.")
        if self.score < 0 or self.score > 1:
            raise ValidationError({"score": "Оценка должна быть от 0 до 1."})

    def __str__(self):
        return f"{self.first_version_id} ↔ {self.second_version_id} · {self.score}"


class EventCardRevision(ValidatedModel):
    event = models.ForeignKey(Event, on_delete=models.PROTECT, related_name="card_revisions")
    slot_key = models.CharField(max_length=160)
    language = models.CharField(max_length=2, choices=[("ru", "Русский"), ("en", "English")])
    revision = models.PositiveIntegerField()
    editorial_state = models.CharField(
        max_length=20,
        default="draft",
        choices=[
            ("draft", "Черновик"),
            ("review", "На проверке"),
            ("approved", "Утверждено"),
            ("published", "Опубликовано"),
            ("held", "Отложено"),
        ],
    )
    is_current = models.BooleanField(default=True)
    title = models.CharField(max_length=300)
    summary = models.TextField()
    evidence_membership_ids = models.JSONField(default=list)
    citation_membership_ids = models.JSONField(default=list)
    evidence_hash = models.CharField(max_length=64)
    content_hash = models.CharField(max_length=64, editable=False)
    source_ru_revision = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="translations",
    )
    change_note = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ["event", "slot_key", "language", "revision"]
        verbose_name = "Редакция карточки"
        verbose_name_plural = "Редакции карточек"
        constraints = [
            models.UniqueConstraint(
                fields=["event", "slot_key", "language", "revision"],
                name="unique_event_card_revision",
            ),
            models.UniqueConstraint(
                fields=["event", "slot_key", "language"],
                condition=models.Q(is_current=True),
                name="unique_current_event_card",
            ),
            models.CheckConstraint(
                condition=(models.Q(language="ru", source_ru_revision__isnull=True))
                | (models.Q(language="en", source_ru_revision__isnull=False)),
                name="translation_requires_ru_revision",
            ),
        ]

    def clean(self):
        if not self.title.strip():
            raise ValidationError({"title": "Заголовок обязателен."})
        if not self.summary.strip():
            raise ValidationError({"summary": "Текст карточки обязателен."})
        if not isinstance(self.citation_membership_ids, list) or not self.citation_membership_ids:
            raise ValidationError({"citation_membership_ids": "Нужна хотя бы одна ссылка."})
        if not isinstance(self.evidence_membership_ids, list) or not self.evidence_membership_ids:
            raise ValidationError({"evidence_membership_ids": "Нужен снимок материалов события."})
        if len(self.evidence_membership_ids) != len(set(self.evidence_membership_ids)):
            raise ValidationError({"evidence_membership_ids": "Материалы не должны повторяться."})
        if not set(self.citation_membership_ids).issubset(self.evidence_membership_ids):
            raise ValidationError(
                {"citation_membership_ids": "Ссылка отсутствует в снимке материалов."}
            )
        if len(self.citation_membership_ids) != len(set(self.citation_membership_ids)):
            raise ValidationError({"citation_membership_ids": "Ссылки не должны повторяться."})
        if len(self.citation_membership_ids) > 6:
            raise ValidationError(
                {"citation_membership_ids": "В карточке допускается до 6 ссылок."}
            )
        if self.event_id and len(
            set(
                EventMembership.objects.filter(
                    event_id=self.event_id, pk__in=self.evidence_membership_ids
                ).values_list("pk", flat=True)
            )
        ) != len(self.evidence_membership_ids):
            raise ValidationError({"evidence_membership_ids": "Снимок содержит чужой материал."})
        if self.language == "ru" and self.source_ru_revision_id:
            raise ValidationError({"source_ru_revision": "Русская карточка является исходной."})
        if self.language == "en":
            if not self.source_ru_revision_id:
                raise ValidationError(
                    {"source_ru_revision": "Перевод привязан к русской редакции."}
                )
            elif self.source_ru_revision.language != "ru":
                raise ValidationError(
                    {"source_ru_revision": "Исходная редакция должна быть русской."}
                )
            elif self.source_ru_revision.event_id != self.event_id:
                raise ValidationError(
                    {"source_ru_revision": "Перевод и оригинал относятся к событию."}
                )
            elif self.source_ru_revision.slot_key != self.slot_key:
                raise ValidationError(
                    {"source_ru_revision": "Перевод и оригинал относятся к слоту."}
                )
            elif (
                self.evidence_hash != self.source_ru_revision.evidence_hash
                or self.evidence_membership_ids != self.source_ru_revision.evidence_membership_ids
                or self.citation_membership_ids != self.source_ru_revision.citation_membership_ids
            ):
                raise ValidationError(
                    "Перевод должен использовать доказательную базу и ссылки русской редакции."
                )

    def save(self, *args, **kwargs):
        payload = json.dumps(
            {
                "title": self.title.strip(),
                "summary": self.summary.strip(),
                "evidence": self.evidence_membership_ids,
                "citations": self.citation_membership_ids,
                "evidence_hash": self.evidence_hash,
                "source_ru_revision_id": self.source_ru_revision_id,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        self.content_hash = hashlib.sha256(payload.encode()).hexdigest()
        if self.pk:
            old = type(self).objects.get(pk=self.pk)
            immutable = [
                "event_id",
                "slot_key",
                "language",
                "revision",
                "title",
                "summary",
                "evidence_membership_ids",
                "citation_membership_ids",
                "evidence_hash",
                "source_ru_revision_id",
                "content_hash",
            ]
            if any(getattr(old, field) != getattr(self, field) for field in immutable):
                raise ValidationError("Текст и основание редакции неизменяемы; создайте новую.")
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.language.upper()} · {self.event} · r{self.revision}"


class Edition(ValidatedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    collection = models.ForeignKey(Collection, on_delete=models.PROTECT, related_name="editions")
    schedule_revision = models.ForeignKey(
        ScheduleRevision, on_delete=models.PROTECT, related_name="editions"
    )
    scheduled_at = models.DateTimeField()
    window_start = models.DateTimeField()
    window_end = models.DateTimeField()

    class Meta:
        ordering = ["-scheduled_at"]
        verbose_name = "Выпуск"
        verbose_name_plural = "Выпуски"
        constraints = [
            models.UniqueConstraint(
                fields=["collection", "schedule_revision", "scheduled_at"],
                name="unique_collection_schedule_slot",
            )
        ]

    def clean(self):
        if self.window_start >= self.window_end:
            raise ValidationError("Начало окна должно быть раньше конца.")
        if self.scheduled_at < self.window_end:
            raise ValidationError("Выпуск нельзя планировать раньше окончания окна.")

    def __str__(self):
        return f"{self.collection} · {self.scheduled_at:%Y-%m-%d %H:%M}"


class EditionRevision(ValidatedModel):
    edition = models.ForeignKey(Edition, on_delete=models.PROTECT, related_name="revisions")
    language = models.CharField(max_length=2, choices=[("ru", "Русский"), ("en", "English")])
    revision = models.PositiveIntegerField()
    editorial_state = models.CharField(
        max_length=20,
        default="draft",
        choices=[
            ("draft", "Черновик"),
            ("review", "На проверке"),
            ("approved", "Утверждено"),
            ("published", "Опубликовано"),
            ("held", "Отложено"),
        ],
    )
    is_current = models.BooleanField(default=True)
    source_ru_revision = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="translations",
    )
    item_snapshot = models.JSONField(default=list)
    rendered_parts = models.JSONField(default=list)
    content_hash = models.CharField(max_length=64, editable=False)
    change_note = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ["edition", "language", "revision"]
        verbose_name = "Редакция выпуска"
        verbose_name_plural = "Редакции выпусков"
        constraints = [
            models.UniqueConstraint(
                fields=["edition", "language", "revision"],
                name="unique_edition_revision",
            ),
            models.UniqueConstraint(
                fields=["edition", "language"],
                condition=models.Q(is_current=True),
                name="unique_current_edition_revision",
            ),
            models.CheckConstraint(
                condition=(models.Q(language="ru", source_ru_revision__isnull=True))
                | (models.Q(language="en", source_ru_revision__isnull=False)),
                name="edition_translation_requires_ru",
            ),
        ]

    def clean(self):
        if not isinstance(self.item_snapshot, list) or not self.item_snapshot:
            raise ValidationError({"item_snapshot": "В выпуске должна быть хотя бы одна история."})
        if not isinstance(self.rendered_parts, list) or not self.rendered_parts:
            raise ValidationError({"rendered_parts": "Выпуск должен содержать готовые части."})
        if self.language == "ru" and self.source_ru_revision_id:
            raise ValidationError({"source_ru_revision": "Русский выпуск является исходным."})
        if self.language == "en":
            if not self.source_ru_revision_id:
                raise ValidationError({"source_ru_revision": "EN выпуск привязан к RU-редакции."})
            elif (
                self.source_ru_revision.language != "ru"
                or self.source_ru_revision.edition_id != self.edition_id
            ):
                raise ValidationError({"source_ru_revision": "Выбрана чужая RU-редакция."})
            else:
                ru_shape = [
                    (item["event_id"], item["tier"], item["position"])
                    for item in self.source_ru_revision.item_snapshot
                ]
                en_shape = [
                    (item["event_id"], item["tier"], item["position"])
                    for item in self.item_snapshot
                ]
                if en_shape != ru_shape:
                    raise ValidationError(
                        "EN выпуск должен повторять события и порядок RU-выпуска."
                    )

    def save(self, *args, **kwargs):
        payload = json.dumps(
            {
                "language": self.language,
                "items": self.item_snapshot,
                "parts": self.rendered_parts,
                "source_ru_revision_id": self.source_ru_revision_id,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        self.content_hash = hashlib.sha256(payload.encode()).hexdigest()
        if self.pk:
            old = type(self).objects.get(pk=self.pk)
            immutable = [
                "edition_id",
                "language",
                "revision",
                "source_ru_revision_id",
                "item_snapshot",
                "rendered_parts",
                "content_hash",
            ]
            if any(getattr(old, field) != getattr(self, field) for field in immutable):
                raise ValidationError("Содержимое выпуска неизменяемо; создайте новую редакцию.")
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.language.upper()} · {self.edition} · r{self.revision}"


class EditionItem(ValidatedModel):
    edition_revision = models.ForeignKey(
        EditionRevision, on_delete=models.PROTECT, related_name="items"
    )
    card_revision = models.ForeignKey(
        EventCardRevision, on_delete=models.PROTECT, related_name="edition_items"
    )
    tier = models.CharField(
        max_length=20, choices=[("main", "Главная тема"), ("additional", "Дополнительно")]
    )
    position = models.PositiveSmallIntegerField()

    class Meta:
        ordering = ["position"]
        verbose_name = "История выпуска"
        verbose_name_plural = "Истории выпуска"
        constraints = [
            models.UniqueConstraint(
                fields=["edition_revision", "position"], name="unique_edition_position"
            ),
            models.UniqueConstraint(
                fields=["edition_revision", "card_revision"], name="unique_edition_card"
            ),
        ]

    def __str__(self):
        return f"{self.edition_revision_id} · {self.position}"


class EditorialSelection(ValidatedModel):
    slot = models.ForeignKey(ScheduleSlot, on_delete=models.PROTECT, related_name="selections")
    collection = models.ForeignKey(
        Collection, on_delete=models.PROTECT, related_name="editorial_selections"
    )
    event = models.ForeignKey(Event, on_delete=models.PROTECT, related_name="selections")
    status = models.CharField(
        max_length=15,
        default="pending",
        choices=[
            ("pending", "Ожидает оценки"),
            ("main", "Главная тема"),
            ("additional", "Дополнительно"),
            ("excluded", "Исключено"),
            ("held", "Отложено"),
        ],
    )
    impact = models.PositiveSmallIntegerField(null=True, blank=True)
    relevance = models.PositiveSmallIntegerField(null=True, blank=True)
    novelty = models.PositiveSmallIntegerField(null=True, blank=True)
    timeliness = models.PositiveSmallIntegerField(null=True, blank=True)
    corroboration = models.PositiveSmallIntegerField(null=True, blank=True)
    weighted_score = models.DecimalField(max_digits=5, decimal_places=2, null=True, editable=False)
    rationale = models.TextField(blank=True)

    class Meta:
        ordering = ["collection__sort_order", "-weighted_score", "event_id"]
        verbose_name = "Отбор сюжета"
        verbose_name_plural = "Редакторский отбор"
        constraints = [
            models.UniqueConstraint(
                fields=["slot", "collection", "event"], name="unique_editorial_selection"
            )
        ]

    def clean(self):
        components = [
            self.impact,
            self.relevance,
            self.novelty,
            self.timeliness,
            self.corroboration,
        ]
        self.weighted_score = self._calculated_score(components)
        if any(value is not None and value > 5 for value in components):
            raise ValidationError("Каждая оценка должна быть от 0 до 5.")
        selected = self.status in ["main", "additional"]
        if selected and (any(value is None for value in components) or not self.rationale.strip()):
            raise ValidationError("Для выбранного сюжета нужны все оценки и обоснование.")
        if selected and self.event.status != "approved":
            raise ValidationError("В выпуск можно выбрать только подтверждённое событие.")
        if selected and self.relevance < 3:
            raise ValidationError("Релевантность выбранного сюжета должна быть не ниже 3.")
        if self.status == "main" and (self.weighted_score or 0) < 60:
            raise ValidationError("Для главной темы нужна оценка не ниже 60.")
        if self.status == "main" and self.impact < 3:
            raise ValidationError("Для главной темы влияние должно быть не ниже 3.")
        if self.status == "additional" and (self.weighted_score or 0) < 40:
            raise ValidationError("Для дополнительного сюжета нужна оценка не ниже 40.")

    def save(self, *args, **kwargs):
        components = [
            self.impact,
            self.relevance,
            self.novelty,
            self.timeliness,
            self.corroboration,
        ]
        self.weighted_score = self._calculated_score(components)
        super().save(*args, **kwargs)

    @staticmethod
    def _calculated_score(components):
        if any(value is None for value in components):
            return None
        impact, relevance, novelty, timeliness, corroboration = components
        return impact * 7 + relevance * 5 + novelty * 4 + timeliness * 2 + corroboration * 2

    def __str__(self):
        return f"{self.collection} · {self.event}"
