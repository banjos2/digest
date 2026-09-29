from django.contrib import admin

from .models import (
    IngestionRun,
    Publication,
    PublicationVersion,
    SourceFetchState,
    SourceProbeRun,
)


class PublicationVersionInline(admin.TabularInline):
    model = PublicationVersion
    extra = 0
    can_delete = False
    fields = ["number", "language", "content_scope", "fetched_at", "expires_at", "content_hash"]
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Publication)
class PublicationAdmin(admin.ModelAdmin):
    list_display = ["title", "source", "published_at", "last_seen_at"]
    list_filter = ["source__kind", "source__input_language"]
    search_fields = ["title", "canonical_url", "external_id", "source__profile__name"]
    readonly_fields = [
        "source",
        "external_id",
        "canonical_url",
        "title",
        "published_at",
        "first_seen_at",
        "last_seen_at",
    ]
    inlines = [PublicationVersionInline]
    list_select_related = ["source", "source__profile"]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(PublicationVersion)
class PublicationVersionAdmin(admin.ModelAdmin):
    list_display = [
        "publication",
        "number",
        "language",
        "content_scope",
        "fetched_at",
        "expires_at",
        "purged_at",
    ]
    list_filter = ["language", "content_scope"]
    search_fields = ["publication__title", "publication__canonical_url", "content_hash"]
    readonly_fields = [
        "publication",
        "number",
        "content_hash",
        "title",
        "body",
        "language",
        "content_scope",
        "fetched_at",
        "expires_at",
        "purged_at",
        "purge_reason",
        "extraction_metadata",
    ]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(SourceFetchState)
class SourceFetchStateAdmin(admin.ModelAdmin):
    list_display = [
        "source",
        "source_kind",
        "last_status",
        "last_external_id",
        "last_success_at",
        "consecutive_failures",
    ]
    list_filter = ["last_status", "source__kind"]
    readonly_fields = [field.name for field in SourceFetchState._meta.fields]

    def has_add_permission(self, request):
        return False

    @admin.display(description="Тип", ordering="source__kind")
    def source_kind(self, obj):
        return obj.source.get_kind_display()


@admin.register(IngestionRun)
class IngestionRunAdmin(admin.ModelAdmin):
    list_display = [
        "source",
        "source_kind",
        "status",
        "started_at",
        "discovered_count",
        "new_version_count",
        "rejected_count",
    ]
    list_filter = ["status", "source__kind"]
    readonly_fields = [field.name for field in IngestionRun._meta.fields]

    def has_add_permission(self, request):
        return False

    @admin.display(description="Тип", ordering="source__kind")
    def source_kind(self, obj):
        return obj.source.get_kind_display()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(SourceProbeRun)
class SourceProbeRunAdmin(admin.ModelAdmin):
    list_display = [
        "source",
        "source_kind",
        "status",
        "http_status",
        "item_count",
        "dated_item_count",
        "article_success_count",
        "latency_ms",
        "started_at",
    ]
    list_filter = ["status", "source__kind", "http_status", "content_type"]
    search_fields = ["source__id", "source__profile__name", "error_code", "final_url"]
    readonly_fields = [field.name for field in SourceProbeRun._meta.fields]

    def has_add_permission(self, request):
        return False

    @admin.display(description="Тип", ordering="source__kind")
    def source_kind(self, obj):
        return obj.source.get_kind_display()

    def has_delete_permission(self, request, obj=None):
        return False
