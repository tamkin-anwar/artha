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

import calendar as cal
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from functools import wraps

from flask import g, jsonify, request

from ...extensions import db, limiter
from ...models import ApiToken, Transaction, User
from ...models.account import ACCOUNT_TYPES, Account, NetWorthSnapshot
from ...models.category_budget import CategoryBudget
from ...services.exchange_rate_service import convert_amount, convert_usd_to, get_rates
from ...services.month_plan import bills_this_month, typical_spending
from ...services.month_snapshot import build_month_snapshot
from ...utils import CURRENCY_CODES, next_due_date, user_today
from ..accounts.routes import (
    ValidationError as AccountValidationError,
    _account_balances_usd,
    delete_account_record,
    net_worth_summary,
    save_account,
)
from ..auth.routes import _verify_totp_or_recovery_code
from ..finance.routes import (
    TRANSACTION_CATEGORIES,
    ValidationError,
    category_budget_statuses,
    create_transaction,
    delete_transaction_record,
    generate_due_recurring,
    remove_category_budget,
    save_budget,
    save_category_budget,
    update_transaction_fields,
)
from . import api_bp

log = logging.getLogger(__name__)

# How stale ApiToken.last_used_at may get before a request refreshes it,
# so reading data doesn't cost a database write on every call.
LAST_USED_REFRESH = timedelta(minutes=5)

# How far ahead "Coming up" looks for recurring bills, and how many it shows.
COMING_UP_WINDOW_DAYS = 31
COMING_UP_LIMIT = 5

RECENT_LIMIT = 5
TRANSACTIONS_PAGE_DEFAULT = 50
TRANSACTIONS_PAGE_MAX = 200

_CENT = Decimal("0.01")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def api_error(status: int, code: str, message: str):
    """Every API error has the same shape: a stable machine-readable
    `error` code for the app to branch on, and a `message` written for the
    person, safe to show as-is."""
    return jsonify({"error": code, "message": message}), status


def money(value) -> str | None:
    if value is None:
        return None
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    return str(value.quantize(_CENT, rounding=ROUND_HALF_UP))


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


def _month_info(snap) -> dict:
    return {
        "name": cal.month_name[snap.today.month],
        "start": snap.month_start.date().isoformat(),
        "end": (snap.month_end - timedelta(days=1)).date().isoformat(),
        "day": snap.today.day,
        "days_in_month": snap.days_in_month,
        "days_left": snap.days_left,
    }


def _category_label(key: str | None) -> str:
    if key is None:
        return "Uncategorized"
    return TRANSACTION_CATEGORIES.get(key, {}).get("label", key)


def _serialize_transaction(tx: Transaction, display_currency: str, rates) -> dict:
    """A transaction's own amount and currency, exactly as entered. When
    that currency isn't the person's display currency, its converted
    equivalent rides along separately as `display_amount` (at the USD
    value locked when it was saved), never in place of the real amount."""
    data = {
        "id": tx.id,
        "description": tx.description,
        "amount": money(tx.amount),
        "currency": tx.native_currency,
        "type": tx.type,
        "category": tx.category,
        "category_label": _category_label(tx.category),
        "date": tx.timestamp.date().isoformat() if tx.timestamp else None,
        "is_recurring": bool(tx.is_recurring),
        "recurrence_interval": (tx.recurrence_interval or "monthly") if tx.is_recurring else None,
        # "manual", "csv" (statement import), "apple_pay", or null (older rows).
        "source": tx.import_source,
        "display_amount": None,
    }
    if tx.native_currency != display_currency:
        data["display_amount"] = money(convert_usd_to(tx.value_in_usd, display_currency, rates))
    return data


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
# Today
# ---------------------------------------------------------------------------

