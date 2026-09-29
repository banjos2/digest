#!/usr/bin/env python
import os
import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "digest_service.settings")

if __name__ == "__main__":
    from django.core.management import execute_from_command_line

    # SIGTERM exits through finally blocks, so long-running commands release their process leases.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(128 + signal.SIGTERM))
    execute_from_command_line(sys.argv)
