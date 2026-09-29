import hashlib

from django.contrib import admin, messages
from django.core.exceptions import ValidationError

from digest_service.editorial.service import evidence_hash
from digest_service.operations.service import enqueue_job

from .models import (
    Edition,
    EditionItem,
    EditionRevision,
    EditorialSelection,
    Event,
    EventCardRevision,
    EventCollectionAssignment,
    EventMembership,
    GroupingSuggestion,
)
from .workflow import (
    accept_grouping_suggestion,
    approve_card_pair,
    approve_edition_pair,
    publish_edition_pair,
)


def _show_error(admin_model, request, error):
    admin_model.message_user(request, " ".join(error.messages), level=messages.ERROR)


class EventMembershipInline(admin.TabularInline):
    model = EventMembership
    extra = 0
    autocomplete_fields = ["publication_version"]


class EventCardRevisionInline(admin.TabularInline):
    model = EventCardRevision
    extra = 0
    can_delete = False
    fields = ["language", "revision", "editorial_state", "is_current", "title"]
    readonly_fields = ["language", "revision", "editorial_state", "is_current", "title"]

    def has_add_permission(self, request, obj=None):
        return False


class EventCollectionAssignmentInline(admin.TabularInline):
    model = EventCollectionAssignment
    extra = 0
    autocomplete_fields = ["collection"]


@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    list_display = ["working_title", "status", "occurred_at", "created_at"]
    list_filter = ["status"]
    search_fields = ["working_title", "review_note"]
    inlines = [EventMembershipInline, EventCollectionAssignmentInline, EventCardRevisionInline]
    actions = ["approve_selected"]

    @admin.action(description="Подтвердить выбранные события после проверки")
    def approve_selected(self, request, queryset):
        approved = 0
        for event in queryset.filter(status="review"):
            event.status = "approved"
            try:
                event.full_clean()
                event.save()
            except ValidationError as error:
                _show_error(self, request, error)
            else:
                approved += 1
        self.message_user(request, f"Подтверждено событий: {approved}")


@admin.register(EventCollectionAssignment)
class EventCollectionAssignmentAdmin(admin.ModelAdmin):
    list_display = ["event", "collection", "assigned_by", "is_active"]
    list_filter = ["collection", "assigned_by", "is_active"]
    search_fields = ["event__working_title", "rationale"]
    autocomplete_fields = ["event", "collection"]


@admin.register(EventMembership)
class EventMembershipAdmin(admin.ModelAdmin):
    list_display = ["event", "publication_version", "role"]
    list_filter = ["role"]
    autocomplete_fields = ["event", "publication_version"]


@admin.register(GroupingSuggestion)
class GroupingSuggestionAdmin(admin.ModelAdmin):
    list_display = ["first_version", "second_version", "score", "status", "created_at"]
    list_filter = ["status", "algorithm_version"]
    search_fields = [
        "first_version__title",
        "second_version__title",
        "first_version__publication__source__profile__name",
        "second_version__publication__source__profile__name",
    ]
    readonly_fields = [
        "first_version",
        "second_version",
        "score",
        "reasons",
        "algorithm_version",
        "created_at",
        "updated_at",
    ]
    actions = ["accept_selected"]

    @admin.action(description="Принять и создать/дополнить событие")
    def accept_selected(self, request, queryset):
        accepted = 0
        for suggestion in queryset:
            try:
                accept_grouping_suggestion(suggestion=suggestion)
            except ValidationError as error:
                _show_error(self, request, error)
            else:
                accepted += 1
        self.message_user(request, f"Принято предложений: {accepted}")

    def has_add_permission(self, request):
        return False


