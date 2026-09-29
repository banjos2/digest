#!/usr/bin/env bash
# Railway attaches a volume to one service only. The Celery worker writes the Telethon
# session, backups and erasure guard that the Telegram sender reads, so every background
# process shares this container. Each restarts on its own, like `restart: unless-stopped`.
set -u
mkdir -p "$(dirname "$TELEGRAM_SOURCE_SESSION_PATH")" "$BACKUP_OUTPUT_DIR" "$ERASURE_GUARD_OUTPUT_DIR"

keep_running() {
    trap 'kill -TERM "$child" 2>/dev/null; wait "$child"; exit 0' TERM
    while true; do
        "$@" &
        child=$!
        wait "$child"
        echo "'$*' exited with $?, restarting in 15s" >&2
        sleep 15
    done
}

# Forward SIGTERM so each process shuts down cleanly and releases its process lease.
trap 'kill -TERM $(jobs -p) 2>/dev/null; wait; exit 0' TERM INT

keep_running celery -A digest_service.celery_app worker -Q maintenance \
    --loglevel=INFO --concurrency=1 --hostname=worker@%h &
keep_running python manage.py run_background_scheduler --poll-seconds 15 &
keep_running python manage.py run_telegram_polling --timeout 30 --receive-only --delete-webhook &
keep_running python manage.py run_telegram_sender --poll-seconds 2 --batch-size 25 &
wait
