from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions


def _keyboard(rows):
    if not rows:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(**button) for button in row] for row in rows]
    )


async def send_bot_reply(bot: Bot, reply):
    if reply.callback_query_id:
        try:
            await bot.answer_callback_query(reply.callback_query_id)
        except (TelegramBadRequest, TelegramAPIError):
            pass
    parameters = {
        "chat_id": reply.chat_id,
        "text": reply.text,
        "parse_mode": reply.parse_mode,
        "reply_markup": _keyboard(reply.keyboard),
        "link_preview_options": LinkPreviewOptions(is_disabled=True),
    }
    if reply.edit_message_id is not None:
        return await bot.edit_message_text(message_id=reply.edit_message_id, **parameters)
    return await bot.send_message(**parameters)


async def send_delivery_part(bot: Bot, part):
    return await bot.send_message(
        chat_id=part.delivery.subscriber_generation.telegram_chat_id,
        text=part.body,
        parse_mode="HTML",
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )
