import asyncio
import json
import time
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from digest_service.catalog.models import Source
from digest_service.ingestion.models import SourceProbeRun
from digest_service.ingestion.service import IngestionError, probe_source_feed


def _pilot_source_ids(wave):
    path = settings.BASE_DIR / "config" / "pilot_sources.seed.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    ids = []
    for group in payload["collection_groups"]:
        if wave == "all" or group["wave"] == wave:
            ids.extend(group["source_ids"])
    return list(dict.fromkeys(ids))


class Command(BaseCommand):
    help = "Проверить RSS без активации источников и без сохранения публикаций."

    def add_arguments(self, parser):
        parser.add_argument("--source", action="append", dest="source_ids")
        parser.add_argument("--wave", choices=["A", "B", "all"], default="A")
        parser.add_argument("--limit", type=int, default=3)
        parser.add_argument("--article-limit", type=int, default=0)
        parser.add_argument("--concurrency", type=int, default=4)
        parser.add_argument("--output", default="")

    def handle(self, *args, **options):
        if not 1 <= options["limit"] <= 10:
            raise CommandError("limit must be between 1 and 10")
        if not 1 <= options["concurrency"] <= 8:
            raise CommandError("concurrency must be between 1 and 8")
        if not 0 <= options["article_limit"] <= options["limit"]:
            raise CommandError("article-limit must be between 0 and limit")
        source_ids = options["source_ids"] or _pilot_source_ids(options["wave"])
        sources = list(
            Source.objects.select_related("profile")
            .filter(
                pk__in=source_ids,
                kind="website",
                preferred_adapter="rss_then_article_extraction",
            )
            .order_by("id")
        )
        missing = sorted(set(source_ids) - {source.pk for source in sources})
        results = asyncio.run(
            self._probe(
                sources,
                limit=options["limit"],
                article_limit=options["article_limit"],
                concurrency=options["concurrency"],
            )
        )
        for source, result in zip(sources, results, strict=True):
            SourceProbeRun.objects.create(source=source, **result)
        report = {
            "schema_version": 1,
            "checked_at": timezone.now().isoformat(),
            "wave": options["wave"],
            "requested_source_ids": source_ids,
            "skipped_non_probeable_ids": missing,
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
                f"probed={len(results)} succeeded={succeeded} failed={len(results) - succeeded} skipped={len(missing)}"
            )
        )

    async def _probe(self, sources, *, limit, article_limit, concurrency):
        semaphore = asyncio.Semaphore(concurrency)

        async def one(source):
            started = timezone.now()
            clock = time.monotonic()
            values = {
                "status": "failed",
                "started_at": started,
                "finished_at": started,
            }
            try:
                async with semaphore:
                    details = await probe_source_feed(
                        source,
                        limit=limit,
                        article_limit=article_limit,
                    )
            except IngestionError as error:
                values["error_code"] = error.code
            except Exception:
                values["error_code"] = "probe_failed"
            else:
                values.update(details)
                values["status"] = "success"
            values["latency_ms"] = max(1, int((time.monotonic() - clock) * 1000))
            values["finished_at"] = timezone.now()
            return values

        return await asyncio.gather(*(one(source) for source in sources))
