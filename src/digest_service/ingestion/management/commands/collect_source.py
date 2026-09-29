import asyncio
import os
import socket
import uuid

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from digest_service.catalog.models import Source
from digest_service.ingestion.models import IngestionRun, SourceFetchState
from digest_service.ingestion.service import (
    IngestionError,
    fetch_source,
    fetch_telegram_source,
    store_candidate,
)
from digest_service.operations.service import acquire_process_lease, release_process_lease


class CollectionCommandError(CommandError):
    def __init__(self, code, *, retry_after_seconds=0):
        super().__init__(code)
        self.retry_after_seconds = retry_after_seconds


class Command(BaseCommand):
    help = "Получить новые публикации одного проверенного сайта или Telegram-канала"

    def add_arguments(self, parser):
        parser.add_argument("source_id")
        parser.add_argument("--limit", type=int, default=10)

    def handle(self, *args, **options):
        try:
            source = Source.objects.select_related("retention_policy").get(pk=options["source_id"])
        except Source.DoesNotExist as error:
            raise CommandError("source_not_found") from error
        limit = options["limit"]
        if limit < 1 or limit > settings.SOURCE_COLLECTION_MAX_ITEMS:
            raise CommandError(
                f"limit must be between 1 and {settings.SOURCE_COLLECTION_MAX_ITEMS}"
            )
        lease_owner = ""
        if source.kind == "telegram":
            lease_owner = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4()}"
            if not acquire_process_lease(name="telethon_collector", owner=lease_owner):
                raise CommandError("telethon_collector_busy")
        state, _ = SourceFetchState.objects.get_or_create(source=source)
        started_at = timezone.now()
        run = IngestionRun.objects.create(
            source=source,
            requested_limit=limit,
            started_at=started_at,
        )
        state.last_attempt_at = started_at
        state.save()
        try:
            if source.kind == "telegram":
                try:
                    min_id = int(state.last_external_id or 0)
                except ValueError:
                    min_id = 0
                batch = asyncio.run(fetch_telegram_source(source, limit=limit, min_id=min_id))
            else:
                batch = asyncio.run(
                    fetch_source(
                        source,
                        limit=limit,
                        etag=state.etag,
                        last_modified=state.last_modified,
                    )
                )
            run.discovered_count = len(batch.candidates)
            run.rejected_count = batch.rejected_count
            if batch.http_status == 304:
                run.status = "not_modified"
                state.last_status = "not_modified"
            else:
                for candidate in batch.candidates:
                    _, _, publication_created, version_created = store_candidate(source, candidate)
                    run.created_count += int(publication_created)
                    run.new_version_count += int(version_created)
                    run.unchanged_count += int(not version_created)
                run.status = "success"
                state.last_status = "success"
                state.last_success_at = timezone.now()
                state.etag = batch.etag
                state.last_modified = batch.last_modified
                state.last_external_id = batch.cursor or state.last_external_id
            state.last_http_status = batch.http_status
            state.last_error_code = ""
            state.consecutive_failures = 0
        except Exception as error:
            code = error.code if isinstance(error, IngestionError) else "collection_failed"
            run.status = "failed"
            run.error_code = code
            state.last_status = "failed"
            state.last_error_code = code
            state.consecutive_failures += 1
            raise CollectionCommandError(
                code, retry_after_seconds=getattr(error, "retry_after_seconds", 0)
            ) from error
        finally:
            run.finished_at = timezone.now()
            run.save()
            state.save()
            if lease_owner:
                release_process_lease(name="telethon_collector", owner=lease_owner)
        self.stdout.write(
            self.style.SUCCESS(
                f"{run.status}: found={run.discovered_count}, versions={run.new_version_count}, unchanged={run.unchanged_count}"
            )
        )
