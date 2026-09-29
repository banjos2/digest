from django.contrib import admin

from .models import SeedSnapshot


class StableIdAdmin(admin.ModelAdmin):
    def get_readonly_fields(self, request, obj=None):
        return tuple(super().get_readonly_fields(request, obj)) + (("id",) if obj else ())


@admin.register(SeedSnapshot)
class SeedSnapshotAdmin(admin.ModelAdmin):
    list_display = ["filename", "sha256", "created_at"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
