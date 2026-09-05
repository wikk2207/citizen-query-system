"""Configuration shared by local Flask development and serverless deployments."""
import os
import tempfile
from datetime import timedelta

from sqlalchemy.pool import NullPool


basedir = os.path.abspath(os.path.dirname(__file__))


def _as_bool(name, default=False):
    return os.environ.get(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def normalize_database_url(url):
    """Normalize legacy Postgres URLs to a SQLAlchemy-supported scheme."""
    if url and url.startswith("postgres://"):
        return "postgresql://" + url[len("postgres://"):]
    return url


IS_VERCEL = bool(os.environ.get("VERCEL"))
IS_PRODUCTION = IS_VERCEL or os.environ.get("FLASK_ENV", "").lower() == "production"
RUNTIME_ROOT = os.environ.get(
    "RUNTIME_DATA_DIR",
    os.path.join(tempfile.gettempdir(), "civicvoice") if IS_VERCEL else os.path.join(basedir, "instance"),
)


class Config:
    IS_VERCEL = IS_VERCEL
    IS_PRODUCTION = IS_PRODUCTION
    RUNTIME_ROOT = RUNTIME_ROOT
    SECRET_KEY = os.environ.get("SECRET_KEY")
    SQLALCHEMY_DATABASE_URI = normalize_database_url(os.environ.get("DATABASE_URL"))
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = (
        {"poolclass": NullPool, "pool_pre_ping": True}
        if IS_VERCEL or os.environ.get("DB_POOL_MODE", "").lower() == "null"
        else {"pool_pre_ping": True, "pool_recycle": 300}
    )

    # Schema changes are applied with Alembic in production, never on a Vercel import.
    AUTO_SCHEMA_MANAGEMENT = _as_bool("AUTO_SCHEMA_MANAGEMENT", not IS_PRODUCTION)
    DEV_OTP_MODE = _as_bool("DEV_OTP_MODE", not IS_PRODUCTION)

    MAIL_SERVER = os.environ.get("MAIL_SERVER")
    MAIL_PORT = int(os.environ.get("MAIL_PORT", "587"))
    MAIL_USE_TLS = _as_bool("MAIL_USE_TLS", True)
    MAIL_USE_SSL = _as_bool("MAIL_USE_SSL", False)
    MAIL_USERNAME = os.environ.get("MAIL_USERNAME")
    MAIL_PASSWORD = os.environ.get("MAIL_PASSWORD")
    MAIL_DEFAULT_SENDER = os.environ.get("MAIL_DEFAULT_SENDER")
    MAIL_DEBUG = _as_bool("MAIL_DEBUG", False)
    MAIL_TIMEOUT = int(os.environ.get("MAIL_TIMEOUT", "6"))
    FORCE_SMTP = _as_bool("FORCE_SMTP", False)
    RESEND_API_KEY = os.environ.get("RESEND_API_KEY")
    RESEND_FROM_EMAIL = os.environ.get("RESEND_FROM_EMAIL")

    # Vercel has no persistent disk. Do not save user files to /tmp as if permanent.
    UPLOAD_STORAGE_BACKEND = os.environ.get("UPLOAD_STORAGE_BACKEND", "local").lower()
    PERSISTENT_UPLOADS_ENABLED = not IS_VERCEL and UPLOAD_STORAGE_BACKEND == "local"
    UPLOAD_FOLDER = (
        os.path.join(RUNTIME_ROOT, "uploads")
        if IS_VERCEL
        else os.path.join(basedir, "static", "uploads")
    )
    MAX_CONTENT_LENGTH = int(os.environ.get("MAX_CONTENT_LENGTH", str(100 * 1024 * 1024)))
    ALLOWED_EXTENSIONS = {
        "pdf", "png", "jpg", "jpeg", "gif", "webp", "doc", "docx", "ppt", "pptx",
        "xls", "xlsx", "txt", "zip", "mp4", "webm", "mov", "mkv", "avi", "mp3", "wav", "m4a",
    }

    PERMANENT_SESSION_LIFETIME = timedelta(minutes=int(os.environ.get("SESSION_TIMEOUT_MINUTES", "60")))
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = IS_PRODUCTION
    WTF_CSRF_ENABLED = True
    TESSERACT_CMD = os.environ.get("TESSERACT_CMD")
    CORS_ORIGINS = [origin.strip() for origin in os.environ.get("CORS_ORIGINS", "").split(",") if origin.strip()]

    LEVEL_POINTS = {"College": 10, "State": 30, "National": 50, "International": 100}
    MENTOR_EMAIL = os.environ.get("MENTOR_EMAIL")
    MENTOR_WHITELIST_EMAILS = os.environ.get("MENTOR_WHITELIST_EMAILS", "")
    RANK_BONUS = {"First": 20, "Second": 15, "Third": 10, "Participation": 5}


class ProductionConfig(Config):
    DEBUG = False


class DevelopmentConfig(Config):
    DEBUG = True


config_map = {
    "development": DevelopmentConfig,
    "production": ProductionConfig,
    "default": DevelopmentConfig,
}