@api_bp.get("/today")
@api_auth_required
def today():
    """Everything the app's Today screen shows, in one call."""
    user = g.api_user
    _catch_up_recurring(user, "today")
    snap = build_month_snapshot(user)
    currency = snap.display_currency
    rates = snap.rates
    today_date = snap.today

    # Spending by day through today (bills dated later this month are in
    # the month's total but haven't happened yet, so they stay off the
    # line), and by category for the whole month so far.
    usd_by_day: dict[int, Decimal] = defaultdict(Decimal)
    usd_by_category: dict[str | None, Decimal] = defaultdict(Decimal)
    for tx in snap.transactions:
        if tx.type != "expense":
            continue
        usd_by_category[tx.category] += tx.value_in_usd
        tx_date = tx.timestamp.date() if tx.timestamp else None
        if tx_date is not None and tx_date <= today_date:
            usd_by_day[tx_date.day] += tx.value_in_usd

    running_usd = Decimal("0")
    spending_by_day = []
    for day in range(1, today_date.day + 1):
        running_usd += usd_by_day.get(day, Decimal("0"))
        spending_by_day.append({"day": day, "spent": money(convert_usd_to(running_usd, currency, rates))})

    categories = sorted(
        (
            {"key": key, "label": _category_label(key), "amount": convert_usd_to(total, currency, rates)}
            for key, total in usd_by_category.items()
        ),
        key=lambda c: c["amount"],
        reverse=True,
    )
    for c in categories:
        c["amount"] = money(c["amount"])

    coming_up = []
    for (desc, ttype), tx in snap.recurring_templates.items():
        if ttype != "expense":
            continue
        due = next_due_date(tx, today_date)
        if due is None or (due - today_date).days > COMING_UP_WINDOW_DAYS:
            continue
        coming_up.append({
            "description": desc,
            "amount": money(tx.amount),
            "currency": tx.native_currency,
            "category": tx.category,
            "due": due.isoformat(),
            "days_away": (due - today_date).days,
        })
    coming_up.sort(key=lambda b: (b["due"], b["description"]))
    coming_up = coming_up[:COMING_UP_LIMIT]

    # Only what has already happened: a recurring bill's copy is posted up
    # front for later this month (generate_due_recurring), and "Recent"
    # leading with next week's rent would read as if it were already paid.
    # Bills still ahead are what Coming up is for.
    end_of_today = datetime(today_date.year, today_date.month, today_date.day) + timedelta(days=1)
    recent_rows = (
        Transaction.query.filter(Transaction.user_id == user.id, Transaction.timestamp < end_of_today)
        .order_by(Transaction.timestamp.desc(), Transaction.id.desc())
        .limit(RECENT_LIMIT)
        .all()
    )

    safe_to_spend = None
    budget = None
    if snap.budget.get("has_budget"):
        budget = {
            "cap": money(snap.budget["cap"]),
            "spent": money(snap.budget["spent"]),
            "percent_used": round(snap.budget["pct"], 1),
            "tier": snap.budget["tier"],
        }
        amount = Decimal(str(snap.safe_to_spend))
        spendable = max(amount, Decimal("0"))
        safe_to_spend = {
            "amount": money(amount),
            "per_day": money(spendable / snap.days_left),
            "bills_still_due": money(snap.safe_to_spend_unposted),
            "over_by": money(max(-amount, Decimal("0"))),
            "status": snap.pace_status,
            "even_pace_spent": money(snap.even_pace_spent),
        }

    net_worth = net_worth_summary(user)
    if net_worth is not None:
        net_worth = {
            "amount": money(net_worth["net_worth"]),
            "assets": money(net_worth["total_assets"]),
            "liabilities": money(net_worth["total_liabilities"]),
            "account_count": net_worth["account_count"],
        }

    return jsonify({
        "date": today_date.isoformat(),
        "currency": currency,
        "month": _month_info(snap),
        "spent": money(snap.expense),
        "income": money(snap.income),
        "budget": budget,
        "safe_to_spend": safe_to_spend,
        "spending_by_day": spending_by_day,
        "categories": categories,
        "coming_up": coming_up,
        "recent": [_serialize_transaction(tx, currency, rates) for tx in recent_rows],
        "net_worth": net_worth,
    })


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------

