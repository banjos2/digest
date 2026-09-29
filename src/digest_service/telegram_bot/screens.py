import html

from digest_service.catalog.models import Collection
from digest_service.scheduling.models import Schedule


def button(text, callback_data):
    return {"text": text, "callback_data": callback_data}


def _draft_tag(draft):
    return draft.token.hex[:8]


def language_screen():
    return (
        "Выберите язык интерфейса и дайджеста.\nChoose your interface and digest language.",
        [[button("Русский", "lang:ru"), button("English", "lang:en")]],
    )


def collections_screen(subscriber, draft=None):
    draft = draft or subscriber.preference_draft
    tag = _draft_tag(draft)
    language = draft.interface_language
    rows = []
    collections = (
        Collection.objects.filter(is_active=True, release_target="v1")
        .exclude(kind="video_digest")
        .order_by("sort_order", "id")
    )
    for collection in collections:
        name = collection.name_ru if language == "ru" else collection.name_en
        is_selected = collection.pk in draft.collection_ids
        mark = "✅" if is_selected else "▫️"
        target = "remove" if is_selected else "select"
        rows.append([button(f"{mark} {name}", f"col:{tag}:{collection.pk}:{target}")])
    done = "Готово" if language == "ru" else "Done"
    cancel = "Отмена" if language == "ru" else "Cancel"
    rows.append([button(done, f"cols:{tag}:done"), button(cancel, f"prefs:{tag}:cancel")])
    text = "Выберите подборки:" if language == "ru" else "Choose your sections:"
    return text, rows


def schedules_screen(subscriber):
    draft = subscriber.preference_draft
    language = draft.interface_language
    tag = _draft_tag(draft)
    rows = []
    schedules = Schedule.objects.filter(visible_to_new_subscribers=True).order_by(
        "sort_order", "id"
    )
    for schedule in schedules:
        name = schedule.name_ru if language == "ru" else schedule.name_en
        rows.append([button(name, f"sched:{tag}:{schedule.pk}")])
    cancel = "Отмена" if language == "ru" else "Cancel"
    rows.append([button(cancel, f"prefs:{tag}:cancel")])
    text = "Выберите частоту:" if language == "ru" else "Choose a schedule:"
    return text, rows


def preference_summary(subscriber, draft=None):
    draft = draft or subscriber.preference_draft
    language = draft.interface_language
    collections = list(
        Collection.objects.filter(pk__in=draft.collection_ids).order_by("sort_order", "id")
    )
    schedule = Schedule.objects.get(pk=draft.schedule_id)
    names = [item.name_ru if language == "ru" else item.name_en for item in collections]
    schedule_name = schedule.name_ru if language == "ru" else schedule.name_en
    tag = _draft_tag(draft)
    if language == "ru":
        text = (
            f"<b>Проверьте настройки</b>\n"
            f"Язык: Русский\n"
            f"Подборки: {html.escape(', '.join(names))}\n"
            f"Частота: {html.escape(schedule_name)}"
        )
        keyboard = [
            [button("Получать дайджесты", f"prefs:{tag}:save")],
            [button("Отмена", f"prefs:{tag}:cancel")],
        ]
    else:
        text = (
            f"<b>Check your settings</b>\n"
            f"Language: English\n"
            f"Sections: {html.escape(', '.join(names))}\n"
            f"Schedule: {html.escape(schedule_name)}"
        )
        keyboard = [
            [button("Start digests", f"prefs:{tag}:save")],
            [button("Cancel", f"prefs:{tag}:cancel")],
        ]
    return text, keyboard


def current_settings_screen(subscriber):
    preference = subscriber.current_preferences
    language = subscriber.interface_language
    names = [
        item.collection.name_ru if language == "ru" else item.collection.name_en
        for item in preference.selected_collections.select_related("collection").order_by(
            "collection__sort_order", "collection_id"
        )
    ]
    schedule = preference.schedule_revision.schedule
    schedule_name = schedule.name_ru if language == "ru" else schedule.name_en
    if language == "ru":
        state = "на паузе" if subscriber.subscription_status == "paused" else "активна"
        text = (
            f"<b>Ваши настройки</b>\nПодписка: {state}\n"
            f"Подборки: {html.escape(', '.join(names))}\nЧастота: {html.escape(schedule_name)}"
        )
        keyboard = [
            [button("Изменить", "prefs:edit"), button("Последний выпуск", "digest:latest")],
            [button("Пауза", "subscription:pause")],
            [button("Мои данные", "data:menu")],
        ]
    else:
        state = "paused" if subscriber.subscription_status == "paused" else "active"
        text = (
            f"<b>Your settings</b>\nSubscription: {state}\n"
            f"Sections: {html.escape(', '.join(names))}\nSchedule: {html.escape(schedule_name)}"
        )
        keyboard = [
            [button("Change", "prefs:edit"), button("Latest digest", "digest:latest")],
            [button("Pause", "subscription:pause")],
            [button("My data", "data:menu")],
        ]
    return text, keyboard


def erasure_confirmation_screen(subscriber, request):
    token = str(request.confirmation_token)
    if subscriber.interface_language == "ru":
        text = (
            "<b>Удалить мои данные?</b>\n"
            "Подписка и ожидающие отправки будут остановлены сразу. Рабочие данные будут "
            "удалены в течение 24 часов. После подтверждения отменить запрос нельзя. "
            "Сообщения, уже находящиеся в Telegram, сервис удалить не может."
        )
        labels = ("Подтвердить удаление", "Отмена")
    else:
        text = (
            "<b>Delete my data?</b>\n"
            "Your subscription and queued deliveries will stop immediately. Working data will "
            "be deleted within 24 hours. The request cannot be cancelled after confirmation. "
            "The service cannot delete messages already stored by Telegram."
        )
        labels = ("Confirm deletion", "Cancel")
    return text, [
        [button(labels[0], f"erase:{token}:confirm")],
        [button(labels[1], f"erase:{token}:cancel")],
    ]
