import asyncio

from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from asgiref.sync import sync_to_async
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q
from django.utils import timezone

from digest_service.delivery.models import Delivery
from digest_service.delivery.service import claim_next_part, record_part_result
from digest_service.operations.guard_service import delivery_guard_ready
from digest_service.subscriptions.models import SubscriberGeneration
from digest_service.telegram_bot.queue_service import claim_next_reply, record_reply_result
from digest_service.telegram_bot.sender import send_bot_reply, send_delivery_part


def _next_delivery():
    return (
        Delivery.objects.filter(status__in=["pending", "sending"])
        .filter(
            Q(parts__status="pending")
            | Q(parts__status="retry_wait", parts__next_attempt_at__lte=timezone.now())
        )
        .order_by("scheduled_at", "created_at")
        .distinct()
        .first()
    )


def _mark_delivery_chat(delivery, state):
    SubscriberGeneration.objects.filter(pk=delivery.subscriber_generation_id).update(
        chat_reachability=state
    )


class Command(BaseCommand):
    help = "Отправить ограниченное число подготовленных ответов и частей через Telegram"

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=100)

    def handle(self, *args, **options):
        if not settings.TELEGRAM_BOT_TOKEN:
            raise CommandError("TELEGRAM_BOT_TOKEN is not configured")
        if not delivery_guard_ready():
            raise CommandError("delivery is blocked until the current erasure guard is applied")
        if not 1 <= options["limit"] <= 1000:
            raise CommandError("limit must be between 1 and 1000")
        sent = asyncio.run(self._run(limit=options["limit"]))
        self.stdout.write(self.style.SUCCESS(f"processed={sent}"))

    async def _run(self, *, limit):
        bot = Bot(token=settings.TELEGRAM_BOT_TOKEN)
        try:
            return await self._run_with_bot(bot=bot, limit=limit)
        finally:
            await bot.session.close()

    async def _run_with_bot(self, *, bot, limit):
        processed = 0
        while processed < limit:
            ready = await sync_to_async(delivery_guard_ready, thread_sensitive=True)()
            if not ready:
                break
            reply = await sync_to_async(claim_next_reply, thread_sensitive=True)()
            if reply:
                await self._send_reply(bot, reply)
                processed += 1
                continue
            delivery = await sync_to_async(_next_delivery, thread_sensitive=True)()
            if not delivery:
                break
            part = await sync_to_async(claim_next_part, thread_sensitive=True)(
                delivery=delivery
            )
            if not part:
                break
            await self._send_part(bot, part)
            processed += 1
        return processed

    async def _send_reply(self, bot, reply):
        try:
            message = await send_bot_reply(bot, reply)
        except TelegramRetryAfter as error:
            await sync_to_async(record_reply_result, thread_sensitive=True)(
                reply=reply,
                outcome="temporary_failure",
                error_code="retry_after",
                retry_after_seconds=error.retry_after,
            )
        except TelegramForbiddenError:
            await sync_to_async(record_reply_result, thread_sensitive=True)(
                reply=reply, outcome="permanent_failure", error_code="forbidden"
            )
        except TelegramBadRequest:
            await sync_to_async(record_reply_result, thread_sensitive=True)(
                reply=reply, outcome="permanent_failure", error_code="bad_request"
            )
        except (TelegramNetworkError, TelegramServerError):
            await sync_to_async(record_reply_result, thread_sensitive=True)(
                reply=reply, outcome="unknown", error_code="network_or_server"
            )
        else:
            await sync_to_async(record_reply_result, thread_sensitive=True)(
                reply=reply,
                outcome="sent",
                telegram_message_id=str(message.message_id),
            )

    async def _send_part(self, bot, part):
        try:
            message = await send_delivery_part(bot, part)
        except TelegramRetryAfter as error:
            await sync_to_async(record_part_result, thread_sensitive=True)(
                part=part,
                outcome="temporary_failure",
                error_code="retry_after",
                retry_after_seconds=error.retry_after,
            )
        except TelegramForbiddenError:
            await sync_to_async(record_part_result, thread_sensitive=True)(
                part=part, outcome="permanent_failure", error_code="forbidden"
            )
            await sync_to_async(_mark_delivery_chat, thread_sensitive=True)(
                part.delivery, "unreachable"
            )
        except TelegramBadRequest:
            await sync_to_async(record_part_result, thread_sensitive=True)(
                part=part, outcome="permanent_failure", error_code="bad_request"
            )
        except (TelegramNetworkError, TelegramServerError):
            await sync_to_async(record_part_result, thread_sensitive=True)(
                part=part, outcome="unknown", error_code="network_or_server"
            )
        else:
            await sync_to_async(record_part_result, thread_sensitive=True)(
                part=part,
                outcome="sent",
                telegram_message_id=str(message.message_id),
            )
            await sync_to_async(_mark_delivery_chat, thread_sensitive=True)(
                part.delivery, "reachable"
            )