@api_bp.get("/transactions")
@api_auth_required
def list_transactions():
    """Newest first. ?limit= (default 50, at most 200) and ?offset= page
    through the rest; `next_offset` is null on the last page. ?q= keeps
    only descriptions containing that text (any case), and ?type=expense
    or ?type=income keeps only that kind."""
    user = g.api_user
    try:
        limit = int(request.args.get("limit", TRANSACTIONS_PAGE_DEFAULT))
        offset = int(request.args.get("offset", 0))
    except ValueError:
        return api_error(400, "invalid_request", "limit and offset must be whole numbers.")
    limit = max(1, min(limit, TRANSACTIONS_PAGE_MAX))
    offset = max(0, offset)

    query = Transaction.query.filter_by(user_id=user.id)
    search = request.args.get("q", "").strip()[:100]
    if search:
        # % and _ are wildcards to LIKE; escape them so a search for
        # "50%" means the text "50%", not "50 followed by anything".
        escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        query = query.filter(Transaction.description.ilike(f"%{escaped}%", escape="\\"))
    t_type = request.args.get("type", "").strip()
    if t_type:
        if t_type not in ("expense", "income"):
            return api_error(400, "invalid_request", "type must be expense or income.")
        query = query.filter(Transaction.type == t_type)

    rows = (
        query
        .order_by(Transaction.timestamp.desc(), Transaction.id.desc())
        .offset(offset)
        .limit(limit + 1)
        .all()
    )
    has_more = len(rows) > limit
    rows = rows[:limit]

    currency = user.preferred_currency or "USD"
    rates = get_rates() if currency != "USD" else None

    return jsonify({
        "currency": currency,
        # The person's own date, so the app can tell bills posted ahead for
        # later this month apart from ones already paid without trusting
        # the phone's clock or timezone.
        "today": user_today(user).isoformat(),
        "transactions": [_serialize_transaction(tx, currency, rates) for tx in rows],
        "next_offset": offset + limit if has_more else None,
    })


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
    return jsonify({"transaction": _serialize_transaction(tx, currency, rates)}), 201


def _own_transaction(transaction_id: int) -> Transaction | None:
    """The signed-in person's transaction, or None. Someone else's reads
    as missing, not forbidden, so ids can't be probed for existence."""
    tx = db.session.get(Transaction, transaction_id)
    if tx is None or tx.user_id != g.api_user.id:
        return None
    return tx


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
    return jsonify({"transaction": _serialize_transaction(tx, currency, rates)})


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
    snap = build_month_snapshot(user)
    currency = snap.display_currency
    rates = snap.rates

    def display(usd: Decimal) -> Decimal:
        return convert_usd_to(usd, currency, rates)

    month_bills = bills_this_month(snap)
    bills = []
    for bill in month_bills.bills:
        bills.append({
            "description": bill.description,
            "amount": money(bill.amount),
            "currency": bill.currency,
            "category": bill.category,
            "category_label": _category_label(bill.category),
            "date": bill.due.isoformat(),
            "status": bill.status,
            "display_amount": money(display(bill.usd_value)) if bill.currency != currency else None,
        })

    budget = None
    breakdown = None
    if snap.budget.get("has_budget"):
        cap = Decimal(str(snap.budget["cap"]))
        budget = {
            "cap": money(cap),
            "spent": money(snap.budget["spent"]),
            "percent_used": round(snap.budget["pct"], 1),
            "tier": snap.budget["tier"],
        }
        bills_total = display(month_bills.total_usd)
        left = Decimal(str(snap.safe_to_spend))
        breakdown = {
            "bills": money(bills_total),
            "everyday_spent": money(max(snap.expense - display(month_bills.posted_usd), Decimal("0"))),
            "everyday_budget": money(max(cap - bills_total, Decimal("0"))),
            "left": money(max(left, Decimal("0"))),
            "over_by": money(max(-left, Decimal("0"))),
            "per_day": money(max(left, Decimal("0")) / snap.days_left),
            "status": snap.pace_status,
        }

    rows = CategoryBudget.query.filter_by(user_id=user.id).all()
    category_budgets = []
    for item in category_budget_statuses(rows, snap.transactions, currency, rates):
        status = item["status"]
        if not status.get("has_budget"):
            continue
        cap = Decimal(str(status["cap"]))
        spent = Decimal(str(status["spent"]))
        category_budgets.append({
            "key": item["category"],
            "label": item["label"],
            "cap": money(cap),
            "spent": money(spent),
            "left": money(max(cap - spent, Decimal("0"))),
            "over_by": money(max(spent - cap, Decimal("0"))),
            "percent_used": round(status["pct"], 1),
            "tier": status["tier"],
        })
    category_budgets.sort(key=lambda c: -c["percent_used"])
    budgeted = {c["key"] for c in category_budgets}

    typical = typical_spending(user, snap.today, currency, rates)

    return jsonify({
        "date": snap.today.isoformat(),
        "currency": currency,
        "month": _month_info(snap),
        "spent": money(snap.expense),
        "income": money(snap.income),
        "budget": budget,
        "breakdown": breakdown,
        "bills": bills,
        "bills_total": money(display(month_bills.total_usd)),
        "bills_paid": money(display(month_bills.paid_usd)),
        "bills_coming": money(display(month_bills.coming_usd)),
        "category_budgets": category_budgets,
        "unbudgeted_categories": [
            {"key": key, "label": meta["label"]}
            for key, meta in TRANSACTION_CATEGORIES.items()
            if key != "income" and key not in budgeted
        ],
        "typical": None if typical is None else {
            "months": typical["months"],
            "overall": money(typical["overall"]),
            "by_category": {key: money(value) for key, value in typical["by_category"].items()},
        },
    })


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
    """Body: {"monthly_cap": "400.00"}, in the person's display currency."""
    data = _json_body()
    if data is None:
        return api_error(400, "invalid_request", "Send the limit as a JSON object.")
    try:
        save_category_budget(g.api_user, category, data.get("monthly_cap"))
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
# Wealth
# ---------------------------------------------------------------------------

