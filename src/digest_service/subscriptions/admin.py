from django.contrib import admin

from .models import (
    ErasureRequest,
    ErasureTombstone,
    PreferenceDraft,
    PreferenceRevision,
    SubscriberGeneration,
    SubscriptionCollection,
)


class PreferenceRevisionInline(admin.TabularInline):
    model = PreferenceRevision
    extra = 0
    can_delete = False
    fields = ["revision", "digest_language", "schedule_revision", "effective_from", "is_current"]
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(SubscriberGeneration)
class SubscriberGenerationAdmin(admin.ModelAdmin):
    list_display = [
        "telegram_user_id",
        "generation",
        "digest_language",
        "setup_status",
        "subscription_status",
        "access_status",
    ]
    list_filter = [
        "setup_status",
        "subscription_status",
        "access_status",
        "chat_reachability",
        "privacy_state",
    ]
    search_fields = ["=telegram_user_id", "=telegram_chat_id"]
    readonly_fields = ["id", "generation", "is_current", "automatic_delivery_after"]
    inlines = [PreferenceRevisionInline]


@admin.register(PreferenceRevision)
class PreferenceRevisionAdmin(admin.ModelAdmin):
    list_display = ["subscriber", "revision", "digest_language", "schedule_revision", "is_current"]
    list_filter = ["digest_language", "is_current"]
    readonly_fields = [field.name for field in PreferenceRevision._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(SubscriptionCollection)
class SubscriptionCollectionAdmin(admin.ModelAdmin):
    list_display = ["preference_revision", "collection"]
    list_filter = ["collection"]
    readonly_fields = [field.name for field in SubscriptionCollection._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(PreferenceDraft)
class PreferenceDraftAdmin(admin.ModelAdmin):
    list_display = ["subscriber", "digest_language", "schedule_id", "expires_at"]
    readonly_fields = [field.name for field in PreferenceDraft._meta.fields]

    def has_add_permission(self, request):
        return False


@admin.register(ErasureRequest)
class ErasureRequestAdmin(admin.ModelAdmin):
    list_display = ["id", "subscriber", "status", "accepted_at", "deadline_at", "completed_at"]
    list_filter = ["status"]
    search_fields = ["id", "error_code"]
    readonly_fields = [field.name for field in ErasureRequest._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ErasureTombstone)
class ErasureTombstoneAdmin(admin.ModelAdmin):
    list_display = ["subject_hash", "last_generation", "completed_at", "expires_at"]
    readonly_fields = [field.name for field in ErasureTombstone._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
