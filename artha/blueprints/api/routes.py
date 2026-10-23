"""JSON API for the native Artha app (iPhone and iPad today, Mac later; the
artha-apple repo).

Authentication is a per-device bearer token (models/api_token.py), never
the web app's session cookie: the app signs in once with POST /sessions,
keeps the token in the Keychain, and sends `Authorization: Bearer <token>`
on every call. Because no cookie ever authenticates these routes, the whole
blueprint is exempt from CSRF (see create_app) -- a cross-site page can't
make a browser attach a bearer token on its own.

Every figure reuses the web app's own code rather than reimplementing it:
Today comes from services/month_snapshot.py (the Dashboard's Safe to Spend
logic), and adding, editing and deleting a transaction go through
finance.routes' create_transaction, update_transaction_fields and
delete_transaction_record (the same validation, currency locking,
categorizing, remembered corrections and budget alerts as the web app).

Money is always a string with two decimals ("1284.01"), never a float, so
the app can parse it straight into a Decimal without floating-point drift.
A single transaction is always reported in its own currency; any total
across transactions is in the person's display currency, named in the
response. Same rule as the web app (see CLAUDE.md's multi-currency notes).
"""

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from functools import wraps

from flask import g, jsonify, request

from ...extensions import AI_IMPORT_LIMIT, AI_LIMIT_MESSAGE, db, limiter, user_or_ip_key
from ...models import ApiToken, Goal, Transaction, TransferAccount, User
from ...models.account import Account
from ...cli import DEFAULT_REMINDER_HOUR
from ...services.exchange_rate_service import get_rates
from ...services.screens import (
    HISTORY_MONTHS_DEFAULT,
    ScreenError,
    category_label,
    history_screen,
    plan_screen,
    serialize_account,
    serialize_transaction,
    today_screen,
    transactions_screen,
    wealth_screen,
)
from ...services.splits import SplitError
from ...services.bill_matching import CannotPair, NotAPriceChange, NotPaired
from ...services.goals import GoalError, add_to_goal, delete_goal, describe_goal, save_goal
from ...services.recurring_detect import SuggestionError, dismiss_suggestion, track_suggestion
from ...utils import CURRENCY_CODES, user_today
from ..accounts.routes import (
    ValidationError as AccountValidationError,
    delete_account_record,
    save_account,
)
from ..auth.routes import (
    AccountError,
    _verify_totp_or_recovery_code,
    change_user_password,
    disable_two_factor,
    enable_two_factor,
    register_user,
    schedule_account_deletion,
    two_factor_setup,
    update_profile,
)
from ..finance.routes import (
    TRANSACTION_CATEGORIES,
    PdfPasswordRequired,
    ValidationError,
    create_transaction,
    delete_transaction_record,
    generate_due_recurring,
    import_statement_rows,
    mark_transactions_reviewed,
    remove_category_budget,
    save_budget,
    save_category_budget,
    bill_choices,
    pair_transaction_record,
    preview_statement,
    unpair_transaction_record,
    forget_transfer_account,
    keep_new_price_record,
    split_transaction_record,
    unsplit_transaction_record,
    update_transaction_fields,
)
from ..push.routes import update_notification_preferences
from . import api_bp

log = logging.getLogger(__name__)

# How stale ApiToken.last_used_at may get before a request refreshes it,
# so reading data doesn't cost a database write on every call.
LAST_USED_REFRESH = timedelta(minutes=5)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def api_error(status: int, code: str, message: str):
    """Every API error has the same shape: a stable machine-readable
    `error` code for the app to branch on, and a `message` written for the
    person, safe to show as-is."""
    return jsonify({"error": code, "message": message}), status


def _as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def api_auth_required(view):
    """Resolves `Authorization: Bearer <token>` to the signed-in user (as
    g.api_user) or answers 401. Deliberately ignores Flask-Login and any
    session cookie: see this module's docstring."""

    @wraps(view)
    def wrapper(*args, **kwargs):
        scheme, _, raw = request.headers.get("Authorization", "").partition(" ")
        raw = raw.strip()
        if scheme.lower() != "bearer" or not raw:
            return api_error(401, "unauthorized", "Sign in to continue.")

        token = ApiToken.query.filter_by(token_hash=ApiToken.hash_token(raw)).first()
        if token is None:
            return api_error(401, "unauthorized", "You've been signed out. Sign in again to continue.")
        user = db.session.get(User, token.user_id)
        if user is None or user.deleted_at is not None:
            return api_error(401, "unauthorized", "You've been signed out. Sign in again to continue.")

        now = datetime.now(timezone.utc)
        last_used = _as_utc(token.last_used_at)
        if last_used is None or now - last_used > LAST_USED_REFRESH:
            token.last_used_at = now
            db.session.commit()

        g.api_user = user
        g.api_token = token
        return view(*args, **kwargs)

    return wrapper


