"""Environment-specific application configuration."""
from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _database_url() -> str:
    value = os.getenv("DATABASE_URL", "sqlite:///gcemailer.db")
    return "postgresql+psycopg://" + value.removeprefix("postgresql://") if value.startswith("postgresql://") else value


class BaseConfig:
    APP_ENV = os.getenv("APP_ENV", "development").lower()
    SECRET_KEY = os.getenv("SECRET_KEY", "development-only-change-me")
    SQLALCHEMY_DATABASE_URI = _database_url()
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    REDIS_URL = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
    SESSION_TYPE = "redis"
    SESSION_PERMANENT = True
    PERMANENT_SESSION_LIFETIME = timedelta(hours=int(os.getenv("SESSION_HOURS", "12")))
    SESSION_USE_SIGNER = True
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = False
    WTF_CSRF_TIME_LIMIT = timedelta(hours=12)
    WTF_CSRF_ENABLED = False
    MAX_CONTENT_LENGTH = 16 * 1024 * 1024
    MAX_CSV_ROWS = int(os.getenv("MAX_CSV_ROWS", "10000"))
    MAX_CSV_COLUMNS = int(os.getenv("MAX_CSV_COLUMNS", "100"))
    MAX_FIELD_LENGTH = int(os.getenv("MAX_FIELD_LENGTH", "10000"))
    MAX_CAMPAIGN_RECIPIENTS = int(os.getenv("MAX_CAMPAIGN_RECIPIENTS", "1500"))
    ALLOWED_GOOGLE_DOMAIN = os.getenv("ALLOWED_GOOGLE_DOMAIN", "gulfconferences.co.uk").lower()
    INITIAL_ADMIN_EMAILS = {
        value.strip().lower()
        for value in os.getenv("INITIAL_ADMIN_EMAILS", "").split(",") if value.strip()
    }
    TOKEN_ENCRYPTION_KEY = os.getenv("TOKEN_ENCRYPTION_KEY", "")
    RETENTION_DAYS = int(os.getenv("RETENTION_DAYS", "30"))
    AUDIT_RETENTION_DAYS = int(os.getenv("AUDIT_RETENTION_DAYS", "365"))
    STORAGE_ROOT = Path(os.getenv("STORAGE_ROOT", "instance/data")).resolve()
    UPLOAD_FOLDER = str(STORAGE_ROOT / "uploads")
    EMAIL_ASSET_FOLDER = str(STORAGE_ROOT / "assets")
    EMAIL_TEMPLATE_FOLDER = str(STORAGE_ROOT / "legacy_templates")
    REQUIRE_AUTH = _bool("REQUIRE_AUTH", False)
    RATELIMIT_STORAGE_URI = REDIS_URL
    RATELIMIT_DEFAULT = "300 per hour"
    TRUST_PROXY = _bool("TRUST_PROXY", False)
    STAGING_SEND_ALLOWLIST = {
        value.strip().lower()
        for value in os.getenv("STAGING_SEND_ALLOWLIST", "").split(",") if value.strip()
    }

    @classmethod
    def validate(cls) -> list[str]:
        return []


class DevelopmentConfig(BaseConfig):
    APP_ENV = "development"
    SESSION_TYPE = "filesystem"
    SESSION_PERMANENT = False
    RATELIMIT_STORAGE_URI = "memory://"


class TestingConfig(BaseConfig):
    APP_ENV = "testing"
    TESTING = True
    WTF_CSRF_ENABLED = False
    REQUIRE_AUTH = False
    SESSION_TYPE = "filesystem"
    SESSION_PERMANENT = False
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    RATELIMIT_STORAGE_URI = "memory://"


class ProductionConfig(BaseConfig):
    APP_ENV = "production"
    REQUIRE_AUTH = True
    SESSION_COOKIE_SECURE = True
    WTF_CSRF_ENABLED = True

    @classmethod
    def validate(cls) -> list[str]:
        missing = []
        if len(cls.SECRET_KEY) < 32 or cls.SECRET_KEY == "development-only-change-me":
            missing.append("SECRET_KEY (at least 32 characters)")
        if not cls.SQLALCHEMY_DATABASE_URI.startswith(("postgresql://", "postgresql+psycopg://")):
            missing.append("DATABASE_URL (PostgreSQL)")
        if not cls.REDIS_URL:
            missing.append("REDIS_URL")
        if not cls.TOKEN_ENCRYPTION_KEY:
            missing.append("TOKEN_ENCRYPTION_KEY")
        if not os.getenv("GOOGLE_CLIENT_ID"):
            missing.append("GOOGLE_CLIENT_ID")
        if not os.getenv("GOOGLE_CLIENT_SECRET"):
            missing.append("GOOGLE_CLIENT_SECRET")
        if not os.getenv("GOOGLE_REDIRECT_URI", "").startswith("https://"):
            missing.append("GOOGLE_REDIRECT_URI (HTTPS)")
        if not cls.INITIAL_ADMIN_EMAILS:
            missing.append("INITIAL_ADMIN_EMAILS")
        return missing


class StagingConfig(ProductionConfig):
    APP_ENV = "staging"

    @classmethod
    def validate(cls) -> list[str]:
        missing = super().validate()
        if not cls.STAGING_SEND_ALLOWLIST:
            missing.append("STAGING_SEND_ALLOWLIST")
        return missing


CONFIGS = {
    "development": DevelopmentConfig,
    "testing": TestingConfig,
    "staging": StagingConfig,
    "production": ProductionConfig,
}


def get_config(name: str | None = None):
    key = (name or os.getenv("APP_ENV", "development")).lower()
    if key not in CONFIGS:
        raise RuntimeError(f"Unknown APP_ENV: {key}")
    return CONFIGS[key]