# How the app groups accounts, in the order it shows them.
ACCOUNT_GROUPS = {
    "checking": "cash", "savings": "cash",
    "investment": "investments",
    "real_estate": "property", "vehicle": "property", "other_asset": "property",
    "credit_card": "debts", "loan": "debts", "other_liability": "debts",
}


def _serialize_account(account: Account, display_currency: str, rates, now: datetime) -> dict:
    updated = _as_utc(account.balance_updated_at)
    data = {
        "id": account.id,
        "name": account.name,
        "type": account.account_type,
        "type_label": account.type_label,
        "group": ACCOUNT_GROUPS.get(account.account_type, "property"),
        "is_liability": account.is_liability,
        "balance": money(account.current_balance),
        "currency": account.currency or "USD",
        "display_balance": None,
        "updated": updated.date().isoformat() if updated else None,
        "days_since_update": (now - updated).days if updated else None,
    }
    if data["currency"] != display_currency:
        data["display_balance"] = money(
            convert_amount(account.current_balance, data["currency"], display_currency, rates)
        )
    return data


@api_bp.get("/wealth")
@api_auth_required
def wealth():
    """Net worth now and over time, and every account behind it. The same
    accounts and math as the web Accounts page (accounts/routes.py); each
    balance stays in its own currency, with totals and history in the
    person's display currency. History is one point per day a balance
    changed (NetWorthSnapshot), plus today's live figure."""
    user = g.api_user
    currency = user.preferred_currency or "USD"
    rates = get_rates()
    now = datetime.now(timezone.utc)

    accounts, assets_usd, liabilities_usd = _account_balances_usd(user.id, rates)
    assets = convert_usd_to(assets_usd, currency, rates)
    liabilities = convert_usd_to(liabilities_usd, currency, rates)

    history = []
    if accounts:
        today_date = user_today(user)
        snapshots = (
            NetWorthSnapshot.query.filter_by(user_id=user.id)
            .order_by(NetWorthSnapshot.snapshot_date.asc())
            .all()
        )
        history = [
            {"date": snap.snapshot_date.isoformat(), "net_worth": money(convert_usd_to(snap.net_worth_usd, currency, rates))}
            for snap in snapshots
            if snap.snapshot_date < today_date
        ]
        history.append({"date": today_date.isoformat(), "net_worth": money(assets - liabilities)})

    return jsonify({
        "currency": currency,
        "net_worth": money(assets - liabilities) if accounts else None,
        "assets": money(assets),
        "liabilities": money(liabilities),
        "history": history,
        "accounts": [_serialize_account(a, currency, rates, now) for a in accounts],
        "account_types": [
            {"key": key, "label": meta["label"], "is_liability": meta["liability"], "group": ACCOUNT_GROUPS[key]}
            for key, meta in ACCOUNT_TYPES.items()
        ],
        "currencies": sorted(CURRENCY_CODES),
    })


def _own_account(account_id: int) -> Account | None:
    account = db.session.get(Account, account_id)
    if account is None or account.user_id != g.api_user.id:
        return None
    return account


def _account_response(account: Account, status: int = 200):
    currency = g.api_user.preferred_currency or "USD"
    rates = get_rates() if currency != (account.currency or "USD") else None
    return jsonify({"account": _serialize_account(account, currency, rates, datetime.now(timezone.utc))}), status


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