def _serialize_user(user: User) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "currency": user.preferred_currency or "USD",
        "timezone": user.timezone,
        "two_factor_enabled": bool(user.otp_enabled),
        # Shared with the website's notification settings (one preference
        # per person, not per device).
        "notify_bills_due": bool(user.notify_bills_due),
        "notify_budget_alerts": bool(user.notify_budget_alerts),
        "notify_pace_heads_up": bool(user.notify_pace_heads_up),
        "reminder_hour": user.reminder_hour if user.reminder_hour is not None else DEFAULT_REMINDER_HOUR,
    }


def _catch_up_recurring(user: User, endpoint: str) -> None:
    """Same catch-up the Dashboard and Finance run before reading this
    month, so bills that came due since the last visit are counted. A
    failure here must never take the screen down with it."""
    try:
        generate_due_recurring(user)
    except Exception:
        db.session.rollback()
        log.warning("Recurring catch-up failed for user %s on /api/v1/%s", user.id, endpoint, exc_info=True)


# ---------------------------------------------------------------------------
# Signing in and out
# ---------------------------------------------------------------------------

@api_bp.post("/sessions")
@limiter.limit("5 per minute")
def create_session():
    """Sign in. Body: {"username", "password", "code"?, "device_name"?}.
    `code` (an authenticator code or a recovery code) is only needed for an
    account with two-factor sign-in on; without it, that account gets
    401 two_factor_required so the app can ask for one and try again."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = {}
    username = str(data.get("username") or "").strip()
    password = str(data.get("password") or "")
    code = str(data.get("code") or "").strip()

    user = User.query.filter_by(username=username).first() if username else None
    if user is None or not user.check_password(password):
        return api_error(401, "invalid_credentials", "That username and password don't match.")

    if user.otp_enabled:
        if not code:
            return api_error(401, "two_factor_required", "Enter the code from your authenticator app.")
        if not _verify_totp_or_recovery_code(user, code):
            return api_error(401, "invalid_code", "That code didn't work. Try the current one from your authenticator app.")

    # Same rule as the web login: signing back in during the grace window
    # is what cancels a pending account deletion.
    deletion_canceled = user.deleted_at is not None
    user.deleted_at = None
    user.last_login_at = datetime.now(timezone.utc)
    _, raw = ApiToken.issue(user, data.get("device_name") if isinstance(data.get("device_name"), str) else None)
    db.session.commit()

    return jsonify({
        "token": raw,
        "token_type": "Bearer",
        "deletion_canceled": deletion_canceled,
        "user": _serialize_user(user),
    }), 201


@api_bp.post("/users")
@limiter.limit("5 per minute; 20 per hour")
def sign_up():
    """Create an account and sign this device in, in one step. Body:
    {"email", "username", "password", "first_name"?, "currency"?,
    "timezone"?, "device_name"?}. Same rules as the web Register form
    (auth.routes.register_user); the app also sends the phone's currency
    and timezone so the new account starts out right. Returns the same
    shape as POST /sessions."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return api_error(400, "invalid_request", "Send the sign-up as a JSON object.")
    try:
        user = register_user(data)
    except AccountError as exc:
        return api_error(400, "invalid_account", str(exc))
    except Exception:
        log.error("Error during sign-up via API", exc_info=True)
        return api_error(500, "server_error", "Artha couldn't create your account. Try again in a moment.")

    user.last_login_at = datetime.now(timezone.utc)
    device_name = data.get("device_name") if isinstance(data.get("device_name"), str) else None
    _, raw = ApiToken.issue(user, device_name)
    db.session.commit()
    return jsonify({
        "token": raw,
        "token_type": "Bearer",
        "deletion_canceled": False,
        "user": _serialize_user(user),
    }), 201


@api_bp.delete("/me")
@api_auth_required
@limiter.limit("5 per minute")
def delete_me():
    """Ask for this account to be deleted. Body: {"password"}. Same as the
    web: an ACCOUNT_DELETION_GRACE_DAYS window (signing back in cancels it),
    then everything is purged. Every device is signed out, this one
    included. Returns the purge date."""
    data = _json_body() or {}
    user = g.api_user
    if not user.check_password(str(data.get("password") or "")):
        return api_error(400, "invalid_password", "That password isn't right.")
    try:
        purge_date = schedule_account_deletion(user)
    except AccountError as exc:
        return api_error(409, "cannot_delete", str(exc))
    return jsonify({"purge_date": purge_date}), 202


@api_bp.delete("/sessions/current")
@api_auth_required
def delete_session():
    """Sign this device out. The token stops working immediately."""
    db.session.delete(g.api_token)
    db.session.commit()
    return "", 204


