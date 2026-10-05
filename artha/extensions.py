from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from flask_migrate import Migrate
from flask_wtf.csrf import CSRFProtect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_login import current_user

db = SQLAlchemy()
login_manager = LoginManager()
migrate = Migrate()
csrf = CSRFProtect()
# No global default_limits — applied per-route only, so every other
# endpoint is unaffected. In-memory storage: fine for this app's scale,
# but note it's per-process, so if the web service ever runs multiple
# gunicorn workers the *effective* limit is (limit x worker count) since
# each worker tracks its own counts. A shared store (Redis) would fix
# that, not worth the added infra for a handful of users. "moving-window"
# counts over a rolling window, so a burst can't squeeze double the limit
# through by straddling a fixed window's boundary.
limiter = Limiter(key_func=get_remote_address, strategy="moving-window")


def user_or_ip_key() -> str:
    """Rate-limit key for logged-in features: per account, not per IP, so
    two people on one network don't share a budget and one person can't
    dodge a limit by switching networks. Falls back to the IP if there's
    somehow no session (those routes are @login_required anyway)."""
    if current_user and current_user.is_authenticated:
        return f"user:{current_user.id}"
    return get_remote_address()


# Every endpoint that spends real Anthropic API money gets one of these.
# Generous for real use (a person chatting manages a few messages a
# minute), but a retry loop, a stuck script, or a leaked session can no
# longer run up an unbounded bill. Tuned per feature by how often it can
# legitimately fire: the calculator's fallback fires per unresolved line
# (debounced), the bulk categorizer is one confirmed click per backlog.
AI_LIMIT_MESSAGE = "You've hit Artha's AI limit for the moment. Give it a few minutes and try again."
AI_CHAT_LIMIT = "10 per minute;200 per day"
AI_INSIGHTS_LIMIT = "5 per minute;50 per day"
AI_CALCULATOR_LIMIT = "30 per minute;500 per day"
AI_BULK_CATEGORIZE_LIMIT = "5 per minute;30 per day"
AI_IMPORT_LIMIT = "10 per minute;60 per day"


def ai_limit(limit_value: str):
    """@limiter.limit preset for the AI endpoints above: per user, with a
    message that explains what happened instead of the login form's
    "too many attempts"."""
    return limiter.limit(limit_value, key_func=user_or_ip_key, error_message=AI_LIMIT_MESSAGE)
