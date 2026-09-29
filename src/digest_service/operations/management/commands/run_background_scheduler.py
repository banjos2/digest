import os
import socket
import time
import uuid

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError

from digest_service.operations.service import acquire_process_lease, release_process_lease


class Command(BaseCommand):
    help = "Постоянно планировать задания и публиковать outbox"

    def add_arguments(self, parser):
        parser.add_argument("--poll-seconds", type=int, default=15)
        parser.add_argument("--once", action="store_true")
        parser.add_argument("--local-execute", action="store_true")

    def handle(self, *args, **options):
        interval = options["poll_seconds"]
        if interval < 1 or interval > 60:
            raise CommandError("poll-seconds must be between 1 and 60")
        owner = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4()}"
        if not acquire_process_lease(name="background_scheduler", owner=owner):
            raise CommandError("another background scheduler holds the active lease")
        try:
            while True:
                call_command("plan_background_jobs", verbosity=0)
                if options["local_execute"]:
                    call_command("run_due_jobs", verbosity=0)
                else:
                    call_command("publish_outbox", verbosity=0)
                if not acquire_process_lease(name="background_scheduler", owner=owner):
                    raise CommandError("background scheduler lost its process lease")
                if options["once"]:
                    return
                time.sleep(interval)
        finally:
            release_process_lease(name="background_scheduler", owner=owner)