@api_bp.get("/me")
@api_auth_required
def me():
    return jsonify({"user": _serialize_user(g.api_user)})


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

_PROFILE_FIELDS = ("first_name", "last_name", "email", "currency", "timezone")
_NOTIFICATION_FIELDS = ("notify_bills_due", "notify_budget_alerts", "notify_pace_heads_up",
                        "pace_prompt_answered", "reminder_hour")


@api_bp.patch("/me")
@api_auth_required
def update_me():
    """Body: any of {"first_name", "last_name", "email", "currency",
    "timezone", "notify_bills_due", "notify_budget_alerts",
    "notify_pace_heads_up", "pace_prompt_answered", "reminder_hour"};
    only what's sent changes. Same rules as the web Edit
    Profile form and currency picker (auth.routes.update_profile). The app
    sends the phone's timezone on its own whenever it differs, the way the
    website follows the browser's."""
    data = _json_body()
    if data is None:
        return api_error(400, "invalid_request", "Send the changes as a JSON object.")
    fields = {key: data[key] for key in _PROFILE_FIELDS if key in data}
    preferences = {key: data[key] for key in _NOTIFICATION_FIELDS if key in data}
    try:
        if preferences:
            update_notification_preferences(g.api_user, preferences)
        update_profile(g.api_user, fields)
    except (AccountError, ValueError) as exc:
        return api_error(400, "invalid_profile", str(exc))
    return jsonify({"user": _serialize_user(g.api_user)})


@api_bp.post("/me/password")
@api_auth_required
@limiter.limit("5 per minute")
def change_my_password():
    """Body: {"current_password", "new_password"}. Changing the password
    signs out every device (auth.routes.change_user_password), so this one
    gets a fresh token back and stays signed in."""
    data = _json_body() or {}
    user = g.api_user
    device_name = g.api_token.device_name
    try:
        change_user_password(user, str(data.get("current_password") or ""), str(data.get("new_password") or ""))
    except AccountError as exc:
        return api_error(400, "invalid_password", str(exc))
    _, raw = ApiToken.issue(user, device_name)
    db.session.commit()
    return jsonify({"token": raw, "token_type": "Bearer", "user": _serialize_user(user)})


@api_bp.post("/me/two-factor/setup")
@api_auth_required
def start_two_factor():
    """A new secret and its otpauth:// link, for adding Artha to an
    authenticator (the iPhone's Passwords app can take the link directly).
    Nothing is saved until POST /me/two-factor confirms a code from it."""
    try:
        secret, uri = two_factor_setup(g.api_user)
    except AccountError as exc:
        return api_error(409, "already_enabled", str(exc))
    return jsonify({"secret": secret, "uri": uri})


@api_bp.post("/me/two-factor")
@api_auth_required
@limiter.limit("5 per minute")
def turn_on_two_factor():
    """Body: {"secret", "code"}. Turns two-factor sign-in on and returns
    ten recovery codes, shown this once."""
    data = _json_body() or {}
    try:
        codes = enable_two_factor(g.api_user, str(data.get("secret") or ""), str(data.get("code") or ""))
    except AccountError as exc:
        return api_error(400, "invalid_code", str(exc))
    return jsonify({"recovery_codes": codes, "user": _serialize_user(g.api_user)})


@api_bp.delete("/me/two-factor")
@api_auth_required
@limiter.limit("5 per minute")
def turn_off_two_factor():
    """Body: {"password", "code"} (a current code or a recovery code)."""
    data = _json_body() or {}
    try:
        disable_two_factor(g.api_user, str(data.get("password") or ""), str(data.get("code") or ""))
    except AccountError as exc:
        return api_error(400, "invalid_code", str(exc))
    return jsonify({"user": _serialize_user(g.api_user)})


# ---------------------------------------------------------------------------
# Today
# ---------------------------------------------------------------------------

@api_bp.get("/today")
@api_auth_required
def today():
    """Everything the app's Today screen shows, in one call."""
    user = g.api_user
    _catch_up_recurring(user, "today")
    return jsonify(today_screen(user))


@api_bp.get("/transactions")
@api_auth_required
def list_transactions():
    """Newest first. ?limit= (default 50, at most 200) and ?offset= page
    through the rest; `next_offset` is null on the last page. ?q= keeps
    only descriptions containing that text (any case), and ?type=expense
    or ?type=income keeps only that kind. ?month=YYYY-MM keeps only that
    calendar month (the same bounds Today's totals use), and
    ?category=dining,groceries keeps only those categories ("none" means
    uncategorized), so Today's "Where October went" can open the
    transactions behind each of its numbers. ?review=1 keeps only the
    ones waiting to be reviewed (Transaction.needs_review)."""
    try:
        return jsonify(transactions_screen(g.api_user, request.args))
    except ScreenError as exc:
        return api_error(400, "invalid_request", str(exc))


