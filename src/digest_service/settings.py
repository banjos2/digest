import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parents[2]
DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "")
if not SECRET_KEY:
    if not DEBUG:
        raise ImproperlyConfigured("DJANGO_SECRET_KEY is required when DEBUG is disabled.")
    SECRET_KEY = "local-development-only-do-not-use-for-public-deployment"
ERASURE_HASH_KEY = os.environ.get("ERASURE_HASH_KEY", "")
if not ERASURE_HASH_KEY:
    if not DEBUG:
        raise ImproperlyConfigured("ERASURE_HASH_KEY is required when DEBUG is disabled.")
    ERASURE_HASH_KEY = SECRET_KEY

ALLOWED_HOSTS = os.environ.get("DJANGO_ALLOWED_HOSTS", "127.0.0.1,localhost,[::1]").split(",")
CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",")
    if origin.strip()
]
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "digest_service.core",
    "digest_service.catalog",
    "digest_service.ingestion",
    "digest_service.ai_gateway",
    "digest_service.editorial",
    "digest_service.scheduling",
    "digest_service.subscriptions",
    "digest_service.delivery",
    "digest_service.telegram_bot",
    "digest_service.operations",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
ROOT_URLCONF = "digest_service.urls"
WSGI_APPLICATION = "digest_service.wsgi.application"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ]
        },
    }
]
if os.environ.get("POSTGRES_HOST"):
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "HOST": os.environ["POSTGRES_HOST"],
            "PORT": os.environ.get("POSTGRES_PORT", "5432"),
            "NAME": os.environ.get("POSTGRES_DB", "digest"),
            "USER": os.environ.get("POSTGRES_USER", "digest"),
            "PASSWORD": os.environ.get("POSTGRES_PASSWORD", ""),
            "CONN_MAX_AGE": 60,
        }
    }
else:
    if not DEBUG and os.environ.get("DJANGO_ALLOW_SQLITE") != "1":
        raise ImproperlyConfigured("PostgreSQL is required outside local development.")
    (BASE_DIR / "var").mkdir(exist_ok=True)
    DATABASES = {
        "default": {"ENGINE": "django.db.backends.sqlite3", "NAME": BASE_DIR / "var/dev.sqlite3"}
    }

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
LANGUAGE_CODE = "ru"
LANGUAGES = [("ru", "Русский"), ("en", "English")]
TIME_ZONE = "Europe/Moscow"
USE_I18N = True
USE_TZ = True
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_SSL_REDIRECT = not DEBUG and os.environ.get("DJANGO_SECURE_SSL_REDIRECT", "1") == "1"
SECURE_HSTS_SECONDS = 0 if DEBUG else int(os.environ.get("DJANGO_HSTS_SECONDS", "31536000"))
SECURE_HSTS_INCLUDE_SUBDOMAINS = not DEBUG
SECURE_HSTS_PRELOAD = not DEBUG
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"
# Only configure forwarded-protocol trust when requests come through the controlled proxy.
if os.environ.get("DJANGO_TRUST_PROXY") == "1":
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

CELERY_BROKER_URL = os.environ.get("CELERY_BROKER_URL", "amqp://guest:guest@localhost:5672//")
CELERY_TASK_SERIALIZER = "json"
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_IGNORE_RESULT = True
CELERY_TASK_DEFAULT_QUEUE = "maintenance"
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_TASK_SOFT_TIME_LIMIT = int(os.environ.get("CELERY_TASK_SOFT_TIME_LIMIT", "1500"))
CELERY_TASK_TIME_LIMIT = int(os.environ.get("CELERY_TASK_TIME_LIMIT", "1800"))
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
BACKGROUND_JOB_LEASE_SECONDS = int(os.environ.get("BACKGROUND_JOB_LEASE_SECONDS", "120"))
AUTOMATIC_DIGEST_JOB_LEASE_SECONDS = int(
    os.environ.get("AUTOMATIC_DIGEST_JOB_LEASE_SECONDS", "2100")
)
SERVICE_PROCESS_LEASE_SECONDS = int(os.environ.get("SERVICE_PROCESS_LEASE_SECONDS", "300"))
TELEGRAM_IN_FLIGHT_TIMEOUT_SECONDS = int(
    os.environ.get("TELEGRAM_IN_FLIGHT_TIMEOUT_SECONDS", "600")
)
SOURCE_COLLECTION_INTERVAL_MINUTES = int(
    os.environ.get("SOURCE_COLLECTION_INTERVAL_MINUTES", "15")
)

