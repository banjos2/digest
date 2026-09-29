from django import forms
from django.contrib import admin, messages
from django.core.exceptions import ValidationError
from django.http import HttpResponseRedirect
from django.utils.html import format_html_join

from digest_service.core.admin import StableIdAdmin

from .models import Schedule, ScheduleRevision, ScheduleSlot


class ScheduleForm(forms.ModelForm):
    times_text = forms.CharField(label="Время отправки", help_text="Например: 10:00, 19:00")
    weekdays = forms.MultipleChoiceField(
        label="Дни недели",
        required=False,
        widget=forms.CheckboxSelectMultiple,
        choices=[
            (str(i), day) for i, day in enumerate(["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"], 1)
        ],
    )
    expected_revision = forms.IntegerField(widget=forms.HiddenInput, required=False)

    class Meta:
        model = Schedule
        exclude = ["local_times", "iso_weekdays"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.initial["times_text"] = ", ".join(self.instance.local_times)
            self.initial["weekdays"] = [str(x) for x in self.instance.iso_weekdays]
            self.initial["expected_revision"] = self.instance.revision

    def clean(self):
        cleaned = super().clean()
        self.instance.local_times = [
            value.strip() for value in cleaned.get("times_text", "").split(",") if value.strip()
        ]
        self.instance.iso_weekdays = [int(x) for x in cleaned.get("weekdays", [])]
        if (
            not self.instance._state.adding
            and cleaned.get("expected_revision") != self.instance.revision
        ):
            raise forms.ValidationError(
                "Расписание изменилось. Откройте форму заново, чтобы не потерять правки."
            )
        return cleaned

    def _update_errors(self, errors):
        if hasattr(errors, "error_dict"):
            aliases = {"local_times": "times_text", "iso_weekdays": "weekdays"}
            errors = forms.ValidationError(
                {aliases.get(key, key): value for key, value in errors.error_dict.items()}
            )
        super()._update_errors(errors)


@admin.register(Schedule)
class ScheduleAdmin(StableIdAdmin):
    form = ScheduleForm
    list_display = [
        "name_ru",
        "timezone",
        "local_times",
        "recurrence_kind",
        "revision",
        "delivery_enabled",
    ]
    readonly_fields = ["revision", "next_occurrences"]
    list_filter = ["recurrence_kind", "delivery_enabled"]
    search_fields = ["id", "name_ru", "name_en"]

    def changeform_view(self, request, object_id=None, form_url="", extra_context=None):
        try:
            return super().changeform_view(request, object_id, form_url, extra_context)
        except ValidationError as exc:
            # A concurrent save can occur after form validation but before the row lock.
            self.message_user(request, " ".join(exc.messages), level=messages.ERROR)
            return HttpResponseRedirect(request.path)

    @admin.display(description="Ближайшие 5 запусков сохранённого правила")
    def next_occurrences(self, obj):
        if not obj or obj._state.adding:
            return "Сохраните расписание для предпросмотра. Рассылка при этом не запускается."
        return format_html_join(
            "",
            "<div>{} · окно {} — {}</div>",
            (
                (
                    row["local_time"].strftime("%d.%m.%Y %H:%M %Z"),
                    row["window_start"]
                    .astimezone(row["local_time"].tzinfo)
                    .strftime("%d.%m %H:%M"),
                    row["window_end"].astimezone(row["local_time"].tzinfo).strftime("%d.%m %H:%M"),
                )
                for row in obj.preview()
            ),
        )


@admin.register(ScheduleRevision)
class ScheduleRevisionAdmin(admin.ModelAdmin):
    list_display = ["schedule", "number", "created_at"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ScheduleSlot)
class ScheduleSlotAdmin(admin.ModelAdmin):
    list_display = ["schedule_revision", "scheduled_at", "state", "delivery_deadline"]
    list_filter = ["state", "schedule_revision__schedule"]
    search_fields = ["schedule_revision__schedule__name_ru"]
    readonly_fields = [
        "schedule_revision",
        "scheduled_at",
        "window_start",
        "window_end",
        "preparation_at",
        "delivery_deadline",
        "state",
        "preparation_summary",
        "created_at",
        "updated_at",
    ]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