@api_bp.post("/transactions")
@api_auth_required
def add_transaction():
    """Body: {"description", "amount", "type": "expense"|"income",
    "category"?, "currency"?, "date"? (YYYY-MM-DD), "is_recurring"?,
    "recurrence_interval"? ("weekly"|"biweekly"; monthly when omitted),
    "recurring_end_date"?, "source"? ("apple_pay" for a purchase the app
    logged from a Wallet automation)}. Without a category, Artha picks one
    the same way it does on the web (remembered merchant, then keywords,
    then AI)."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return api_error(400, "invalid_request", "Send the transaction as a JSON object.")

    # The web form falls back to the display currency for an unknown code,
    # which is safe there because it only offers supported ones. The app
    # can pass along whatever currency Apple Pay reported (a purchase
    # abroad), and relabeling 1,500 yen as 1,500 dollars would be far worse
    # than not logging it, so an explicit unsupported currency is refused.
    currency = data.get("currency")
    if currency is not None and str(currency).strip().upper() not in CURRENCY_CODES:
        return api_error(
            400, "unsupported_currency",
            f"Artha doesn't support {str(currency).strip().upper() or 'that currency'} yet, so this wasn't logged. "
            "Add it by hand in a supported currency.",
        )

    user = g.api_user
    try:
        tx = create_transaction(user, data)
    except ValidationError as exc:
        return api_error(400, "invalid_transaction", str(exc))
    except Exception:
        log.error("Error adding transaction via API for user %s", user.id, exc_info=True)
        return api_error(500, "server_error", "Artha couldn't save that transaction. Try again in a moment.")

    currency = user.preferred_currency or "USD"
    rates = get_rates() if currency != "USD" else None
    return jsonify({"transaction": serialize_transaction(tx, currency, rates)}), 201


def _own_transaction(transaction_id: int) -> Transaction | None:
    """The signed-in person's transaction, or None. Someone else's reads
    as missing, not forbidden, so ids can't be probed for existence."""
    tx = db.session.get(Transaction, transaction_id)
    if tx is None or tx.user_id != g.api_user.id:
        return None
    return tx


@api_bp.post("/transactions/reviewed")
@api_auth_required
def mark_reviewed():
    """Body: {"ids": [...]}: these transactions look right as they are.
    Only clears the flag; changing something instead goes through PATCH,
    which counts as a review too. Ids that aren't yours are ignored.
    Returns how many are still waiting."""
    data = _json_body() or {}
    ids = data.get("ids")
    if not isinstance(ids, list) or not all(isinstance(i, int) and not isinstance(i, bool) for i in ids):
        return api_error(400, "invalid_request", "Send the ids as a list of numbers.")
    return jsonify({"to_review": mark_transactions_reviewed(g.api_user, ids)})


@api_bp.patch("/transactions/<int:transaction_id>")
@api_auth_required
def edit_transaction(transaction_id):
    """Body: any of {"description", "amount", "type", "category", "date"
    (YYYY-MM-DD), "recurrence_interval", "recurring_end_date"}; anything
    left out stays as it is. Changing the category is remembered for that
    merchant, the same as correcting it on the web."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return api_error(400, "invalid_request", "Send the changes as a JSON object.")
    tx = _own_transaction(transaction_id)
    if tx is None:
        return api_error(404, "not_found", "That transaction doesn't exist anymore.")

    user = g.api_user
    try:
        update_transaction_fields(user, tx, data)
    except ValidationError as exc:
        return api_error(400, "invalid_transaction", str(exc))
    except Exception:
        log.error("Error editing transaction %s via API for user %s", transaction_id, user.id, exc_info=True)
        return api_error(500, "server_error", "Artha couldn't save that change. Try again in a moment.")

    currency = user.preferred_currency or "USD"
    rates = get_rates() if currency != "USD" else None
    return jsonify({"transaction": serialize_transaction(tx, currency, rates)})


@api_bp.get("/transactions/<int:transaction_id>/bills")
@api_auth_required
def transaction_bills(transaction_id):
    """The open bills this transaction could have paid, likeliest first,
    for pairing by hand (a transaction whose can_pair is true)."""
    tx = _own_transaction(transaction_id)
    if tx is None:
        return api_error(404, "not_found", "That transaction doesn't exist anymore.")
    return jsonify({"bills": bill_choices(g.api_user, tx)})


@api_bp.post("/transactions/<int:transaction_id>/pair")
@api_auth_required
def pair_transaction(transaction_id):
    """Body: {"bill_id": n}, one of GET .../bills. Pairs them like an
    automatic pairing (counted once, undone with .../unpair) and returns
    the bill, which now holds the charge; the charge's own id is gone."""
    tx = _own_transaction(transaction_id)
    if tx is None:
        return api_error(404, "not_found", "That transaction doesn't exist anymore.")
    data = _json_body() or {}
    user = g.api_user
    try:
        bill = pair_transaction_record(user, tx, data.get("bill_id"))
    except CannotPair as exc:
        return api_error(400, "cannot_pair", str(exc))
    except Exception:
        log.error("Error pairing transaction %s via API for user %s", transaction_id, user.id, exc_info=True)
        return api_error(500, "server_error", "Artha couldn't pair those. Try again in a moment.")

    currency = user.preferred_currency or "USD"
    rates = get_rates() if currency != "USD" else None
    return jsonify({"transaction": serialize_transaction(bill, currency, rates)})


