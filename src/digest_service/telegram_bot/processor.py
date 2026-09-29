import hashlib
import json
import uuid

from aiogram.types import Update
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from digest_service.delivery.service import prepare_latest_manual_delivery
from digest_service.subscriptions.erasure import (
    cancel_erasure_confirmation,
    confirm_profile_erasure,
    request_profile_erasure,
)
from digest_service.subscriptions.models import PreferenceDraft
from digest_service.subscriptions.service import (
    begin_settings_edit,
    cancel_preference_draft,
    choose_draft_schedule,
    choose_language,
    finish_collection_selection,
    pause_subscriber,
    save_preference_draft,
    set_draft_collection,
    start_subscriber,
)

from .models import BotReply, TelegramUpdateReceipt
from .screens import (
    collections_screen,
    current_settings_screen,
    erasure_confirmation_screen,
    language_screen,
    preference_summary,
    schedules_screen,
)


def _canonical_hash(payload):
    value = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(value.encode()).hexdigest()


def _command(text):
    first = text.strip().split(maxsplit=1)[0].lower()
    return first.split("@", 1)[0]


def _localized(subscriber, ru, en):
    return ru if subscriber.interface_language == "ru" else en


def _tagged_value(subscriber, data, prefix, action=None):
    parts = data.split(":", 2)
    if len(parts) != 3 or parts[0] != prefix:
        raise ValidationError("Некорректная кнопка.")
    try:
        draft = subscriber.preference_draft
    except PreferenceDraft.DoesNotExist as error:
        raise ValidationError("Черновик настроек не найден.") from error
    if parts[1] != draft.token.hex[:8] or (action is not None and parts[2] != action):
        raise ValidationError("Эта кнопка устарела. Откройте настройки заново.")
    return parts[2]


def _collection_action(subscriber, data):
    parts = data.split(":", 3)
    if len(parts) != 4 or parts[0] != "col" or parts[3] not in {"select", "remove"}:
        raise ValidationError("Некорректная кнопка подборки.")
    try:
        draft = subscriber.preference_draft
    except PreferenceDraft.DoesNotExist as error:
        raise ValidationError("Черновик настроек не найден.") from error
    if parts[1] != draft.token.hex[:8]:
        raise ValidationError("Эта кнопка устарела. Откройте настройки заново.")
    return parts[2], parts[3] == "select"


def _route_message(subscriber, text, update_id):
    command = _command(text)
    if command == "/start":
        if subscriber.privacy_state == "erasure_pending":
            return _localized(
                subscriber,
                "Удаление данных уже принято и выполняется.",
                "Your data deletion request is already being processed.",
            ), []
        if subscriber.setup_status == "incomplete":
            return language_screen()
        return current_settings_screen(subscriber)
    if command == "/stop":
        pause_subscriber(subscriber=subscriber)
        return _localized(
            subscriber,
            "Рассылка приостановлена. Настройки сохранены; /start возобновит её.",
            "Digests are paused. Your settings are saved; /start will resume them.",
        ), []
    if command == "/settings":
        begin_settings_edit(subscriber=subscriber)
        return collections_screen(subscriber)
    if command == "/digest":
        delivery = prepare_latest_manual_delivery(
            subscriber=subscriber, request_key=f"update:{update_id}"
        )
        return _localized(
            subscriber,
            f"Готовлю последний выпуск: {delivery.parts.count()} сообщ.",
            f"Preparing the latest digest: {delivery.parts.count()} message(s).",
        ), []
    if command in ["/data", "/delete_my_data"]:
        request = request_profile_erasure(subscriber=subscriber)
        return erasure_confirmation_screen(subscriber, request)
    if command == "/cancel":
        if subscriber.dialog_state == "confirm_data_erasure":
            cancel_erasure_confirmation(subscriber=subscriber)
        else:
            cancel_preference_draft(subscriber=subscriber)
        return _localized(subscriber, "Изменения отменены.", "Changes cancelled."), []
    return _localized(
        subscriber,
        "Команды: /start, /settings, /digest, /stop, /data, /cancel, /help.",
        "Commands: /start, /settings, /digest, /stop, /data, /cancel, /help.",
    ), []


