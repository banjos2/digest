import hashlib
import hmac

from django.conf import settings


def erasure_subject_hash(telegram_user_id):
    return hmac.new(
        settings.ERASURE_HASH_KEY.encode(),
        str(telegram_user_id).encode(),
        hashlib.sha256,
    ).hexdigest()


def anonymized_identifier(request_id, label):
    digest = hmac.new(
        settings.ERASURE_HASH_KEY.encode(),
        f"{request_id}:{label}".encode(),
        hashlib.sha256,
    ).digest()
    return (int.from_bytes(digest[:8], "big") & ((1 << 63) - 1)) or 1