@api_bp.post("/transactions/<int:transaction_id>/new-price")
@api_auth_required
def keep_new_price(transaction_id):
    """A bill charged something other than usual (its price_change isn't
    null): keep that amount as its price from now on. Returns the bill."""
    tx = _own_transaction(transaction_id)
    if tx is None:
        return api_error(404, "not_found", "That transaction doesn't exist anymore.")
    user = g.api_user
    try:
        keep_new_price_record(user, tx)
    except NotAPriceChange as exc:
        return api_error(400, "not_a_price_change", str(exc))
    except Exception:
        log.error("Error keeping a new price on %s via API for user %s", transaction_id, user.id, exc_info=True)
        return api_error(500, "server_error", "Artha couldn't save that. Try again in a moment.")
    currency = user.preferred_currency or "USD"
    rates = get_rates() if currency != "USD" else None
    return jsonify({"transaction": serialize_transaction(tx, currency, rates)})


@api_bp.post("/transactions/<int:transaction_id>/split")
@api_auth_required
def split_transaction(transaction_id):
    """Splits a purchase across categories, or changes how it's split.
    Body: {"parts": [{"amount": "60.00", "category": "groceries"}, ...]},
    at least two, adding up to the purchase exactly. Returns the purchase
    (its "split" lists the parts)."""
    tx = _own_transaction(transaction_id)
    if tx is None:
        return api_error(404, "not_found", "That transaction doesn't exist anymore.")
    user = g.api_user
    data = _json_body() or {}
    try:
        lead = split_transaction_record(user, tx, data.get("parts"))
    except SplitError as exc:
        return api_error(400, "invalid_split", str(exc))
    except Exception:
        log.error("Error splitting transaction %s via API for user %s", transaction_id, user.id, exc_info=True)
        return api_error(500, "server_error", "Artha couldn't split that. Try again in a moment.")
    currency = user.preferred_currency or "USD"
    rates = get_rates() if currency != "USD" else None
    return jsonify({"transaction": serialize_transaction(lead, currency, rates)})


@api_bp.post("/transactions/<int:transaction_id>/unsplit")
@api_auth_required
def unsplit_transaction(transaction_id):
    """Joins a split purchase back into one transaction. Returns it."""
    tx = _own_transaction(transaction_id)
    if tx is None:
        return api_error(404, "not_found", "That transaction doesn't exist anymore.")
    user = g.api_user
    try:
        joined = unsplit_transaction_record(user, tx)
    except Exception:
        log.error("Error unsplitting transaction %s via API for user %s", transaction_id, user.id, exc_info=True)
        return api_error(500, "server_error", "Artha couldn't join that back. Try again in a moment.")
    currency = user.preferred_currency or "USD"
    rates = get_rates() if currency != "USD" else None
    return jsonify({"transaction": serialize_transaction(joined, currency, rates)})


@api_bp.post("/transactions/<int:transaction_id>/unpair")
@api_auth_required
def unpair_transaction(transaction_id):
    """Splits a bill and the charge Artha paired with it (a row whose
    paired_charge isn't null) back into two transactions, the same as
    Unpair on the web. Returns both; the charge won't be paired with a
    bill on its own again."""
    tx = _own_transaction(transaction_id)
    if tx is None:
        return api_error(404, "not_found", "That transaction doesn't exist anymore.")
    user = g.api_user
    try:
        bill, charge = unpair_transaction_record(user, tx)
    except NotPaired as exc:
        return api_error(400, "not_paired", str(exc))
    except Exception:
        log.error("Error unpairing transaction %s via API for user %s", transaction_id, user.id, exc_info=True)
        return api_error(500, "server_error", "Artha couldn't split those. Try again in a moment.")

    currency = user.preferred_currency or "USD"
    rates = get_rates() if currency != "USD" else None
    return jsonify({
        "bill": serialize_transaction(bill, currency, rates),
        "charge": serialize_transaction(charge, currency, rates),
    })


