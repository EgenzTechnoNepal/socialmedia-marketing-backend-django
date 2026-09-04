import os
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent.parent
load_dotenv(Path(__file__).resolve().parent.parent / ".env")
load_dotenv(BASE_DIR / ".env")


def env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def env_bool(key: str, default: bool = False) -> bool:
    raw = os.environ.get(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_list(key: str, default: str = "") -> list[str]:
    raw = os.environ.get(key, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


SECRET_KEY = env("DJANGO_SECRET_KEY", "dev-only-change-me")
DEBUG = env_bool("DJANGO_DEBUG", True)
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1")

INSTALLED_APPS = [
    "daphne",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "corsheaders",
    "channels",
    "apps.common",
    "apps.accounts",
    "apps.billing",
    "apps.contacts",
    "apps.whatsapp",
    "apps.messaging",
    "apps.campaigns",
    "apps.chatbot",
    "apps.calling",
    "apps.webhooks",
    "apps.catalogs",
    "apps.analytics",
    "apps.realtime",
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "apps.common.middleware.GoStyleCSRFMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "apps.common.middleware.OrganizationHeaderMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"
APPEND_SLASH = False

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    }
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env("POSTGRES_DB", "whatomate"),
        "USER": env("POSTGRES_USER", "whatomate"),
        "PASSWORD": env("POSTGRES_PASSWORD", "whatomate"),
        "HOST": env("POSTGRES_HOST", "127.0.0.1"),
        "PORT": env("POSTGRES_PORT", "5432"),
    }
}

AUTH_PASSWORD_VALIDATORS = []
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True
STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "apps.common.authentication.CookieJWTAuthentication",
        "apps.common.authentication.APIKeyAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "apps.common.permissions.CookieAuthenticated",
    ],
    "EXCEPTION_HANDLER": "apps.common.envelope.exception_handler",
    "UNAUTHENTICATED_USER": None,
}

CORS_ALLOWED_ORIGINS = env_list(
    "CORS_ALLOWED_ORIGINS",
    "http://localhost:3000,http://127.0.0.1:3000",
)
CORS_ALLOW_CREDENTIALS = True
CORS_ALLOW_HEADERS = [
    "accept",
    "authorization",
    "content-type",
    "x-api-key",
    "x-csrf-token",
    "x-organization-id",
]

JWT_SECRET = env("JWT_SECRET", "your-super-secret-jwt-key-change-in-production")
JWT_ISSUER = "whatomate"
JWT_ACCESS_EXPIRY_MINS = int(env("JWT_ACCESS_EXPIRY_MINS", "15"))
JWT_REFRESH_EXPIRY_DAYS = int(env("JWT_REFRESH_EXPIRY_DAYS", "1"))
COOKIE_ACCESS_NAME = "whm_access"
COOKIE_REFRESH_NAME = "whm_refresh"
COOKIE_CSRF_NAME = "whm_csrf"
COOKIE_SECURE = env_bool("COOKIE_SECURE", False)
COOKIE_DOMAIN = env("COOKIE_DOMAIN") or None

REDIS_URL = env("REDIS_URL", "redis://127.0.0.1:6379/0")
_redis = urlparse(REDIS_URL)
CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels_redis.core.RedisChannelLayer",
        "CONFIG": {"hosts": [(_redis.hostname or "127.0.0.1", _redis.port or 6379)]},
    }
}
CELERY_BROKER_URL = REDIS_URL
CELERY_RESULT_BACKEND = REDIS_URL
CELERY_TASK_ALWAYS_EAGER = env_bool("CELERY_TASK_ALWAYS_EAGER", DEBUG)
CELERY_TASK_EAGER_PROPAGATES = True
CELERY_BEAT_SCHEDULE = {
    "ingest-dodo-usage": {
        "task": "apps.billing.tasks.ingest_usage_events",
        "schedule": 60.0,
    },
}

PUBLIC_APP_URL = env("PUBLIC_APP_URL", "http://localhost:3000").rstrip("/")
ENCRYPTION_KEY = env("ENCRYPTION_KEY")
WHATSAPP_WEBHOOK_VERIFY_TOKEN = env("WHATSAPP_WEBHOOK_VERIFY_TOKEN")
WHATSAPP_API_VERSION = env("WHATSAPP_API_VERSION", "v24.0")
META_APP_ID = env("META_APP_ID")
META_APP_SECRET = env("META_APP_SECRET")
META_CONFIG_ID = env("META_CONFIG_ID")
STORAGE_LOCAL_PATH = env("STORAGE_LOCAL_PATH", str(Path(__file__).resolve().parent.parent / "media"))

DODO_PAYMENTS_API_KEY = env("DODO_PAYMENTS_API_KEY")
DODO_WEBHOOK_SECRET = env("DODO_WEBHOOK_SECRET")
DODO_ENVIRONMENT = env("DODO_ENVIRONMENT", "test_mode")
DODO_RETURN_URL = env("DODO_RETURN_URL", f"{PUBLIC_APP_URL}/settings/billing")
DODO_PRODUCT_PRO = env("DODO_PRODUCT_PRO")
DODO_PRODUCT_BUSINESS = env("DODO_PRODUCT_BUSINESS")
DODO_ADDON_SEAT = env("DODO_ADDON_SEAT")
DODO_METER_MESSAGE_SENT = env("DODO_METER_MESSAGE_SENT", "message.sent")
DODO_METER_AI_COMPLETION = env("DODO_METER_AI_COMPLETION", "ai.completion")
DODO_METER_CAMPAIGN_RECIPIENT = env("DODO_METER_CAMPAIGN_RECIPIENT", "campaign.recipient")
