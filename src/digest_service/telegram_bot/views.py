import hmac
import json

from django.conf import settings
from django.core.exceptions import ValidationError
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from .processor import process_update_payload


@csrf_exempt
def telegram_webhook(request):
    if request.method != "POST":
        return JsonResponse({"ok": False}, status=405)
    if not settings.TELEGRAM_BOT_TOKEN or not settings.TELEGRAM_WEBHOOK_SECRET:
        return JsonResponse({"ok": False, "error": "telegram_not_configured"}, status=503)
    supplied = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not hmac.compare_digest(supplied, settings.TELEGRAM_WEBHOOK_SECRET):
        return JsonResponse({"ok": False}, status=403)
    if not request.content_type.startswith("application/json"):
        return JsonResponse({"ok": False}, status=415)
    if len(request.body) > settings.TELEGRAM_WEBHOOK_MAX_BYTES:
        return JsonResponse({"ok": False}, status=413)
    try:
        payload = json.loads(request.body)
        if not isinstance(payload, dict):
            raise ValueError
        process_update_payload(payload)
    except (json.JSONDecodeError, TypeError, ValueError, ValidationError):
        return JsonResponse({"ok": False}, status=400)
    return JsonResponse({"ok": True})
