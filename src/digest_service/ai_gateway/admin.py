from django.contrib import admin

from .models import AIBudgetPeriod, AIRequest


@admin.register(AIBudgetPeriod)
class AIBudgetPeriodAdmin(admin.ModelAdmin):
    list_display = ["period_start", "limit_usd", "reserved_usd", "spent_usd", "available"]
    readonly_fields = ["reserved_usd", "spent_usd", "created_at", "updated_at"]

    @admin.display(description="Доступно")
    def available(self, obj):
        return obj.available_usd


@admin.register(AIRequest)
class AIRequestAdmin(admin.ModelAdmin):
    list_display = [
        "operation",
        "provider",
        "model",
        "status",
        "actual_cost_usd",
        "started_at",
    ]
    list_filter = ["operation", "provider", "model", "status"]
    search_fields = ["input_hash", "provider_request_id", "error_code"]
    readonly_fields = [field.name for field in AIRequest._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