@api_bp.delete("/transactions/<int:transaction_id>")
@api_auth_required
def remove_transaction(transaction_id):
    """Deleting a recurring transaction also stops its series, the same as
    on the web. The app offers undo by holding the delete back for a few
    seconds before sending it, so there's no server-side undo here."""
    tx = _own_transaction(transaction_id)
    if tx is None:
        return api_error(404, "not_found", "That transaction doesn't exist anymore.")
    try:
        delete_transaction_record(tx)
    except Exception:
        log.error("Error deleting transaction %s via API for user %s", transaction_id, g.api_user.id, exc_info=True)
        return api_error(500, "server_error", "Artha couldn't delete that. Try again in a moment.")
    return "", 204


# ---------------------------------------------------------------------------
# Importing a statement
# ---------------------------------------------------------------------------

@api_bp.post("/imports/preview")
@api_auth_required
@limiter.limit(AI_IMPORT_LIMIT, key_func=user_or_ip_key, error_message=AI_LIMIT_MESSAGE)
def preview_import():
    """Multipart form: "statement" (a PDF, CSV, OFX/QFX or QIF file) and,
    for a locked PDF, "pdf_password". Reads it exactly as the website does
    (finance.routes.preview_statement: in memory, never stored) and
    returns the rows to review; nothing is saved. Each row: date,
    description, amount (a string), type, category, category_label,
    currency, duplicate (already in Artha) and transfer (between the
    person's own accounts); start both unticked. "totals" is "match" when
    the rows add up to the statement's own totals, "mismatch" when they
    don't, null when the file doesn't print any."""
    password = request.form.get("pdf_password") or None
    try:
        result = preview_statement(g.api_user, request.files.get("statement"), password)
    except PdfPasswordRequired:
        if password is None:
            return api_error(400, "needs_password", "That PDF needs a password to open.")
        return api_error(400, "wrong_password", "That password didn't work.")
    except ValidationError as exc:
        return api_error(400, "unreadable_statement", str(exc))
    except Exception:
        log.error("Error reading a statement via API for user %s", g.api_user.id, exc_info=True)
        return api_error(500, "server_error", "Artha couldn't read that file. Try again in a moment.")

    rows = [{
        "date": row["date"],
        "description": row["description"],
        "amount": f"{Decimal(str(row['amount'])):.2f}",
        "type": row["type"],
        "category": row.get("category"),
        "category_label": category_label(row.get("category")),
        "currency": row.get("currency") or g.api_user.preferred_currency or "USD",
        "duplicate": bool(row.get("duplicate")),
        "transfer": bool(row.get("transfer")),
        # The account the line names ("SAV 7837"), when it names one.
        "account_ref": row.get("account_ref"),
    } for row in result["rows"]]
    return jsonify({
        "rows": rows, "warnings": result["warnings"], "notice": result.get("notice"),
        "totals": result.get("totals"),
    })


@api_bp.get("/me/transfer-accounts")
@api_auth_required
def transfer_accounts():
    """The accounts Artha treats as the person's own (learned from imports):
    lines to or from them start unticked as transfers."""
    accounts = TransferAccount.query.filter_by(user_id=g.api_user.id).order_by(TransferAccount.label).all()
    return jsonify({"accounts": [{"id": a.id, "label": a.label} for a in accounts]})


@api_bp.delete("/me/transfer-accounts/<int:account_id>")
@api_auth_required
def delete_transfer_account(account_id):
    if not forget_transfer_account(g.api_user, account_id):
        return api_error(404, "not_found", "That account isn't remembered anymore.")
    return "", 204


@api_bp.post("/imports")
@api_auth_required
def commit_import():
    """Body: {"rows": [...], "left_out": [...]}: the reviewed rows from
    /imports/preview to keep (edited as the person chose), and the ones
    they unticked, whose account_ref teaches Artha their own accounts. Saved exactly as the
    website saves them (finance.routes.import_statement_rows). Returns how
    many were imported and how many couldn't be."""
    data = _json_body() or {}
    user = g.api_user
    try:
        imported, skipped = import_statement_rows(user, data.get("rows"), data.get("left_out"))
    except ValidationError as exc:
        return api_error(400, "invalid_import", str(exc))
    except Exception:
        log.error("Error importing statement rows via API for user %s", user.id, exc_info=True)
        return api_error(500, "server_error", "Artha couldn't save those. Try again in a moment.")
    return jsonify({"imported": imported, "skipped": skipped})


@api_bp.get("/categories")
@api_auth_required
def categories():
    """The categories a transaction can be filed under, in the order the
    web app lists them."""
    return jsonify({
        "categories": [{"key": key, "label": meta["label"]} for key, meta in TRANSACTION_CATEGORIES.items()],
    })


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Month by month
# ---------------------------------------------------------------------------

