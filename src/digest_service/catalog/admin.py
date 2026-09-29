from django import forms
from django.contrib import admin, messages
from django.core.exceptions import ValidationError

from digest_service.core.admin import StableIdAdmin

from .models import (
    Collection,
    OriginGroup,
    PublisherProfile,
    RetentionPolicy,
    Source,
    SourceCollection,
)


class SourceCollectionInline(admin.TabularInline):
    model = SourceCollection
    extra = 0
    autocomplete_fields = ["source"]

    def get_formset(self, request, obj=None, **kwargs):
        formset = super().get_formset(request, obj, **kwargs)
        formset.form.base_fields["is_active"].initial = True
        return formset


class SourceCollectionForSourceInline(admin.TabularInline):
    model = SourceCollection
    extra = 1
    autocomplete_fields = ["collection"]

    def get_formset(self, request, obj=None, **kwargs):
        formset = super().get_formset(request, obj, **kwargs)
        formset.form.base_fields["is_active"].initial = True
        return formset


class CollectionForm(forms.ModelForm):
    routing_keywords_text = forms.CharField(
        label="Ключевые слова маршрутизации",
        required=False,
        widget=forms.Textarea(attrs={"rows": 8}),
        help_text="По одному слову или фразе на строку.",
    )

    class Meta:
        model = Collection
        exclude = ["routing_keywords", "sources"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.initial["routing_keywords_text"] = "\n".join(
                self.instance.routing_keywords
            )

    def clean(self):
        cleaned = super().clean()
        self.instance.routing_keywords = [
            value.strip()
            for value in cleaned.get("routing_keywords_text", "").splitlines()
            if value.strip()
        ]
        return cleaned


@admin.register(Collection)
class CollectionAdmin(StableIdAdmin):
    form = CollectionForm
    list_display = ["name_ru", "name_en", "kind", "release_target", "is_active", "sort_order"]
    list_filter = ["release_target", "is_active", "kind"]
    search_fields = ["id", "name_ru", "name_en"]
    inlines = [SourceCollectionInline]


@admin.register(Source)
class SourceAdmin(StableIdAdmin):
    list_display = [
        "profile",
        "kind",
        "input_language",
        "is_active",
        "access_review_status",
        "collection_check_status",
    ]
    list_filter = [
        "kind",
        "input_language",
        "is_active",
        "access_review_status",
        "collection_check_status",
    ]
    search_fields = ["id", "url", "profile__name"]
    autocomplete_fields = ["profile", "origin_group", "retention_policy"]
    readonly_fields = ["research_metadata", "technical_status"]
    list_select_related = ["profile", "origin_group", "retention_policy"]
    inlines = [SourceCollectionForSourceInline]
    actions = ["activate_selected", "deactivate_selected"]

    @admin.action(description="Включить выбранные источники без технической проверки")
    def activate_selected(self, request, queryset):
        activated = 0
        for source in queryset:
            source.access_review_status = "approved"
            source.collection_check_status = "verified"
            source.access_review_note = source.access_review_note or (
                "Источник включён администратором без предварительной технической проверки."
            )
            if source.kind == "telegram":
                source.preferred_adapter = "telethon_public_channel"
            else:
                source.preferred_adapter = "rss_then_article_extraction"
                source.preferred_feed_url = source.preferred_feed_url or source.url
            source.is_active = True
            try:
                source.full_clean()
                source.save()
            except ValidationError as error:
                self.message_user(request, " ".join(error.messages), level=messages.ERROR)
            else:
                activated += 1
        self.message_user(request, f"Включено источников: {activated}")

    @admin.action(description="Исключить выбранные источники из сбора")
    def deactivate_selected(self, request, queryset):
        updated = queryset.update(is_active=False)
        self.message_user(request, f"Исключено источников: {updated}")


@admin.register(PublisherProfile)
class PublisherProfileAdmin(StableIdAdmin):
    list_display = ["name", "role", "default_language", "origin_group"]
    search_fields = ["name", "id"]
    autocomplete_fields = ["origin_group"]
    readonly_fields = ["research_metadata"]


@admin.register(OriginGroup)
class OriginGroupAdmin(StableIdAdmin):
    search_fields = ["name", "id"]


@admin.register(RetentionPolicy)
class RetentionPolicyAdmin(StableIdAdmin):
    search_fields = ["name", "id"]
    readonly_fields = ["definition"]

    def has_add_permission(self, request):
        return False


@admin.register(SourceCollection)
class SourceCollectionAdmin(admin.ModelAdmin):
    list_display = ["collection", "source", "priority", "is_active"]
    list_filter = ["collection", "priority", "is_active"]
    search_fields = ["source__profile__name", "collection__name_ru"]
    autocomplete_fields = ["source", "collection"]
    list_select_related = ["source__profile", "collection"]