SOURCE_HTTP_USER_AGENT = os.environ.get(
    "SOURCE_HTTP_USER_AGENT", "OwnedDigestBot/0.1 (+source-access-contact-not-configured)"
)
if not DEBUG and "not-configured" in SOURCE_HTTP_USER_AGENT:
    raise ImproperlyConfigured("SOURCE_HTTP_USER_AGENT must include a real contact in production.")
SOURCE_HTTP_CONNECT_TIMEOUT_SECONDS = 8
SOURCE_HTTP_READ_TIMEOUT_SECONDS = 20
SOURCE_HTTP_MAX_BYTES = 5 * 1024 * 1024
SOURCE_HTTP_MAX_REDIRECTS = 5
SOURCE_COLLECTION_MAX_ITEMS = 50
SOURCE_TEXT_RETENTION_DAYS = 90

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
OPENROUTER_APP_TITLE = os.environ.get("OPENROUTER_APP_TITLE", "Owned AI Digest")
AI_RU_MODEL = os.environ.get("AI_RU_MODEL", "openai/gpt-5.4")
AI_EN_MODEL = os.environ.get("AI_EN_MODEL", "openai/gpt-5-mini")
AI_RU_INPUT_USD_PER_MTOK = os.environ.get("AI_RU_INPUT_USD_PER_MTOK", "2.50")
AI_RU_OUTPUT_USD_PER_MTOK = os.environ.get("AI_RU_OUTPUT_USD_PER_MTOK", "15.00")
AI_EN_INPUT_USD_PER_MTOK = os.environ.get("AI_EN_INPUT_USD_PER_MTOK", "0.25")
AI_EN_OUTPUT_USD_PER_MTOK = os.environ.get("AI_EN_OUTPUT_USD_PER_MTOK", "2.00")
AI_MONTHLY_BUDGET_USD = os.environ.get("AI_MONTHLY_BUDGET_USD", "0.00")
AI_PROVIDER_TIMEOUT_SECONDS = 60
AUTOMATIC_GROUPING_THRESHOLD = float(os.environ.get("AUTOMATIC_GROUPING_THRESHOLD", "0.65"))
AUTOMATIC_MAIN_STORIES = int(os.environ.get("AUTOMATIC_MAIN_STORIES", "5"))
AUTOMATIC_ADDITIONAL_STORIES = int(os.environ.get("AUTOMATIC_ADDITIONAL_STORIES", "2"))

PREFERENCE_DRAFT_TTL_MINUTES = int(os.environ.get("PREFERENCE_DRAFT_TTL_MINUTES", "30"))
ERASURE_CONFIRMATION_TTL_MINUTES = int(
    os.environ.get("ERASURE_CONFIRMATION_TTL_MINUTES", "5")
)
ERASURE_GUARD_DAYS = int(os.environ.get("ERASURE_GUARD_DAYS", "45"))
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
_telegram_source_api_id = os.environ.get("TELEGRAM_SOURCE_API_ID", "")
try:
    TELEGRAM_SOURCE_API_ID = int(_telegram_source_api_id) if _telegram_source_api_id else 0
except ValueError as error:
    raise ImproperlyConfigured("TELEGRAM_SOURCE_API_ID must be an integer.") from error
