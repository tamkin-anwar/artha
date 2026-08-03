import os
from datetime import timedelta

# Absolute path to the repo root (one level up from this file)
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTANCE_DIR = os.path.join(ROOT_DIR, "instance")
os.makedirs(INSTANCE_DIR, exist_ok=True)


def _resolve_db_url() -> str:
    """Normalise Render's postgres:// → postgresql:// and return the URL."""
    url = os.environ.get("DATABASE_URL", "")
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return url


class Config:
    """Shared defaults."""
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-only-change-me")
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = False
    WTF_CSRF_HEADERS = ["X-CSRFToken", "X-CSRF-Token"]
    REMEMBER_COOKIE_DURATION = timedelta(days=30)
    REMEMBER_COOKIE_SECURE = True
    REMEMBER_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_SAMESITE = "Lax"

    # Web Push (renewal reminders). The fallback pair below is a fixed,
    # non-secret dev-only keypair (generated once with py_vapid) — good
    # enough for testing push locally, but every deployed environment
    # must set real env vars or all its browsers share one identity.
    VAPID_PUBLIC_KEY = os.environ.get(
        "VAPID_PUBLIC_KEY",
        "BDtdnorACqlilTm8hiSQ4ZYlji8GdaJxxfPCs0P44FtO9tejgpt-rHDQf9zNJCOHmKtf2WYiJqvbqH45-qwai7E",
    )
    VAPID_PRIVATE_KEY = os.environ.get(
        "VAPID_PRIVATE_KEY", "308EL19hh8MOodozDcYdBquW4xOo0dIPGM1y28bvwMc"
    )
    VAPID_CLAIMS_EMAIL = os.environ.get("VAPID_CLAIMS_EMAIL", "tamkinanwar7@gmail.com")


class DevelopmentConfig(Config):
    DEBUG = True
    # Env var override matters for tooling that needs to point at a
    # throwaway/test DB (e.g. validating a migration) without touching the
    # real dev DB — previously this was hardcoded and silently ignored any
    # SQLALCHEMY_DATABASE_URI already set in the environment.
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "SQLALCHEMY_DATABASE_URI",
        f"sqlite:///{os.path.join(INSTANCE_DIR, 'site.db')}",
    )


class ProductionConfig(Config):
    DEBUG = False
    SESSION_COOKIE_SECURE = True
    SQLALCHEMY_DATABASE_URI = _resolve_db_url()
    SQLALCHEMY_ENGINE_OPTIONS = {
        "pool_pre_ping": True,
        "pool_recycle": 280,
    }


class TestingConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False
    # Off by default so ordinary tests never trip it just from hitting
    # /login a few times across a shared-process test run; the one test
    # that actually exercises rate limiting flips this on for itself.
    RATELIMIT_ENABLED = False


config = {
    "development": DevelopmentConfig,
    "production": ProductionConfig,
    "testing": TestingConfig,
    "default": DevelopmentConfig,
}