@admin.register(EventCardRevision)
class EventCardRevisionAdmin(admin.ModelAdmin):
    list_display = ["event", "slot_key", "language", "revision", "editorial_state", "is_current"]
    list_filter = ["language", "editorial_state", "is_current"]
    search_fields = ["event__working_title", "title", "summary", "slot_key"]
    readonly_fields = [
        "event",
        "slot_key",
        "language",
        "revision",
        "editorial_state",
        "is_current",
        "title",
        "summary",
        "evidence_membership_ids",
        "citation_membership_ids",
        "evidence_hash",
        "content_hash",
        "source_ru_revision",
        "change_note",
    ]
    actions = ["approve_pairs"]

    @admin.action(description="Утвердить текущие RU/EN-пары карточек")
    def approve_pairs(self, request, queryset):
        approved = set()
        for revision in queryset:
            try:
                russian, _ = approve_card_pair(revision=revision)
            except ValidationError as error:
                _show_error(self, request, error)
            else:
                approved.add(russian.pk)
        self.message_user(request, f"Утверждено пар карточек: {len(approved)}")

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class EditionRevisionInline(admin.TabularInline):
    model = EditionRevision
    extra = 0
    can_delete = False
    fields = ["language", "revision", "editorial_state", "is_current", "content_hash"]
    readonly_fields = ["language", "revision", "editorial_state", "is_current", "content_hash"]

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Edition)
class EditionAdmin(admin.ModelAdmin):
    list_display = ["collection", "scheduled_at", "window_start", "window_end"]
    list_filter = ["collection", "schedule_revision__schedule"]
    readonly_fields = [
        "collection",
        "schedule_revision",
        "scheduled_at",
        "window_start",
        "window_end",
    ]
    inlines = [EditionRevisionInline]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class EditionItemInline(admin.TabularInline):
    model = EditionItem
    extra = 0
    can_delete = False
    fields = ["position", "tier", "card_revision"]
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(EditionRevision)
class EditionRevisionAdmin(admin.ModelAdmin):
    list_display = ["edition", "language", "revision", "editorial_state", "is_current"]
    list_filter = ["language", "editorial_state", "is_current"]
    readonly_fields = [
        "edition",
        "language",
        "revision",
        "editorial_state",
        "is_current",
        "source_ru_revision",
        "item_snapshot",
        "rendered_parts",
        "content_hash",
        "change_note",
    ]
    actions = ["approve_pairs", "publish_pairs"]

    @admin.action(description="Утвердить текущие RU/EN-пары выпусков")
    def approve_pairs(self, request, queryset):
        self._run_pair_action(request, queryset, approve_edition_pair, "Утверждено")

    @admin.action(description="Опубликовать ранее утверждённые RU/EN-пары")
    def publish_pairs(self, request, queryset):
        self._run_pair_action(request, queryset, publish_edition_pair, "Опубликовано")

    def _run_pair_action(self, request, queryset, operation, label):
        processed = set()
        for revision in queryset:
            try:
                russian, _ = operation(revision=revision)
            except ValidationError as error:
                _show_error(self, request, error)
            else:
                processed.add(russian.pk)
        self.message_user(request, f"{label} пар выпусков: {len(processed)}")
    inlines = [EditionItemInline]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(EditorialSelection)
class EditorialSelectionAdmin(admin.ModelAdmin):
    list_display = ["slot", "collection", "event", "status", "weighted_score"]
    list_filter = ["status", "collection", "slot__schedule_revision__schedule"]
    search_fields = ["event__working_title", "rationale"]
    readonly_fields = ["slot", "collection", "event", "weighted_score"]
    actions = ["queue_cards", "queue_editions"]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.action(description="Поставить создание RU/EN-карточек в очередь")
    def queue_cards(self, request, queryset):
        created = 0
        for selection in queryset.filter(status__in=["main", "additional"]).select_related(
            "event", "slot"
        ):
            digest = evidence_hash(selection.event)
            _, was_created = enqueue_job(
                kind="generate_event_cards",
                parameters={"slot_id": selection.slot_id, "event_id": str(selection.event_id)},
                idempotency_key=f"cards:{selection.slot_id}:{selection.event_id}:{digest}",
                max_attempts=1,
            )
            created += int(was_created)
        self.message_user(request, f"Новых заданий карточек: {created}")

    @admin.action(description="Поставить сборку RU/EN-выпусков в очередь")
    def queue_editions(self, request, queryset):
        created = 0
        pairs = {(row.slot_id, row.collection_id) for row in queryset}
        for slot_id, collection_id in pairs:
            selected = EditorialSelection.objects.filter(
                slot_id=slot_id,
                collection_id=collection_id,
                status__in=["main", "additional"],
            ).order_by("event_id")
            card_ids = {
                row.event_id: row.pk
                for row in EventCardRevision.objects.filter(
                    event_id__in=selected.values_list("event_id", flat=True),
                    language="ru",
                    editorial_state="approved",
                    is_current=True,
                )
            }
            signature = hashlib.sha256(
                "|".join(
                    f"{row.event_id}:{row.status}:{row.updated_at.isoformat()}:{card_ids.get(row.event_id, 'missing')}"
                    for row in selected
                ).encode()
            ).hexdigest()
            _, was_created = enqueue_job(
                kind="build_editorial_editions",
                parameters={"slot_id": slot_id, "collection_id": collection_id},
                idempotency_key=f"edition:{slot_id}:{collection_id}:{signature}",
                max_attempts=1,
            )
            created += int(was_created)
        self.message_user(request, f"Новых заданий сборки: {created}")