TELEGRAM_SOURCE_API_HASH = os.environ.get("TELEGRAM_SOURCE_API_HASH", "")
TELEGRAM_SOURCE_PHONE = os.environ.get("TELEGRAM_SOURCE_PHONE", "")
TELEGRAM_SOURCE_SESSION_PATH = Path(
    os.environ.get("TELEGRAM_SOURCE_SESSION_PATH", BASE_DIR / "var/telethon/source")
).resolve()
TELEGRAM_SOURCE_EDIT_LOOKBACK = int(os.environ.get("TELEGRAM_SOURCE_EDIT_LOOKBACK", "20"))
TELEGRAM_WEBHOOK_MAX_BYTES = 1024 * 1024
BACKUP_OUTPUT_DIR = Path(
    os.environ.get("BACKUP_OUTPUT_DIR", BASE_DIR / "var" / "training-backups")
).resolve()
BACKUP_STORAGE_CLASS = os.environ.get(
    "BACKUP_STORAGE_CLASS", "local_training" if DEBUG else "external"
)
BACKUP_ALLOW_SQLITE_TRAINING = os.environ.get(
    "BACKUP_ALLOW_SQLITE_TRAINING", "1" if DEBUG else "0"
) == "1"
BACKUP_RETENTION_DAYS = int(os.environ.get("BACKUP_RETENTION_DAYS", "30"))
BACKUP_AGE_RECIPIENT = os.environ.get("BACKUP_AGE_RECIPIENT", "")
BACKUP_AGE_IDENTITY_FILE = os.environ.get("BACKUP_AGE_IDENTITY_FILE", "")
BACKUP_COMMAND_TIMEOUT_SECONDS = int(os.environ.get("BACKUP_COMMAND_TIMEOUT_SECONDS", "1800"))
BACKUP_SCHEDULE_ENABLED = os.environ.get("BACKUP_SCHEDULE_ENABLED", "0") == "1"
BACKUP_RESTORE_DATABASE_PREFIX = os.environ.get(
    "BACKUP_RESTORE_DATABASE_PREFIX", "digest_restore_"
)
ERASURE_GUARD_OUTPUT_DIR = Path(
    os.environ.get("ERASURE_GUARD_OUTPUT_DIR", BASE_DIR / "var" / "training-guard-backups")
).resolve()
ERASURE_GUARD_STORAGE_CLASS = os.environ.get(
    "ERASURE_GUARD_STORAGE_CLASS", "local_training" if DEBUG else "external"
)
ERASURE_GUARD_BACKUP_RETENTION_DAYS = int(
    os.environ.get("ERASURE_GUARD_BACKUP_RETENTION_DAYS", "60")
)
DELIVERY_RECOVERY_GUARD_REQUIRED = os.environ.get(
    "DELIVERY_RECOVERY_GUARD_REQUIRED", "0" if DEBUG else "1"
) == "1"
if not DEBUG and DELIVERY_RECOVERY_GUARD_REQUIRED:
    if ERASURE_GUARD_STORAGE_CLASS != "external":
        raise ImproperlyConfigured("External erasure guard storage is required in production.")
    if ERASURE_GUARD_OUTPUT_DIR == BACKUP_OUTPUT_DIR:
        raise ImproperlyConfigured("Erasure guard storage must be separate from database backups.")
    if ERASURE_GUARD_BACKUP_RETENTION_DAYS < ERASURE_GUARD_DAYS:
        raise ImproperlyConfigured("Erasure guard backup retention is shorter than guard retention.")
    if not BACKUP_AGE_RECIPIENT:
        raise ImproperlyConfigured("BACKUP_AGE_RECIPIENT is required in production.")
if bool(TELEGRAM_BOT_TOKEN) != bool(TELEGRAM_WEBHOOK_SECRET):
    raise ImproperlyConfigured(
        "TELEGRAM_BOT_TOKEN and TELEGRAM_WEBHOOK_SECRET must be configured together."
    )
if bool(TELEGRAM_SOURCE_API_ID) != bool(TELEGRAM_SOURCE_API_HASH):
    raise ImproperlyConfigured(
        "TELEGRAM_SOURCE_API_ID and TELEGRAM_SOURCE_API_HASH must be configured together."
    )