@api_bp.get("/history")
@api_auth_required
def history():
    """The last ?months= (default 6, at most 12) calendar months, this one
    included, oldest first: what came in, what went out, and spending by
    category each month, biggest first. Months before the first one with any
    transactions are left off, so a new account doesn't open on a row of
    empty bars. `average_spent` covers only finished, full months that had
    spending (not this one, which is still going, nor a first month that
    began partway through, which carries `started`), or is null when there
    are none.
    Bucketed by services.cash_flow, the same code as the web Cash Flow
    tab."""
    try:
        count = int(request.args.get("months", HISTORY_MONTHS_DEFAULT))
    except ValueError:
        return api_error(400, "invalid_request", "months must be a whole number.")
    return jsonify(history_screen(g.api_user, count))


@api_bp.get("/plan")
@api_auth_required
def plan():
    """The month as a plan: the budget split into bills, everyday spending
    and what's left (the same math as Today's Safe to Spend, see
    services/month_plan.py), this month's bills paid and still to come,
    category limits, and what this person usually spends, to suggest
    amounts instead of making them guess."""
    user = g.api_user
    _catch_up_recurring(user, "plan")
    return jsonify(plan_screen(user))


def _json_body():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else None


@api_bp.put("/budget")
@api_auth_required
def put_budget():
    """Body: {"monthly_cap": "3400.00"}, in the person's display currency.
    Zero or blank clears the budget."""
    data = _json_body()
    if data is None:
        return api_error(400, "invalid_request", "Send the budget as a JSON object.")
    try:
        save_budget(g.api_user, data.get("monthly_cap"))
    except ValidationError as exc:
        return api_error(400, "invalid_budget", str(exc))
    except Exception:
        log.error("Error saving budget via API for user %s", g.api_user.id, exc_info=True)
        return api_error(500, "server_error", "Artha couldn't save your budget. Try again in a moment.")
    return "", 204


@api_bp.put("/category-budgets/<category>")
@api_auth_required
def put_category_budget(category):
    """Body: {"monthly_cap": "400.00", "rollover"?: true}, in the person's
    display currency. Leaving out rollover keeps what it was."""
    data = _json_body()
    if data is None:
        return api_error(400, "invalid_request", "Send the limit as a JSON object.")
    try:
        save_category_budget(g.api_user, category, data.get("monthly_cap"), data.get("rollover"))
    except ValidationError as exc:
        return api_error(400, "invalid_budget", str(exc))
    except Exception:
        log.error("Error saving category budget via API for user %s", g.api_user.id, exc_info=True)
        return api_error(500, "server_error", "Artha couldn't save that limit. Try again in a moment.")
    return "", 204


@api_bp.delete("/category-budgets/<category>")
@api_auth_required
def delete_category_budget(category):
    try:
        removed = remove_category_budget(g.api_user, category)
    except Exception:
        log.error("Error removing category budget via API for user %s", g.api_user.id, exc_info=True)
        return api_error(500, "server_error", "Artha couldn't remove that limit. Try again in a moment.")
    if not removed:
        return api_error(404, "not_found", "There's no limit on that category.")
    return "", 204


# ---------------------------------------------------------------------------
# Goals and spotted bills (Plan), through the same services/goals.py and
# services/recurring_detect.py functions as the website's Plan page.
# ---------------------------------------------------------------------------

def _own_goal(goal_id: int):
    goal = db.session.get(Goal, goal_id)
    if goal is None or goal.user_id != g.api_user.id:
        return None
    return goal


@api_bp.post("/goals")
@api_auth_required
def create_goal():
    """Body: {"name", "target_amount", "saved_amount"?, "target_date"?
    (YYYY-MM-DD)}, amounts in the person's display currency."""
    data = _json_body()
    if data is None:
        return api_error(400, "invalid_request", "Send the goal as a JSON object.")
    try:
        goal = save_goal(g.api_user, None, data)
    except GoalError as exc:
        return api_error(400, "invalid_goal", str(exc))
    return jsonify({"goal": describe_goal(goal, user_today(g.api_user))}), 201


@api_bp.patch("/goals/<int:goal_id>")
@api_auth_required
def edit_goal(goal_id):
    """Any of the create fields; only those sent change."""
    goal = _own_goal(goal_id)
    if goal is None:
        return api_error(404, "not_found", "That goal is gone.")
    data = _json_body()
    if data is None:
        return api_error(400, "invalid_request", "Send the changes as a JSON object.")
    try:
        save_goal(g.api_user, goal, data, partial=True)
    except GoalError as exc:
        return api_error(400, "invalid_goal", str(exc))
    return jsonify({"goal": describe_goal(goal, user_today(g.api_user))})


