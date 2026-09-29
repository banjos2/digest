import hashlib
import json
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from digest_service.catalog.models import (
    Collection,
    OriginGroup,
    PublisherProfile,
    RetentionPolicy,
    Source,
    SourceCollection,
)
from digest_service.core.models import SeedSnapshot
from digest_service.scheduling.models import Schedule

FILENAMES = [
    "source_catalog.seed.json",
    "schedule_catalog.seed.json",
    "data_retention_policy.json",
    "editorial_policy.json",
    "user_flow_policy.json",
    "pilot_sources.seed.json",
]


class Command(BaseCommand):
    help = (
        "Import initial catalogue and schedules without overwriting existing administrator changes."
    )

    def add_arguments(self, parser):
        parser.add_argument("--directory", type=Path, default=settings.BASE_DIR / "config")

    def handle(self, *args, **options):
        try:
            loaded = {
                name: json.loads((options["directory"] / name).read_text(encoding="utf8"))
                for name in FILENAMES
            }
            with transaction.atomic():
                counts = self.import_records(loaded)
                for name, payload in loaded.items():
                    digest = hashlib.sha256(
                        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
                    ).hexdigest()
                    SeedSnapshot.objects.get_or_create(
                        filename=name, sha256=digest, defaults={"payload": payload}
                    )
        except (OSError, ValueError, KeyError, TypeError, ValidationError) as exc:
            raise CommandError(f"Seed import rolled back: {exc}") from exc
        self.stdout.write(json.dumps(counts, ensure_ascii=True))

    def import_records(self, loaded):
        counts = {"created": 0, "kept": 0}

        def create(model, lookup, **defaults):
            obj, created = model.objects.get_or_create(**lookup, defaults=defaults)
            counts["created" if created else "kept"] += 1
            return obj

        retention = loaded["data_retention_policy.json"]
        for entry in retention["source_retention_profiles"]:
            create(
                RetentionPolicy,
                {"id": entry["id"]},
                name=entry["id"],
                definition={
                    "profile": entry,
                    "categories": {
                        key: retention["categories"][key] for key in entry["category_refs"]
                    },
                    "source_overrides": retention["source_overrides"],
                },
            )
        catalog = loaded["source_catalog.seed.json"]
        for entry in catalog["profiles"]:
            group = create(
                OriginGroup, {"id": entry["origin_group_id"]}, name=entry["origin_group_id"]
            )
            create(
                PublisherProfile,
                {"id": entry["id"]},
                name=entry["name"],
                origin_group=group,
                role=entry["role"],
                default_language=entry["default_language"],
                notes=entry.get("notes", ""),
                research_metadata=entry,
            )
        for entry in catalog["collections"]:
            create(
                Collection,
                {"id": entry["id"]},
                name_ru=entry["name"]["ru"],
                name_en=entry["name"]["en"],
                description_ru=entry.get("description", {}).get("ru", ""),
                description_en=entry.get("description", {}).get("en", ""),
                kind=entry["kind"],
                release_target=entry["release_target"],
                is_active=False,
                sort_order=entry["sort_order"],
                routing_keywords=entry.get("routing_keywords", []),
            )
        for entry in catalog["sources"]:
            create(
                Source,
                {"id": entry["id"]},
                profile_id=entry["profile_id"],
                origin_group_id=entry["origin_group_id"],
                kind=entry["kind"],
                url=entry["url"],
                input_language=entry["input_language"],
                retention_policy_id=entry["retention_policy_id"],
                is_active=False,
                access_review_status="pending",
                collection_check_status="not_connected",
                preferred_adapter=entry.get("preferred_adapter", ""),
                preferred_feed_url=entry.get("preferred_feed_url") or "",
                technical_status=entry.get("technical_status", ""),
                research_metadata=entry,
            )
        for entry in catalog["source_collections"]:
            create(
                SourceCollection,
                {"source_id": entry["source_id"], "collection_id": entry["collection_id"]},
                is_active=False,
                priority=entry["priority"],
                usage=entry["usage"],
                topic_filter=(
                    "Требуется тематическая классификация события."
                    if entry.get("topic_filter_required")
                    else ""
                ),
                editorial_note=entry.get("editorial_note", ""),
            )
        schedule_catalog = loaded["schedule_catalog.seed.json"]
        defaults = schedule_catalog["proposed_defaults"]
        for entry in schedule_catalog["schedules"]:
            recurrence = entry["recurrence"]
            create(
                Schedule,
                {"id": entry["id"]},
                name_ru=entry["name"]["ru"],
                name_en=entry["name"]["en"],
                timezone=entry["timezone"],
                recurrence_kind=recurrence["kind"],
                local_times=recurrence["local_times"],
                iso_weekdays=recurrence.get("iso_weekdays", []),
                sort_order=entry["sort_order"],
                preparation_lead_minutes=defaults["preparation_lead_minutes"],
                late_delivery_minutes=defaults["automatic_late_delivery_max_minutes"],
                visible_to_new_subscribers=False,
                delivery_enabled=False,
            )
        return counts