def _route_callback(subscriber, data, update_id):
    if data.startswith("lang:"):
        choose_language(subscriber=subscriber, language=data.removeprefix("lang:"))
        return collections_screen(subscriber)
    if data.startswith("col:"):
        collection_id, selected = _collection_action(subscriber, data)
        draft = set_draft_collection(
            subscriber=subscriber, collection_id=collection_id, selected=selected
        )
        return collections_screen(subscriber, draft=draft)
    if data.startswith("cols:"):
        _tagged_value(subscriber, data, "cols", "done")
        finish_collection_selection(subscriber=subscriber)
        return schedules_screen(subscriber)
    if data.startswith("sched:"):
        schedule_id = _tagged_value(subscriber, data, "sched")
        draft = choose_draft_schedule(subscriber=subscriber, schedule_id=schedule_id)
        return preference_summary(subscriber, draft)
    if data.startswith("prefs:") and data.endswith(":save"):
        _tagged_value(subscriber, data, "prefs", "save")
        save_preference_draft(subscriber=subscriber)
        return _localized(
            subscriber,
            "Готово. Подписка включена; первый выпуск придёт в следующий будущий слот.",
            "All set. Your first digest will arrive at the next future slot.",
        ), []
    if data.startswith("prefs:") and data.endswith(":cancel"):
        _tagged_value(subscriber, data, "prefs", "cancel")
        cancel_preference_draft(subscriber=subscriber)
        return _localized(subscriber, "Изменения отменены.", "Changes cancelled."), []
    if data == "prefs:edit":
        begin_settings_edit(subscriber=subscriber)
        return collections_screen(subscriber)
    if data == "subscription:pause":
        pause_subscriber(subscriber=subscriber)
        return _localized(subscriber, "Рассылка приостановлена.", "Digests are paused."), []
    if data == "digest:latest":
        delivery = prepare_latest_manual_delivery(
            subscriber=subscriber, request_key=f"update:{update_id}"
        )
        return _localized(
            subscriber,
            f"Готовлю последний выпуск: {delivery.parts.count()} сообщ.",
            f"Preparing the latest digest: {delivery.parts.count()} message(s).",
        ), []
    if data == "data:menu":
        request = request_profile_erasure(subscriber=subscriber)
        return erasure_confirmation_screen(subscriber, request)
    if data.startswith("erase:"):
        parts = data.split(":", 2)
        if len(parts) != 3:
            raise ValidationError("Некорректная кнопка удаления.")
        try:
            token = uuid.UUID(parts[1])
        except ValueError as error:
            raise ValidationError("Некорректная кнопка удаления.") from error
        if parts[2] == "cancel":
            cancel_erasure_confirmation(subscriber=subscriber)
            return _localized(subscriber, "Удаление отменено.", "Deletion cancelled."), []
        if parts[2] == "confirm":
            confirm_profile_erasure(subscriber=subscriber, token=token)
            return _localized(
                subscriber,
                "Запрос принят. Рассылка остановлена; данные будут удалены в течение 24 часов.",
                "Request accepted. Digests have stopped; your data will be deleted within 24 hours.",
            ), []
        raise ValidationError("Некорректная кнопка удаления.")
    raise ValidationError("Эта кнопка устарела. Откройте настройки заново.")


@transaction.atomic
def process_update_payload(payload):
    payload_hash = _canonical_hash(payload)
    update_id = payload.get("update_id")
    if type(update_id) is not int:
        raise ValidationError("Telegram update_id отсутствует или некорректен.")
    receipt = TelegramUpdateReceipt.objects.select_for_update().filter(update_id=update_id).first()
    if receipt:
        if receipt.payload_hash != payload_hash:
            raise ValidationError("Один update_id получен с другим содержимым.")
        return getattr(receipt, "reply", None), False
    receipt = TelegramUpdateReceipt.objects.create(update_id=update_id, payload_hash=payload_hash)
    update = Update.model_validate(payload)
    callback = update.callback_query
    message = update.message
    if callback and callback.message:
        chat = callback.message.chat
        user = callback.from_user
        data = callback.data or ""
        kind = "callback_query"
    elif message and message.from_user and message.text:
        chat = message.chat
        user = message.from_user
        data = message.text
        kind = "message"
    else:
        receipt.update_kind = "unsupported"
        receipt.status = "ignored"
        receipt.finished_at = timezone.now()
        receipt.save()
        return None, True
    if str(chat.type) != "private" or user.is_bot or user.id <= 0 or chat.id <= 0:
        receipt.update_kind = kind
        receipt.status = "ignored"
        receipt.finished_at = timezone.now()
        receipt.save()
        return None, True
    subscriber, _ = start_subscriber(telegram_user_id=user.id, telegram_chat_id=chat.id)
    try:
        if kind == "callback_query":
            text, keyboard = _route_callback(subscriber, data, update_id)
        else:
            text, keyboard = _route_message(subscriber, data, update_id)
    except ValidationError as error:
        text = _localized(
            subscriber,
            "; ".join(error.messages),
            "This button is out of date or unavailable. Open settings and try again.",
        )
        keyboard = []
    reply = BotReply.objects.create(
        receipt=receipt,
        chat_id=chat.id,
        callback_query_id=callback.id if callback else "",
        edit_message_id=callback.message.message_id if callback and callback.message else None,
        text=text,
        keyboard=keyboard,
    )
    receipt.update_kind = kind
    receipt.status = "processed"
    receipt.finished_at = timezone.now()
    receipt.save()
    return reply, True
