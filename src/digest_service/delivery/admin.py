from django.contrib import admin

from .models import Delivery, DeliveryAttempt, DeliveryManifest, DeliveryPart


class DeliveryPartInline(admin.TabularInline):
    model = DeliveryPart
    extra = 0
    can_delete = False
    fields = ["part_number", "status", "in_flight_at", "body_hash", "telegram_message_id"]
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Delivery)
class DeliveryAdmin(admin.ModelAdmin):
    list_display = [
        "subscriber_generation",
        "scheduled_at",
        "kind",
        "language",
        "status",
    ]
    list_filter = ["kind", "language", "status"]
    search_fields = ["=subscriber_generation__telegram_user_id", "idempotency_key"]
    readonly_fields = [field.name for field in Delivery._meta.fields]
    inlines = [DeliveryPartInline]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(DeliveryManifest)
class DeliveryManifestAdmin(admin.ModelAdmin):
    list_display = ["delivery", "frozen_at", "content_hash"]
    readonly_fields = [field.name for field in DeliveryManifest._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(DeliveryPart)
class DeliveryPartAdmin(admin.ModelAdmin):
    list_display = ["delivery", "part_number", "status", "in_flight_at", "telegram_message_id"]
    list_filter = ["status"]
    readonly_fields = [field.name for field in DeliveryPart._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(DeliveryAttempt)
class DeliveryAttemptAdmin(admin.ModelAdmin):
    list_display = ["part", "attempt_number", "outcome", "attempted_at"]
    list_filter = ["outcome"]
    readonly_fields = [field.name for field in DeliveryAttempt._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
