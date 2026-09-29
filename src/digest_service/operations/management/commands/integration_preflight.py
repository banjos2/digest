import json
import shutil
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from digest_service.ai_gateway.models import AIBudgetPeriod
from digest_service.catalog.models import Source
from digest_service.ingestion.models import SourceProbeRun
from digest_service.operations.guard_service import delivery_guard_ready


class Command(BaseCommand):
    help = "Создать безопасный отчёт готовности внешних интеграций без сетевых вызовов."

    def add_arguments(self, parser):
        parser.add_argument("--output", default="")

    def handle(self, *args, **options):
        today = timezone.localdate()
        budget = AIBudgetPeriod.objects.filter(period_start=today.replace(day=1)).first()
        configured_limit = budget.limit_usd if budget else Decimal(settings.AI_MONTHLY_BUDGET_USD)
        latest_by_source = {}
        for probe in SourceProbeRun.objects.select_related("source").order_by(
            "source_id", "-started_at"
        ):
            latest_by_source.setdefault(probe.source_id, probe)
        live_successes = sum(probe.status == "success" for probe in latest_by_source.values())
        live_failures = sum(probe.status == "failed" for probe in latest_by_source.values())
        telegram_latest = [
            probe for probe in latest_by_source.values() if probe.source.kind == "telegram"
        ]
        pilot_config = json.loads(
            (settings.BASE_DIR / "config" / "pilot_sources.seed.json").read_text(encoding="utf-8")
        )
        pilot_feed_ids = {
            item["source_id"] for item in pilot_config["primary_sources"] if item.get("feed_url")
        }
        pilot_feed_successes = sorted(
            source_id
            for source_id in pilot_feed_ids
            if source_id in latest_by_source and latest_by_source[source_id].status == "success"
        )
        pilot_feed_failures = sorted(
            source_id
            for source_id in pilot_feed_ids
            if source_id in latest_by_source and latest_by_source[source_id].status == "failed"
        )
        pilot_feed_missing = sorted(pilot_feed_ids - latest_by_source.keys())
        guard_ready = delivery_guard_ready()
        telethon_session = Path(settings.TELEGRAM_SOURCE_SESSION_PATH)
        telethon_session_exists = (
            telethon_session.exists() or Path(f"{telethon_session}.session").exists()
        )
        report = {
            "schema_version": 1,
            "checked_at": timezone.now().isoformat(),
            "database_engine": settings.DATABASES["default"]["ENGINE"],
            "sources": {
                "active": Source.objects.filter(is_active=True).count(),
                "unique_live_probed": len(latest_by_source),
                "latest_live_probe_successes": live_successes,
                "latest_live_probe_failures": live_failures,
                "telegram": {
                    "catalogued": Source.objects.filter(kind="telegram").count(),
                    "active": Source.objects.filter(kind="telegram", is_active=True).count(),
                    "latest_probe_successes": sum(
                        probe.status == "success" for probe in telegram_latest
                    ),
                    "latest_probe_failures": sum(
                        probe.status == "failed" for probe in telegram_latest
                    ),
                },
                "pilot_primary_feeds": {
                    "configured": len(pilot_feed_ids),
                    "successful": len(pilot_feed_successes),
                    "failed_ids": pilot_feed_failures,
                    "not_probed_ids": pilot_feed_missing,
                },
            },
            "ai": {
                "provider": "openrouter",
                "api_key_configured": bool(settings.OPENROUTER_API_KEY),
                "base_url": settings.OPENROUTER_BASE_URL,
                "model_ru": settings.AI_RU_MODEL,
                "model_en": settings.AI_EN_MODEL,
                "monthly_limit_usd": str(configured_limit),
                "ready_for_live_call": bool(settings.OPENROUTER_API_KEY and configured_limit > 0),
            },
            "telegram": {
                "bot_token_configured": bool(settings.TELEGRAM_BOT_TOKEN),
                "webhook_secret_configured": bool(settings.TELEGRAM_WEBHOOK_SECRET),
                "delivery_guard_required": settings.DELIVERY_RECOVERY_GUARD_REQUIRED,
                "delivery_guard_ready": guard_ready,
                "ready_for_api_probe": bool(
                    settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_WEBHOOK_SECRET and guard_ready
                ),
                "source_account": {
                    "api_id_configured": bool(settings.TELEGRAM_SOURCE_API_ID),
                    "api_hash_configured": bool(settings.TELEGRAM_SOURCE_API_HASH),
                    "phone_configured": bool(settings.TELEGRAM_SOURCE_PHONE),
                    "session_file_exists": telethon_session_exists,
                    "ready_for_live_collection": bool(
                        settings.TELEGRAM_SOURCE_API_ID
                        and settings.TELEGRAM_SOURCE_API_HASH
                        and telethon_session_exists
                    ),
                },
            },
            "runtime": {
                "docker_available": shutil.which("docker") is not None,
                "pg_dump_available": shutil.which("pg_dump") is not None,
                "age_available": shutil.which("age") is not None,
            },
        }
        if options["output"]:
            output = Path(options["output"]).resolve()
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        self.stdout.write(json.dumps(report, ensure_ascii=False, indent=2))
