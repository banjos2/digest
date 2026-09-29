import asyncio
import json
import os
import socket
import time
import uuid
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from digest_service.catalog.models import Source
from digest_service.ingestion.management.commands.probe_sources import _pilot_source_ids
from digest_service.ingestion.models import SourceProbeRun
from digest_service.ingestion.service import (
    IngestionError,
    connect_telethon_client,
    create_telethon_client,
    probe_telegram_source,
)
from digest_service.operations.service import acquire_process_lease, release_process_lease


class Command(BaseCommand):
    help = "Проверить доступ выделенного аккаунта к публичным каналам без сохранения текста."

    def add_arguments(self, parser):
        parser.add_argument("--source", action="append", dest="source_ids")
        parser.add_argument("--wave", choices=["A", "B", "all"], default="A")
        parser.add_argument("--output", default="")

    def handle(self, *args, **options):
        source_ids = options["source_ids"] or _pilot_source_ids(options["wave"])
        sources = list(
            Source.objects.select_related("profile")
            .filter(
                pk__in=source_ids,
                kind="telegram",
                preferred_adapter="telethon_public_channel",
            )
            .order_by("id")
        )
        missing = sorted(set(source_ids) - {source.pk for source in sources})
        if not sources:
            raise CommandError("no_telethon_sources_selected")
        lease_owner = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4()}"
        if not acquire_process_lease(name="telethon_collector", owner=lease_owner):
            raise CommandError("telethon_collector_busy")
        try:
            try:
                results = asyncio.run(self._probe(sources))
            except IngestionError as error:
                raise CommandError(error.code) from error
        finally:
            release_process_lease(name="telethon_collector", owner=lease_owner)
        for source, result in zip(sources, results, strict=True):
            SourceProbeRun.objects.create(source=source, **result)
        report = {
            "schema_version": 1,
            "checked_at": timezone.now().isoformat(),
            "wave": options["wave"],
            "requested_source_ids": source_ids,
            "skipped_non_telegram_ids": missing,
            "results": [
                {
                    "source_id": source.pk,
                    "name": source.profile.name,
                    **{
                        key: value.isoformat() if hasattr(value, "isoformat") else value
                        for key, value in result.items()
                    },
                }
                for source, result in zip(sources, results, strict=True)
            ],
        }
        if options["output"]:
            output = Path(options["output"]).resolve()
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        succeeded = sum(item["status"] == "success" for item in results)
        self.stdout.write(
            self.style.SUCCESS(
                f"probed={len(results)} succeeded={succeeded} "
                f"failed={len(results) - succeeded} skipped={len(missing)}"
            )
        )

    async def _probe(self, sources):
        client = create_telethon_client()
        await connect_telethon_client(client)
        try:
            results = []
            for source in sources:
                started = timezone.now()
                clock = time.monotonic()
                values = {"status": "failed", "started_at": started, "finished_at": started}
                try:
                    details = await probe_telegram_source(source, client=client)
                except IngestionError as error:
                    values["error_code"] = error.code
                except Exception:
                    values["error_code"] = "telethon_probe_failed"
                else:
                    values.update(details)
                    values["status"] = "success"
                values["latency_ms"] = max(1, int((time.monotonic() - clock) * 1000))
                values["finished_at"] = timezone.now()
                results.append(values)
            return results
        finally:
            if client.is_connected():
                await client.disconnect()
