from django.contrib import admin

from .models import BotReply, TelegramUpdateReceipt


@admin.register(TelegramUpdateReceipt)
class TelegramUpdateReceiptAdmin(admin.ModelAdmin):
    list_display = ["update_id", "update_kind", "status", "created_at", "finished_at"]
    list_filter = ["update_kind", "status"]
    search_fields = ["=update_id", "payload_hash"]
    readonly_fields = [field.name for field in TelegramUpdateReceipt._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(BotReply)
class BotReplyAdmin(admin.ModelAdmin):
    list_display = [
        "chat_id",
        "receipt",
        "status",
        "attempt_count",
        "in_flight_at",
        "created_at",
    ]
    list_filter = ["status"]
    search_fields = ["=chat_id", "=receipt__update_id"]
    readonly_fields = [field.name for field in BotReply._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
