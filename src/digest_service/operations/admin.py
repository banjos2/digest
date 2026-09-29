from django.contrib import admin

from .models import (
    BackgroundJob,
    BackupRun,
    DeliverySafetyState,
    OutboxEntry,
    ProcessLease,
    RestoreDrill,
)


@admin.register(BackgroundJob)
class BackgroundJobAdmin(admin.ModelAdmin):
    list_display = ["kind", "status", "attempts", "next_attempt_at", "created_at"]
    list_filter = ["kind", "status"]
    search_fields = ["id", "idempotency_key", "last_error_code"]
    readonly_fields = [field.name for field in BackgroundJob._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(OutboxEntry)
class OutboxEntryAdmin(admin.ModelAdmin):
    list_display = ["job", "status", "attempts", "next_attempt_at"]
    list_filter = ["status"]
    search_fields = ["job__id", "broker_message_id", "last_error_code"]
    readonly_fields = [field.name for field in OutboxEntry._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ProcessLease)
class ProcessLeaseAdmin(admin.ModelAdmin):
    list_display = ["name", "owner", "heartbeat_at", "lease_expires_at"]
    readonly_fields = [field.name for field in ProcessLease._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(BackupRun)
class BackupRunAdmin(admin.ModelAdmin):
    list_display = [
        "created_at",
        "kind",
        "database_backend",
        "storage_class",
        "status",
        "encrypted",
        "size_bytes",
        "expires_at",
    ]
    list_filter = ["kind", "database_backend", "storage_class", "status", "encrypted"]
    search_fields = ["id", "artifact_name", "sha256", "error_code"]
    readonly_fields = [field.name for field in BackupRun._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(RestoreDrill)
class RestoreDrillAdmin(admin.ModelAdmin):
    list_display = ["created_at", "backup", "status", "isolated", "finished_at"]
    list_filter = ["status", "isolated"]
    search_fields = ["id", "backup__id", "error_code"]
    readonly_fields = [field.name for field in RestoreDrill._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(DeliverySafetyState)
class DeliverySafetyStateAdmin(admin.ModelAdmin):
    list_display = ["status", "reason", "applied_at", "updated_at"]
    readonly_fields = [field.name for field in DeliverySafetyState._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