@api_bp.post("/goals/<int:goal_id>/add")
@api_auth_required
def add_money_to_goal(goal_id):
    """Body: {"amount": "50.00"}, in the goal's own currency; negative
    takes money back out (never below zero)."""
    goal = _own_goal(goal_id)
    if goal is None:
        return api_error(404, "not_found", "That goal is gone.")
    data = _json_body() or {}
    try:
        add_to_goal(goal, data.get("amount"))
    except GoalError as exc:
        return api_error(400, "invalid_amount", str(exc))
    return jsonify({"goal": describe_goal(goal, user_today(g.api_user))})


@api_bp.delete("/goals/<int:goal_id>")
@api_auth_required
def remove_goal(goal_id):
    goal = _own_goal(goal_id)
    if goal is None:
        return api_error(404, "not_found", "That goal is gone.")
    delete_goal(goal)
    return "", 204


@api_bp.post("/bill-suggestions/track")
@api_auth_required
def track_bill_suggestion():
    """Body: {"key"} from /plan's bill_suggestions. Starts tracking that
    charge as a repeating bill."""
    data = _json_body() or {}
    try:
        tx = track_suggestion(g.api_user, str(data.get("key") or ""), user_today(g.api_user))
    except SuggestionError as exc:
        return api_error(404, "not_found", str(exc))
    currency = g.api_user.preferred_currency or "USD"
    rates = get_rates() if currency != tx.native_currency else None
    return jsonify({"transaction": serialize_transaction(tx, currency, rates)})


@api_bp.post("/bill-suggestions/dismiss")
@api_auth_required
def dismiss_bill_suggestion():
    """Body: {"key"}. Stops suggesting that merchant."""
    data = _json_body() or {}
    try:
        dismiss_suggestion(g.api_user, str(data.get("key") or ""))
    except SuggestionError as exc:
        return api_error(400, "invalid_request", str(exc))
    return "", 204


# ---------------------------------------------------------------------------
# Wealth
# ---------------------------------------------------------------------------

@api_bp.get("/wealth")
@api_auth_required
def wealth():
    """Net worth now and over time, and every account behind it. The same
    accounts and math as the web Accounts page (accounts/routes.py); each
    balance stays in its own currency, with totals and history in the
    person's display currency. History is one point per day a balance
    changed (NetWorthSnapshot), plus today's live figure."""
    return jsonify(wealth_screen(g.api_user))


def _own_account(account_id: int) -> Account | None:
    account = db.session.get(Account, account_id)
    if account is None or account.user_id != g.api_user.id:
        return None
    return account


def _account_response(account: Account, status: int = 200):
    currency = g.api_user.preferred_currency or "USD"
    rates = get_rates() if currency != (account.currency or "USD") else None
    return jsonify({"account": serialize_account(account, currency, rates, datetime.now(timezone.utc))}), status


@api_bp.post("/accounts")
@api_auth_required
def add_account():
    """Body: {"name", "account_type", "currency"?, "current_balance"}. A
    balance is always positive: what's held for an asset, what's owed for
    a debt. Currency defaults to the person's display currency."""
    data = _json_body()
    if data is None:
        return api_error(400, "invalid_request", "Send the account as a JSON object.")
    user = g.api_user
    fields = dict(data)
    fields.setdefault("currency", user.preferred_currency or "USD")
    account = Account(user_id=user.id)
    try:
        save_account(user, account, fields)
    except AccountValidationError as exc:
        return api_error(400, "invalid_account", str(exc))
    except Exception:
        log.error("Error adding account via API for user %s", user.id, exc_info=True)
        return api_error(500, "server_error", "Artha couldn't save that account. Try again in a moment.")
    return _account_response(account, 201)


@api_bp.patch("/accounts/<int:account_id>")
@api_auth_required
def edit_account(account_id):
    """Body: any of {"name", "account_type", "currency", "current_balance"}.
    Sending a balance (even the same one) marks it as just updated."""
    data = _json_body()
    if data is None:
        return api_error(400, "invalid_request", "Send the changes as a JSON object.")
    account = _own_account(account_id)
    if account is None:
        return api_error(404, "not_found", "That account doesn't exist anymore.")
    try:
        save_account(g.api_user, account, data, partial=True)
    except AccountValidationError as exc:
        db.session.rollback()
        return api_error(400, "invalid_account", str(exc))
    except Exception:
        log.error("Error editing account %s via API for user %s", account_id, g.api_user.id, exc_info=True)
        return api_error(500, "server_error", "Artha couldn't save that change. Try again in a moment.")
    return _account_response(account)


@api_bp.delete("/accounts/<int:account_id>")
@api_auth_required
def remove_account(account_id):
    account = _own_account(account_id)
    if account is None:
        return api_error(404, "not_found", "That account doesn't exist anymore.")
    try:
        delete_account_record(g.api_user, account)
    except Exception:
        log.error("Error deleting account %s via API for user %s", account_id, g.api_user.id, exc_info=True)
        return api_error(500, "server_error", "Artha couldn't delete that account. Try again in a moment.")
    return "", 204
