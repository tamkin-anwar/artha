import calendar
from types import SimpleNamespace
import csv
import io
import re
import time
import logging
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_DOWN, Decimal, InvalidOperation

from flask import render_template, redirect, url_for, request, flash, session, jsonify, Response
from flask_login import login_required, current_user
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from ...extensions import db, ai_limit, AI_BULK_CATEGORIZE_LIMIT, AI_IMPORT_LIMIT
from ...models import Transaction, User, PushSubscription, TransferAccount
from ...models.budget import Budget
from ...models.category_budget import CategoryBudget
from ...models.merchant_category_rule import MerchantCategoryRule
from ...services import bill_matching, splits, statement_reader
from ...services.exchange_rate_service import lock_usd_value, get_rates, convert_usd_to, convert_amount
from ...services.cash_flow import month_starts, monthly_totals
from ...services.push_service import send_push
from ...utils import (
    is_ajax_request,
    current_month_bounds,
    budget_status,
    next_due_date,
    recurring_occurrences,
    user_today,
    CURRENCY_CODES,
    CURRENCY_SYMBOLS,
    csv_formula_safe,
    format_money,
)
from . import finance_bp

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Categories — a small, closed set (not user-extensible). Deliberately kept
# to ~10 buckets: "broad categories that match real decisions" is the
# researched sweet spot for a personal budget — enough to be useful, few
# enough that categorizing a transaction is never a chore. A custom/
# per-user category system is a materially bigger feature (its own CRUD,
# migration of existing data, etc.) and isn't what was asked for here.
# ---------------------------------------------------------------------------

TRANSACTION_CATEGORIES = {
    "income":        {"label": "Income",             "icon": "trending-up",     "color": "#10b981"},
    "housing":       {"label": "Housing",             "icon": "home",            "color": "#6366f1"},
    "utilities":     {"label": "Utilities",           "icon": "zap",             "color": "#f59e0b"},
    "groceries":     {"label": "Groceries",           "icon": "shopping-cart",   "color": "#22c55e"},
    "dining":        {"label": "Dining",              "icon": "utensils",        "color": "#f97316"},
    "transport":     {"label": "Transport",           "icon": "car",             "color": "#0ea5e9"},
    "subscriptions": {"label": "Subscriptions",       "icon": "repeat",          "color": "#a855f7"},
    "shopping":      {"label": "Shopping",            "icon": "shopping-bag",    "color": "#ec4899"},
    "health":        {"label": "Health",              "icon": "heart-pulse",     "color": "#14b8a6"},
    "entertainment": {"label": "Entertainment",       "icon": "clapperboard",    "color": "#eab308"},
    "debt":          {"label": "Debt & loans",        "icon": "credit-card",     "color": "#ef4444"},
    "other":         {"label": "Other",               "icon": "more-horizontal", "color": "#64748b"},
}

# Not a real category — the bucket for expense/income rows with no category
# set at all, kept visually distinct (lighter gray) from the "Other" category
# above so users can tell "I chose Other" apart from "I never categorized this".
_UNCATEGORIZED = {"label": "Uncategorized", "icon": "help-circle", "color": "#94a3b8"}

# Keyword -> category, checked against a lowercased transaction description.
# Deliberately simple substring matching, not an ML/LLM call: it's free,
# instant, fully offline, and good enough for the common-merchant case that
# dominates a real bank statement. Order matters within a description only
# in the pathological case of two keywords both matching — first match in
# dict-iteration (i.e. definition) order wins, so more distinctive brand
# names are listed ahead of generic terms where that could matter.
_CATEGORY_KEYWORDS = {
    "housing": ["rent", "mortgage", "landlord", "property management"],
    "utilities": [
        "electric", "electricity", "water bill", "gas bill", "internet",
        "comcast", "xfinity", "verizon", "at&t", "att bill", "t-mobile",
        "utility", "utilities",
        # Non-US telecoms/utilities — a keyword list can never keep pace
        "vodafone", "grameenphone", "robi", "airtel", "banglalink",
        "jio", "bsnl", "bt group", "sky broadband", "telstra", "optus",
    ],
    "groceries": [
        "grocery", "groceries", "supermarket", "walmart", "target",
        "safeway", "kroger", "whole foods", "trader joe", "costco",
        "aldi", "publix",
        "tesco", "sainsbury", "asda", "morrisons", "waitrose", "lidl",
        "carrefour", "big bazaar", "shwapno", "meena bazar", "agora",
        "coles", "woolworths",
    ],
    "dining": [
        "restaurant", "starbucks", "coffee", "mcdonald", "chipotle",
        "doordash", "uber eats", "ubereats", "grubhub", "pizza", "cafe",
        "diner", "bar & grill",
        "foodpanda", "zomato", "swiggy", "deliveroo", "kfc", "burger king",
        "domino",
    ],
    "transport": [
        "uber", "lyft", "gas station", "shell", "chevron", "exxon",
        "parking", "transit", "metro", "dmv", "auto insurance",
        "grab", "careem", "ola", "bolt", "pathao", "bp fuel", "petrol",
        "toll", "railway", "train ticket",
    ],
    "subscriptions": [
        "netflix", "spotify", "hulu", "disney+", "disney plus",
        "amazon prime", "subscription", "apple.com/bill", "icloud",
        "youtube premium", "playstation plus", "xbox game pass",
        "chatgpt", "openai", "claude.ai", "anthropic",
    ],
    "shopping": [
        "amazon", "ebay", "best buy", "clothing", "mall", "ikea",
        "aliexpress", "shein", "daraz", "flipkart", "myntra", "asos",
    ],
    "health": [
        "pharmacy", "cvs", "walgreens", "doctor", "clinic", "hospital",
        "dental", "vision", "urgent care",
        "boots pharmacy", "apollo pharmacy", "lazz pharma",
    ],
    "entertainment": [
        "movie", "cinema", "amc", "concert", "ticketmaster", "steam",
        "playstation store", "xbox live",
        "star cineplex", "bookmyshow", "cineworld",
    ],
    "debt": ["loan payment", "credit card payment", "student loan"],
}



# ---------------------------------------------------------------------------
# Per-merchant categorization memory -- a user's own "I categorized this
# merchant as X" corrections, checked before the global keyword list in
# _guess_category() below. The Competitive-ambition-driven counterpart to
# Copilot Money's "trains on your own activity" categorizer (see CLAUDE.md):
# the two-tier keyword-then-AI categorizer already existed, this adds a
# tier ahead of both that's personal to the user instead of a shared global
# list.
# ---------------------------------------------------------------------------

_MERCHANT_KEY_NOISE_RE = re.compile(r"[^a-z0-9 ]+")
_MERCHANT_KEY_WORD_LIMIT = 4


def _merchant_key(description: str) -> str:
    """Normalizes a free-text description down to a stable "who was
    this" fragment for MerchantCategoryRule lookups. Not full NLP --
    just enough that "STARBUCKS #4821 SEATTLE WA" and "Starbucks #9921
    Seattle WA" land on the same key, which plain exact-string matching
    wouldn't catch: lowercase, strip punctuation, drop any token that's
    purely digits (store/transaction/reference numbers, which vary
    between visits to the same merchant regardless of how many digits
    long they are), keep the first few remaining words. Returns "" for
    a description that normalizes away to nothing, which callers treat
    as "no key to look up or remember."."""
    desc = (description or "").lower()
    desc = _MERCHANT_KEY_NOISE_RE.sub(" ", desc)
    words = [w for w in desc.split() if not w.isdigit()]
    return " ".join(words[:_MERCHANT_KEY_WORD_LIMIT])


def _load_merchant_rules(user_id: int) -> dict[str, str]:
    """One query per request/batch, not one per description -- callers
    that categorize many rows in a loop (categorize_uncategorized, the
    import preview functions) pass the resulting dict into
    _guess_category() instead of hitting the DB per row."""
    return {
        r.merchant_key: r.category
        for r in MerchantCategoryRule.query.filter_by(user_id=user_id).all()
    }


def _remembered_category_for(user_id: int, description: str) -> str | None:
    """Single-row equivalent of _load_merchant_rules, for call sites
    categorizing exactly one transaction (add_transaction) where
    fetching every rule just to look up one key would be wasteful."""
    key = _merchant_key(description)
    if not key:
        return None
    rule = MerchantCategoryRule.query.filter_by(user_id=user_id, merchant_key=key).first()
    return rule.category if rule else None


def _remember_merchant_category(user_id: int, description: str, category: str) -> None:
    """Records (or updates) this user's own correction for this
    merchant, called only where a category was *explicitly* chosen by
    a person (or an AI-Assistant proposal the person then confirmed),
    never from a keyword or bulk-AI guess -- learning from the
    categorizer's own guesses would just be circular. Caller commits;
    this only adds to the session."""
    if not category:
        return
    key = _merchant_key(description)
    if not key:
        return
    rule = MerchantCategoryRule.query.filter_by(user_id=user_id, merchant_key=key).first()
    if rule:
        rule.category = category
    else:
        db.session.add(MerchantCategoryRule(user_id=user_id, merchant_key=key, category=category))


def _guess_category(description: str, t_type: str, rules: dict[str, str] | None = None) -> str | None:
    """Best-effort category from a free-text description — used to
    pre-fill CSV-import rows so most of a statement doesn't need manual
    categorizing. Returns None (not "other") when nothing matches, so the
    caller can tell "confidently uncategorized" apart from "no guess".
    `rules` (from _load_merchant_rules) is checked first when given --
    a user's own remembered correction for this merchant outranks the
    global keyword list."""
    if t_type == "income":
        return "income"
    if rules:
        remembered = rules.get(_merchant_key(description))
        if remembered:
            return remembered
    desc = (description or "").lower()
    for category, keywords in _CATEGORY_KEYWORDS.items():
        if any(kw in desc for kw in keywords):
            return category
    return None


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

class ValidationError(Exception):
    pass


def _validate_amount(amount_str: str) -> Decimal:
    """Parse and validate a user-supplied amount string → Decimal."""
    try:
        amount = Decimal(str(amount_str))
    except InvalidOperation:
        raise ValidationError("Invalid amount format.")
    # "NaN" and "Infinity" parse as Decimals but can't be compared or stored.
    if not amount.is_finite():
        raise ValidationError("Invalid amount format.")
    if amount < 0:
        raise ValidationError("Amount must be non-negative.")
    return amount


def _resolve_transaction_timestamp(date_str: str | None, user=None) -> datetime:
    """
    Parse an optional YYYY-MM-DD date string into a datetime pinned to noon
    UTC (avoids the transaction silently landing on the "wrong" calendar
    day near a midnight UTC boundary). Falls back to today — also pinned
    to noon UTC — if the string is missing or malformed. `user` is whose
    "today" that is: the signed-in web user by default, passed explicitly
    by the native app's API (which has no Flask-Login session).
    """
    date_str = (str(date_str) if date_str is not None else "").strip()
    parsed = None
    if date_str:
        try:
            parsed = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            parsed = None
    if parsed is None:
        # The user's own local date, not the server's UTC one -- otherwise
        # a transaction added with no explicit date in the evening (US) or
        # early morning (Asia) lands on the wrong calendar day.
        parsed = user_today(user if user is not None else current_user)
    return datetime(parsed.year, parsed.month, parsed.day, 12, 0, 0, tzinfo=timezone.utc)


_RECURRENCE_INTERVALS = {"weekly", "biweekly"}


def _parse_recurrence_fields(form) -> tuple[str | None, date | None]:
    """(recurrence_interval, recurring_end_date) from a form/dict-like
    payload, both optional -- an unrecognized interval reads as None
    (monthly, the default) rather than rejecting the whole save over it,
    same leniency _validate_amount's caller-facing siblings use elsewhere
    in this file. A malformed end date is silently dropped rather than
    erroring, since it's optional by nature."""
    interval = str(form.get("recurrence_interval") or "").strip().lower()
    if interval not in _RECURRENCE_INTERVALS:
        interval = None

    end_date = None
    end_date_str = str(form.get("recurring_end_date") or "").strip()
    if end_date_str:
        try:
            end_date = datetime.strptime(end_date_str, "%Y-%m-%d").date()
        except ValueError:
            end_date = None

    return interval, end_date


def _current_month_bounds_local(today: date) -> tuple[date, date]:
    """(month_start, next_month_start) for `today` — same shape as
    utils.current_month_bounds but returning plain dates (Budget/
    CategoryBudget.alerted_month is a Date, not a datetime) and taking
    `today` as a parameter instead of calling date.today() itself, so a
    single call above each of this module's alert-check call sites can
    share one `today` with the rest of that request."""
    if today.month == 12:
        return date(today.year, 12, 1), date(today.year + 1, 1, 1)
    return date(today.year, today.month, 1), date(today.year, today.month + 1, 1)


# A pace heads-up waits until this day of the month: one big purchase on
# the 2nd says nothing yet about where the month is heading.
PACE_HEADS_UP_FROM_DAY = 5


def _check_pace_heads_up(user_id: int) -> None:
    """Sends one push when spending first runs ahead of an even pace this
    month (MonthSnapshot.pace_status "ahead": more than 5% of the budget
    past even, but not over it yet, which _check_budget_overage_alerts
    covers), for someone who turned the heads-up on. Once a month
    (Budget.pace_alerted_month), re-armed if spending drops back to an
    even pace, so drifting ahead again warns again without flapping at the
    margin. Never raises, like the overage check it runs beside."""
    from ...services.month_snapshot import build_month_snapshot

    try:
        owner = db.session.get(User, user_id)
        if owner is None or not owner.notify_pace_heads_up:
            return
        budget = Budget.query.filter_by(user_id=user_id).first()
        if budget is None or not budget.monthly_cap or budget.monthly_cap <= 0:
            return

        snap = build_month_snapshot(owner)
        month_start = date(snap.today.year, snap.today.month, 1)
        if budget.pace_alerted_month == month_start:
            even = snap.even_pace_spent
            if even is not None and Decimal(str(snap.expense)) <= even:
                budget.pace_alerted_month = None
                db.session.commit()
            return
        if snap.pace_status != "ahead" or snap.today.day < PACE_HEADS_UP_FROM_DAY:
            return

        subs = PushSubscription.query.filter_by(user_id=user_id).all()
        if not subs:
            return
        per_day = (Decimal(str(snap.safe_to_spend)) / snap.days_left).quantize(Decimal("1"), rounding=ROUND_DOWN)
        month = snap.today.strftime("%B")
        body = (f"You're ahead of pace for {month}. Up to "
                f"{format_money(per_day, snap.display_currency, whole=True)} a day keeps it on plan.")
        for sub in subs:
            if send_push(sub, title="Heads-up", body=body, url="/plan") == "gone":
                db.session.delete(sub)
        budget.pace_alerted_month = month_start
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        log.error("Error checking the pace heads-up for user %s: %s", user_id, e, exc_info=True)


def _check_budget_overage_alerts(user_id: int) -> None:
    """
    Sends one push the moment this user's overall budget, or any one of
    their category budgets, first crosses over its cap for the current
    calendar month -- not the 90%/"warning" tier utils.budget_status also
    tracks, and not on every subsequent transaction after the first one
    that tips it over (Budget.alerted_month / CategoryBudget.alerted_month
    dedupe that, the same way PushSubscription.last_notified_date dedupes
    the daily due-today job in cli.py).

    Call this right after committing any transaction save that could
    increase a user's current-month expense total -- add_transaction,
    update_transaction, generate_recurring, import_commit all do. It
    never raises: a failed push, or a user with alerts off or no
    subscriptions at all, should never surface as an error on the save
    that triggered this check.

    Every call site wants the pace heads-up checked too, so it runs first.
    """
    _check_pace_heads_up(user_id)
    try:
        owner = db.session.get(User, user_id)
        if owner is None or not owner.notify_budget_alerts:
            return

        subs = PushSubscription.query.filter_by(user_id=user_id).all()
        if not subs:
            return

        today = user_today(owner)
        month_start, month_end = _current_month_bounds_local(today)

        expenses = Transaction.query.filter(
            Transaction.user_id == user_id,
            Transaction.type == "expense",
            Transaction.timestamp >= month_start,
            Transaction.timestamp < month_end,
        ).all()
        if not expenses:
            return

        rates = get_rates()
        total_usd = sum((t.value_in_usd for t in expenses), Decimal("0"))
        category_usd: dict[str, Decimal] = defaultdict(Decimal)
        for t in expenses:
            if t.category:
                category_usd[t.category] += t.value_in_usd

        newly_over = []

        budget = Budget.query.filter_by(user_id=user_id).first()
        if budget and budget.monthly_cap and budget.monthly_cap > 0 and budget.alerted_month != month_start:
            spent = convert_usd_to(total_usd, budget.currency or "USD", rates)
            if spent >= budget.monthly_cap:
                symbol = CURRENCY_SYMBOLS.get(budget.currency or "USD", "$")
                newly_over.append(f"You're over your {symbol}{budget.monthly_cap:.2f} monthly budget.")
                budget.alerted_month = month_start

        for cb in CategoryBudget.query.filter_by(user_id=user_id).all():
            if cb.alerted_month == month_start:
                continue
            spent = convert_usd_to(category_usd.get(cb.category, Decimal("0")), cb.currency or "USD", rates)
            cap = cb.monthly_cap
            if cb.rollover:
                carry = category_rollover_carry(user_id, [cb], today, cb.currency or "USD", rates)
                cap = max(Decimal(cb.monthly_cap) + carry.get(cb.category, Decimal("0")), Decimal("0"))
            if cap > 0 and spent >= cap:
                label = TRANSACTION_CATEGORIES.get(cb.category, {}).get("label", cb.category)
                newly_over.append(f"You're over your {label} budget.")
                cb.alerted_month = month_start

        if not newly_over:
            return

        body = newly_over[0] if len(newly_over) == 1 else f"{len(newly_over)} budgets just went over: " + " ".join(newly_over)

        for sub in subs:
            result = send_push(sub, title="Budget alert", body=body, url="/plan")
            if result == "gone":
                db.session.delete(sub)

        db.session.commit()
    except Exception as e:
        db.session.rollback()
        log.error("Error checking budget overage alerts for user %s: %s", user_id, e, exc_info=True)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def _truthy(value) -> bool:
    """A form checkbox ("1"/"on" or absent) and a JSON boolean both read
    correctly; an explicit "false"/"0" from a JSON client reads as False
    instead of as a non-empty string."""
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() not in ("", "0", "false", "off", "no")


def create_transaction(user, fields) -> Transaction:
    """Validates, categorizes, currency-locks and saves one manually added
    transaction for `user`, then runs the budget-overage push check.
    `fields` is anything with .get(): the web form (add_transaction below)
    or the native app's JSON body (api/routes.py), so both go through the
    exact same rules. Raises ValidationError with a message meant for the
    person; any other exception is a real failure (already rolled back)."""
    description = str(fields.get("description") or "").strip()
    amount_str = str(fields.get("amount") if fields.get("amount") is not None else "").strip()
    t_type = str(fields.get("type") or "").strip()

    if not description:
        raise ValidationError("Description is required.")

    amount = _validate_amount(amount_str)

    if t_type not in ("income", "expense"):
        raise ValidationError("Invalid transaction type.")

    category = fields.get("category") or None
    if not isinstance(category, str) or category not in TRANSACTION_CATEGORIES:
        category = None
    # Only a real, explicit category survives past here -- remembered
    # below once the transaction is actually saved. Only the AI
    # Assistant's confirmed add_transaction proposal sends this today
    # (see ai_service.py's add_transaction tool); the plain add-forms
    # have no category field of their own.
    explicit_category = category is not None

    # Auto-categorize a manual add exactly like an imported statement row
    # already gets categorized -- same two-tier shape as import_preview():
    # a user's own remembered per-merchant correction first (see
    # _remembered_category_for), then the free/instant keyword guess
    # (also resolves "income"), then the AI fallback only for what that
    # leaves genuinely uncategorized. The add-transaction form has no
    # category field of its own today, so this is the only chance a
    # manual entry (recurring bills included, since they're created
    # through this same route) gets categorized without the user doing
    # it by hand afterward.
    if category is None:
        category = _remembered_category_for(user.id, description) or _guess_category(description, t_type)
    if category is None and t_type == "expense":
        guess_row = {"type": t_type, "description": description, "category": None}
        _fill_uncategorized_via_ai([guess_row])
        category = guess_row["category"]

    # Whatever currency the page was actively displaying at submit time
    # (static/js/currency.js's getCurrencyCode(), posted as a hidden
    # field) is the only honest signal available for a manual entry — the
    # amount was typed in while looking at that currency. An
    # unrecognized/missing value falls back the same way /set_currency
    # already does, rather than rejecting the whole transaction over it.
    currency = str(fields.get("currency") or "").strip().upper()
    if currency not in CURRENCY_CODES:
        currency = user.preferred_currency or "USD"
    usd_value, usd_rate_locked = lock_usd_value(amount, currency)

    is_recurring = _truthy(fields.get("is_recurring"))
    recurrence_interval, recurring_end_date = _parse_recurrence_fields(fields) if is_recurring else (None, None)

    # Provenance only: an Apple Pay purchase the native app logged on its
    # own, versus one someone typed in. Anything else reads as manual.
    import_source = "apple_pay" if fields.get("source") == "apple_pay" else "manual"

    timestamp = _resolve_transaction_timestamp(fields.get("date"), user)

    # The real charge for a tracked bill (Apple Pay's "NETFLIX.COM" for
    # "Netflix") lands on that bill's open occurrence instead of being
    # saved as a second expense -- see services/bill_matching.py. Not for
    # a new series someone is setting up right now (is_recurring).
    if not is_recurring:
        occurrence = bill_matching.find_open_occurrence(
            user.id, t_type, currency, amount, description, timestamp.date())
        if occurrence is not None:
            bill_matching.absorb_charge(
                occurrence, description=description, category=category,
                amount=amount, currency=currency, usd_value=usd_value,
                usd_rate_locked=usd_rate_locked, timestamp=timestamp,
                import_source=import_source, needs_review=import_source == "apple_pay")
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()
                raise
            if t_type == "expense":
                _check_budget_overage_alerts(user.id)
            return occurrence

    max_pos = (
        db.session.query(func.max(Transaction.position))
        .filter_by(user_id=user.id)
        .scalar()
        or 0
    )
    new_tx = Transaction(
        description=description,
        amount=amount,
        type=t_type,
        user_id=user.id,
        position=int(max_pos) + 1,
        timestamp=timestamp,
        is_recurring=is_recurring,
        recurrence_interval=recurrence_interval,
        recurring_end_date=recurring_end_date,
        category=category,
        import_source=import_source,
        # Logged without anyone looking at it, so it waits to be reviewed
        # (see Transaction.needs_review).
        needs_review=import_source == "apple_pay",
        currency=currency,
        usd_value=usd_value,
        usd_rate_locked=usd_rate_locked,
    )

    try:
        db.session.add(new_tx)
        if explicit_category and category is not None:
            _remember_merchant_category(user.id, description, category)
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    if t_type == "expense":
        _check_budget_overage_alerts(user.id)
    return new_tx


@finance_bp.route("/add_transaction", methods=["POST"])
@login_required
def add_transaction():
    try:
        new_tx = create_transaction(current_user, request.form)
    except ValidationError as exc:
        msg = str(exc)
        if is_ajax_request():
            return jsonify({"message": msg}), 400
        flash(msg, "error")
        return redirect(url_for("dashboard.index"))
    except Exception as e:
        log.error("Error adding transaction: %s", e, exc_info=True)
        msg = "Error adding transaction"
        if is_ajax_request():
            return jsonify({"message": msg}), 500
        flash(msg, "error")
        return redirect(url_for("dashboard.index"))

    if is_ajax_request():
        # Every page that adds over AJAX (the Add sheet, the AI Assistant's
        # confirm card) reloads or moves on rather than inserting a row.
        return jsonify({"message": "Transaction added!", "id": new_tx.id})
    flash("Transaction added!", "success")
    return redirect(url_for("dashboard.index"))


def set_recurring(tx: Transaction, on: bool) -> None:
    """Starts or stops `tx`'s series (not committed). Stopping clears the
    flag on every row sharing its (description, type) too, for the same
    reason as delete_transaction(): generate_due_recurring() picks its
    template from the most recent is_recurring=True row, so an older
    sibling still flagged would quietly restart the series."""
    tx.is_recurring = bool(on)
    if not on:
        Transaction.query.filter(
            Transaction.user_id == tx.user_id,
            Transaction.description == tx.description,
            Transaction.type == tx.type,
            Transaction.id != tx.id,
        ).update({"is_recurring": False})


def update_transaction_fields(user, tx: Transaction, data) -> Transaction:
    """Applies an edit to one of `user`'s transactions and saves it, then
    runs the budget-overage push check. `data` is the web row's autosave
    payload (update_transaction below) or the native app's JSON body
    (api/routes.py), so both follow the same rules. Raises ValidationError
    with a message meant for the person; any other exception is a real
    failure (already rolled back)."""
    if tx.split_group:
        return _update_split_purchase(user, tx, data)
    desc = str(data.get("description") or tx.description).strip()
    t_type = data.get("type") or tx.type

    amount = _validate_amount(str(data.get("amount", tx.amount)))

    if t_type not in ("income", "expense"):
        raise ValidationError("Invalid transaction type.")

    tx.description = desc
    tx.amount = amount
    tx.type = t_type

    # Only touch the date if one was actually sent — an edit to just the
    # description/amount/type shouldn't silently reset the transaction to
    # today.
    date_str = str(data.get("date") or "").strip()
    if date_str:
        tx.timestamp = _resolve_transaction_timestamp(date_str, user)

    # Same "only touch what was sent" rule as date — "category" being
    # absent from the payload (every other caller of this endpoint today)
    # must never silently clear an existing category.
    if "category" in data:
        category = data.get("category") or None
        new_category = category if isinstance(category, str) and category in TRANSACTION_CATEGORIES else None
        # An editor may send the current category along with every save,
        # not only when it was touched -- only remember when the value
        # genuinely changed, so editing an unrelated field (amount,
        # description) doesn't "re-teach" a merchant its own
        # already-stored category on every save.
        if new_category is not None and new_category != tx.category:
            _remember_merchant_category(tx.user_id, desc, new_category)
        tx.category = new_category

    # currency itself only changes if explicitly sent (no UI does this
    # today — Activity's amount field always edits the native amount in
    # its existing currency, never a converted number; see CLAUDE.md's
    # multi-currency notes). usd_value, though,
    # always gets relocked against whatever amount/currency this row ends
    # up with: `amount` above is unconditionally reassigned even when
    # unchanged, so a stale usd_value left over from the pre-edit amount
    # would otherwise silently mismatch it.
    if "currency" in data:
        new_currency = str(data.get("currency") or "").strip().upper()
        if new_currency in CURRENCY_CODES:
            tx.currency = new_currency

    # Starting or stopping the series: sent by Activity's Repeats field
    # and the app's editor, absent from everything else, which leaves the
    # series alone.
    if "is_recurring" in data:
        on = _truthy(data.get("is_recurring"))
        if on != bool(tx.is_recurring):
            set_recurring(tx, on)

    # Same "only touch what was sent" rule again -- how often and until
    # when only come from a repeating transaction's own settings, so
    # their absence must never reset an existing series back to plain
    # monthly or clear its end date. Sending an empty string for either
    # is how an editor *explicitly* clears it (back to monthly, or no
    # end date).
    if "recurrence_interval" in data or "recurring_end_date" in data:
        interval, end_date = _parse_recurrence_fields(data)
        if "recurrence_interval" in data:
            tx.recurrence_interval = interval
        if "recurring_end_date" in data:
            tx.recurring_end_date = end_date
    tx.usd_value, tx.usd_rate_locked = lock_usd_value(tx.amount, tx.native_currency)
    # Someone just looked at it and saved it: that's a review.
    tx.needs_review = False

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    if tx.type == "expense":
        _check_budget_overage_alerts(tx.user_id)
    return tx


def _update_split_purchase(user, tx: Transaction, data) -> Transaction:
    """An edit to a split purchase (services/splits.py): its name, date and
    type are the whole purchase's, so they change on every part; amounts
    and categories only change in the split itself, so an editor sending
    the amounts it shows (the purchase's total, or this part's own) is
    fine, and anything else is refused rather than quietly unbalancing
    the parts."""
    rows = splits.parts_of(tx)
    if "amount" in data:
        sent = _validate_amount(str(data.get("amount")))
        if sent not in (Decimal(tx.amount), splits.total_of(rows)):
            raise ValidationError("This purchase is split. Change its amounts in Split.")
    if _truthy(data.get("is_recurring")):
        raise ValidationError("A split purchase can't repeat. Unsplit it first.")
    if "currency" in data:
        new_currency = str(data.get("currency") or "").strip().upper()
        if new_currency and new_currency != tx.native_currency:
            raise ValidationError("A split purchase keeps its currency. Unsplit it first.")
    t_type = data.get("type") or tx.type
    if t_type not in ("income", "expense"):
        raise ValidationError("Invalid transaction type.")
    shared = {"description": str(data.get("description") or tx.description).strip(), "type": t_type, "needs_review": False}
    date_str = str(data.get("date") or "").strip()
    if date_str:
        shared["timestamp"] = _resolve_transaction_timestamp(date_str, user)
    splits.share_edit(tx, shared)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    if t_type == "expense":
        _check_budget_overage_alerts(tx.user_id)
    return splits.lead_of(tx)


def split_transaction_record(user, tx: Transaction, parts) -> Transaction:
    """Splits a purchase across categories, or changes how it's split
    (services/splits.py), for the web Activity editor and the app alike.
    Raises splits.SplitError with a message for the person; any other
    exception is a real failure (already rolled back). Returns the
    purchase's first row."""
    try:
        lead = splits.split(tx, parts, TRANSACTION_CATEGORIES)
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    if lead.type == "expense":
        _check_budget_overage_alerts(user.id)
    return lead


def unsplit_transaction_record(user, tx: Transaction) -> Transaction:
    """Joins a split purchase back into one transaction."""
    try:
        joined = splits.unsplit(tx)
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    return joined


def delete_transaction_record(tx: Transaction) -> None:
    """Deletes one transaction, stopping its recurring series if it had
    one, or every part of a split purchase (it's one purchase). Shared by
    the web route below and the native app's API. Any exception is a real
    failure (already rolled back)."""
    if tx.split_group:
        try:
            for row in splits.parts_of(tx):
                db.session.delete(row)
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise
        return
    try:
        # Deleting a recurring transaction only removes this month's row —
        # generate_recurring() picks its template from the most recent
        # is_recurring=True row for this (description, type), so an older
        # row still flagged recurring would silently regenerate this one
        # right back on the next /finance load. Deleting is the only way
        # this UI offers to cancel a recurring series (there's no separate
        # "stop recurring" action), so it has to mean "stop recurring",
        # not just "remove this one instance" — clear the flag on every
        # other row sharing this key too.
        if tx.is_recurring:
            Transaction.query.filter(
                Transaction.user_id == tx.user_id,
                Transaction.description == tx.description,
                Transaction.type == tx.type,
                Transaction.id != tx.id,
            ).update({"is_recurring": False})
        db.session.delete(tx)
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise


@finance_bp.route("/update_transaction/<int:transaction_id>", methods=["POST"])
@login_required
def update_transaction(transaction_id):
    tx = db.session.get(Transaction, transaction_id)
    if tx is None:
        return jsonify({"message": "Not found"}), 404
    if tx.user_id != current_user.id:
        return jsonify({"message": "Unauthorized"}), 403

    data = request.get_json(silent=True) or {}
    try:
        update_transaction_fields(current_user, tx, data)
    except ValidationError as exc:
        return jsonify({"message": str(exc)}), 400
    except Exception as e:
        log.error("Error updating transaction: %s", e, exc_info=True)
        return jsonify({"message": "Database error"}), 500

    return jsonify({
        "message": "Transaction updated successfully",
        "date": tx.timestamp.strftime("%Y-%m-%d"),
        "date_label": tx.timestamp.strftime("%b %d, %Y"),
        "is_recurring": tx.is_recurring,
        "recurrence_interval": tx.recurrence_interval or "monthly",
        "recurring_end_date_label": (
            "ends " + tx.recurring_end_date.strftime("%b %d, %Y") if tx.recurring_end_date else None
        ),
    })


@finance_bp.route("/delete_transaction/<int:transaction_id>", methods=["POST"])
@login_required
def delete_transaction(transaction_id):
    tx = db.session.get(Transaction, transaction_id)
    if tx is None:
        if is_ajax_request():
            return jsonify({"message": "Not found"}), 404
        flash("Transaction not found", "error")
        return redirect(url_for("dashboard.index"))

    if tx.user_id != current_user.id:
        if is_ajax_request():
            return jsonify({"message": "Unauthorized"}), 403
        flash("Unauthorized", "error")
        return redirect(url_for("dashboard.index"))

    # Store as string — Decimal is not JSON-serialisable
    session["last_deleted_tx"] = {
        "user_id": tx.user_id,
        "description": tx.description,
        "amount": str(tx.amount),
        "type": tx.type,
        "position": int(tx.position or 0),
        "is_recurring": bool(tx.is_recurring),
        "category": tx.category,
        "import_source": tx.import_source,
        "currency": tx.currency,
        "usd_value": str(tx.usd_value) if tx.usd_value is not None else None,
        "usd_rate_locked": str(tx.usd_rate_locked) if tx.usd_rate_locked is not None else None,
        "timestamp": (
            tx.timestamp.replace(tzinfo=timezone.utc).isoformat()
            if tx.timestamp
            else datetime.now(timezone.utc).isoformat()
        ),
        "deleted_at": time.time(),
    }

    try:
        delete_transaction_record(tx)
        if is_ajax_request():
            return jsonify({"message": "Transaction deleted", "can_undo": True})
        flash("Transaction deleted!", "success")
        return redirect(url_for("dashboard.index"))
    except Exception as e:
        log.error("Error deleting transaction: %s", e, exc_info=True)
        if is_ajax_request():
            return jsonify({"message": "Error deleting transaction"}), 500
        flash("Error deleting transaction", "error")
        return redirect(url_for("dashboard.index"))


@finance_bp.route("/undo_delete_transaction", methods=["POST"])
@login_required
def undo_delete_transaction():
    data = session.get("last_deleted_tx")

    if not data or data.get("user_id") != current_user.id:
        return jsonify({"message": "Nothing to undo."}), 400

    if time.time() - float(data.get("deleted_at", 0)) > 10:
        session.pop("last_deleted_tx", None)
        return jsonify({"message": "Undo window expired."}), 400

    try:
        ts = None
        try:
            ts = datetime.fromisoformat(data["timestamp"].replace("Z", "+00:00"))
        except Exception:
            ts = None

        restored_pos = int(data.get("position") or 0)
        if restored_pos <= 0:
            max_pos = (
                db.session.query(func.max(Transaction.position))
                .filter_by(user_id=current_user.id)
                .scalar()
                or 0
            )
            restored_pos = int(max_pos) + 1
        else:
            Transaction.query.filter(
                Transaction.user_id == current_user.id,
                Transaction.position >= restored_pos,
            ).update(
                {Transaction.position: Transaction.position + 1},
                synchronize_session=False,
            )

        restored = Transaction(
            description=data["description"],
            amount=Decimal(data["amount"]),
            type=data["type"],
            user_id=current_user.id,
            position=restored_pos,
            timestamp=ts or db.func.current_timestamp(),
            is_recurring=bool(data.get("is_recurring")),
            category=data.get("category"),
            import_source=data.get("import_source"),
            currency=data.get("currency"),
            usd_value=Decimal(data["usd_value"]) if data.get("usd_value") is not None else None,
            usd_rate_locked=Decimal(data["usd_rate_locked"]) if data.get("usd_rate_locked") is not None else None,
        )
        db.session.add(restored)
        db.session.commit()
        session.pop("last_deleted_tx", None)

        return jsonify({"message": "Transaction restored.", "id": restored.id})
    except Exception as e:
        db.session.rollback()
        log.error("Error undoing delete: %s", e, exc_info=True)
        return jsonify({"message": "Error restoring transaction"}), 500


@finance_bp.route("/finance/transaction/<int:transaction_id>/toggle-recurring", methods=["PATCH"])
@login_required
def toggle_recurring(transaction_id):
    tx = db.session.get(Transaction, transaction_id)
    if tx is None:
        return jsonify({"message": "Not found"}), 404
    if tx.user_id != current_user.id:
        return jsonify({"message": "Unauthorized"}), 403

    set_recurring(tx, not tx.is_recurring)

    try:
        db.session.commit()
        return jsonify({"message": "Recurring status updated", "is_recurring": tx.is_recurring})
    except Exception as e:
        db.session.rollback()
        log.error("Error toggling recurring flag: %s", e, exc_info=True)
        return jsonify({"message": "Database error"}), 500


# How far back a dormant series is allowed to catch up in one go -- a
# year of missed weekly bills or monthly ones, not every occurrence since
# a series was first created. Same "cap the backlog, never flood" idea
# _generate_recurring_events' MAX_GENERATED applies to calendar events.
_RECURRING_CATCHUP_DAYS = 366
_RECURRING_CATCHUP_MONTHS = 12


def _add_months(d: date, months: int) -> date:
    month_index = d.month - 1 + months
    return date(d.year + month_index // 12, month_index % 12 + 1, 1)


def generate_due_recurring(user) -> dict:
    """
    Create every recurring-transaction occurrence that's come due since
    each series' most recent row, each on its own original date, oldest
    first -- not just "this period's" copy. Runs on every /finance and
    Dashboard load (see generate_recurring() and dashboard.index()).

    Walking forward from the series' *latest* row, never its first one,
    is deliberate: an occurrence the user deleted on purpose sits behind
    that row and is never recreated. Lookback is capped at
    _RECURRING_CATCHUP_DAYS/_MONTHS so a long-forgotten series catches
    up on its last year, not its entire history.

    Before this, a weekly/biweekly occurrence only ever got created if
    Finance happened to be opened on its exact due day (one
    next_due_date() call from today, skipped if that landed in the
    future), and a monthly bill was lost outright for any month Finance
    wasn't opened at all.

    Dedup is unchanged: a weekly/biweekly occurrence matches on its exact
    date (recurring_month == due, or any row timestamped that day); a
    monthly one matches any transaction with the same description + type
    anywhere in that calendar month, generated or hand-entered. The
    uq_transaction_recurring_month constraint still backstops two
    near-simultaneous loads racing each other.

    A series whose next occurrence falls past its own recurring_end_date
    has is_recurring cleared on its latest row, so it stops showing as an
    active rule once it's actually finished.
    """
    uid = user.id
    today = user_today(user)
    current_month_start = _month_start(today.year, today.month)

    recurring_txs = Transaction.query.filter_by(user_id=uid, is_recurring=True).all()

    # Recurring transactions accumulate one row per period (each generated
    # copy stays is_recurring=True so it keeps showing the recurring UI).
    # Collapse to one representative row per unique (description, type) --
    # the most recent -- so a "Netflix" template with 6 months of history
    # doesn't get processed 6 times.
    templates_by_key: dict[tuple[str, str], Transaction] = {}
    for tx in recurring_txs:
        key = (tx.description, tx.type)
        current = templates_by_key.get(key)
        if current is None or (tx.timestamp and current.timestamp and tx.timestamp > current.timestamp):
            templates_by_key[key] = tx

    max_pos = (
        db.session.query(func.max(Transaction.position))
        .filter_by(user_id=uid)
        .scalar()
        or 0
    )

    generated = 0
    skipped = 0
    ended = 0

    def _exists_on_day(key, day: date) -> bool:
        day_start = datetime(day.year, day.month, day.day, 0, 0, 0)
        return db.session.query(Transaction.id).filter(
            Transaction.user_id == uid,
            Transaction.description == key[0],
            Transaction.type == key[1],
        ).filter(
            db.or_(
                Transaction.recurring_month == day,
                db.and_(Transaction.timestamp >= day_start, Transaction.timestamp < day_start + timedelta(days=1)),
            )
        ).first() is not None

    def _exists_in_month(key, month_start: date) -> bool:
        return db.session.query(Transaction.id).filter(
            Transaction.user_id == uid,
            Transaction.description == key[0],
            Transaction.type == key[1],
            Transaction.timestamp >= month_start,
            Transaction.timestamp < _add_months(month_start, 1),
        ).first() is not None

    for key, template_tx in templates_by_key.items():
        interval = template_tx.recurrence_interval
        end_date = template_tx.recurring_end_date
        last_date = template_tx.timestamp.date()
        # (occurrence date, recurring_month value) for each copy to create.
        targets: list[tuple[date, date]] = []
        # Real charges taken on as occurrences of this series instead.
        adopted: list[Transaction] = []
        series_ended = False

        if interval in ("weekly", "biweekly"):
            after_last = last_date + timedelta(days=1)
            if next_due_date(template_tx, after_last) is None:
                series_ended = True
            else:
                window_start = max(after_last, today - timedelta(days=_RECURRING_CATCHUP_DAYS))
                for due in recurring_occurrences(template_tx, window_start, today, limit=60):
                    if _exists_on_day(key, due):
                        skipped += 1
                    elif (charge := bill_matching.find_charge_for(template_tx, due)) is not None:
                        bill_matching.adopt_charge(charge, template_tx, due, due)
                        adopted.append(charge)
                        skipped += 1
                    else:
                        targets.append((due, due))
                # Matches on recurring_month == due (a prior machine-
                # generated occurrence) OR the raw timestamp actually
                # falling on `due` (the series' own anchor/template row,
                # which never gets a recurring_month itself) -- see
                # _exists_on_day. Without the second half, a template
                # whose anchor date equals a computed due date would be
                # "generated" a second time, duplicating itself.
        else:
            first_month = max(
                _add_months(_month_start(last_date.year, last_date.month), 1),
                _add_months(current_month_start, -(_RECURRING_CATCHUP_MONTHS - 1)),
            )
            month = first_month
            while month <= current_month_start:
                # Preserve the template's own day-of-month (clamped to the
                # month's length, e.g. day 31 in a 30-day month) rather
                # than stamping every copy with today's date -- same
                # clamping next_due_date() uses. The current month's copy
                # is still created up front even if its day is later this
                # month, exactly as before.
                days_in_month = calendar.monthrange(month.year, month.month)[1]
                target = date(month.year, month.month, min(template_tx.timestamp.day, days_in_month))
                if end_date and target > end_date:
                    series_ended = True
                    break
                if _exists_in_month(key, month):
                    skipped += 1
                elif (charge := bill_matching.find_charge_for(template_tx, target)) is not None:
                    # Already paid by a real charge under another name
                    # (bill_matching): that charge becomes this occurrence.
                    bill_matching.adopt_charge(charge, template_tx, month, target)
                    adopted.append(charge)
                    skipped += 1
                else:
                    targets.append((target, month))
                month = _add_months(month, 1)
            if not targets and not series_ended and first_month > current_month_start:
                # Latest row is already this month's copy -- nothing due.
                skipped += 1

        if interval in ("weekly", "biweekly") and not targets and not series_ended:
            skipped += 1

        # Same currency as the template (a recurring bill doesn't change
        # currency month to month), but usd_value is relocked fresh for
        # each copy rather than copied -- a genuinely new transaction
        # created right now locks in *this* moment's rate.
        currency = template_tx.native_currency
        # Naive and aware datetimes can't be compared (SQLite reads them
        # back naive); the date alone decides which row is newest.
        latest_row = max([template_tx, *adopted], key=lambda row: row.timestamp.replace(tzinfo=None))
        # What the bill expects next: its usual amount, unless a new price
        # is confirmed (bill_matching.expected_amount). A one-off charge
        # on the last occurrence doesn't carry forward.
        next_amount = bill_matching.expected_amount(template_tx)
        for target_date, recurring_month in targets:
            usd_value, usd_rate_locked = lock_usd_value(next_amount, currency)
            max_pos += 1
            new_tx = Transaction(
                description=template_tx.description,
                amount=next_amount,
                type=template_tx.type,
                category=template_tx.category,
                user_id=uid,
                position=int(max_pos),
                is_recurring=True,
                timestamp=datetime(target_date.year, target_date.month, target_date.day, 12, 0, 0, tzinfo=timezone.utc),
                recurring_month=recurring_month,
                recurrence_interval=interval,
                recurring_end_date=end_date,
                currency=currency,
                usd_value=usd_value,
                usd_rate_locked=usd_rate_locked,
            )
            # One savepoint per copy: uq_transaction_recurring_month is what
            # actually stops two near-simultaneous loads from both creating
            # the same occurrence, and a losing IntegrityError must be
            # skippable without discarding every other copy in this pass.
            try:
                with db.session.begin_nested():
                    db.session.add(new_tx)
                generated += 1
                latest_row = new_tx
            except IntegrityError:
                skipped += 1

        if series_ended:
            latest_row.is_recurring = False
            ended += 1

    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        log.error("Error generating recurring transactions: %s", e, exc_info=True)
        raise

    if generated:
        _check_budget_overage_alerts(uid)

    return {"generated": generated, "skipped": skipped, "ended": ended}


@finance_bp.route("/finance/generate-recurring", methods=["POST"])
@login_required
def generate_recurring():
    """Fired silently on every /finance load -- see generate_due_recurring()."""
    try:
        return jsonify(generate_due_recurring(current_user))
    except Exception:
        return jsonify({"message": "Error generating recurring transactions"}), 500


@finance_bp.get("/api/finance_totals")
@login_required
def finance_totals():
    """
    Direct DB query — the in-memory cache has been removed.

    Why: the old `finance_cache = {}` was a module-level dict that breaks
    under Gunicorn multi-worker deployments (each worker has its own copy).
    A single PostgreSQL aggregate query is fast enough for one user's data
    and is always correct across all workers.

    Scoped to the current month, same as the dashboard route that renders
    the cards this endpoint refreshes — this is only ever called to
    live-update the dashboard's stat cards/chart after a transaction is
    added/edited/deleted, so it needs to match what the page just showed
    on load, not sum all-time totals back in.
    """
    uid = current_user.id
    month_start, month_end = current_month_bounds(user_today(current_user))

    # coalesce(usd_value, amount): transactions can carry different
    # currencies now, so a raw sum of `amount` across them would be
    # meaningless. A legacy/USD row (usd_value NULL) just sums its own
    # amount unchanged — see Transaction.value_in_usd's own docstring.
    usd_amount = func.coalesce(Transaction.usd_value, Transaction.amount)
    income = (
        db.session.query(func.sum(usd_amount))
        .filter(
            Transaction.user_id == uid,
            Transaction.type == "income",
            Transaction.timestamp >= month_start,
            Transaction.timestamp < month_end,
        )
        .scalar()
        or Decimal("0")
    )
    expense = (
        db.session.query(func.sum(usd_amount))
        .filter(
            Transaction.user_id == uid,
            Transaction.type == "expense",
            Transaction.timestamp >= month_start,
            Transaction.timestamp < month_end,
        )
        .scalar()
        or Decimal("0")
    )
    # Converted to the user's own display currency — this is what the
    # dashboard's stat cards refresh from after an add/edit/delete, and
    # they need to already be in the currency it's showing everywhere
    # else, not a raw USD-pivot figure.
    display_currency = current_user.preferred_currency or "USD"
    rates = get_rates() if display_currency != "USD" else None
    income = convert_usd_to(income, display_currency, rates)
    expense = convert_usd_to(expense, display_currency, rates)
    balance = income - expense

    return jsonify({
        "income": float(income),
        "expense": float(expense),
        "balance": float(balance),
    })


# ---------------------------------------------------------------------------
# Monthly Tabs — full finance page with month-by-month filtering
# ---------------------------------------------------------------------------

def _month_start(year: int, month: int) -> date:
    return date(year, month, 1)


def _recurring_bills(uid: int, today: date) -> list[dict]:
    """Every distinct recurring "rule" (by description + type), soonest due
    first — shared by finance_page()'s panel and the Recurring tab's
    breakdown so both agree on the exact same list. Not a raw row count:
    each rule accumulates one generated row per month, so the most recent
    row per rule is the template used both for its displayed amount/
    category and for next_due_date()'s day-of-month signal — same
    dedup-then-next_due_date() pattern the dashboard's "renewals this
    week" and the calendar's upcoming-recurring banner already use, so
    all agree on the same due date for the same bill.
    """
    recurring_rows = Transaction.query.filter_by(user_id=uid, is_recurring=True).all()
    templates: dict[tuple[str, str], Transaction] = {}
    for t in recurring_rows:
        key = (t.description, t.type)
        current = templates.get(key)
        if current is None or (t.timestamp and current.timestamp and t.timestamp > current.timestamp):
            templates[key] = t

    bills = []
    for (desc, ttype), tx in templates.items():
        due = next_due_date(tx, today)
        bills.append({
            "description": desc,
            "type": ttype,
            "amount": float(tx.amount),
            "currency": tx.native_currency,
            # For summing across bills that may be in different
            # currencies (see the "recurring" view's monthly_income/
            # monthly_expense below) — "amount" above stays the exact
            # native figure for this one bill's own display.
            "usd_amount": float(tx.value_in_usd),
            "category": tx.category,
            "interval": tx.recurrence_interval or "monthly",
            "end_date": tx.recurring_end_date.strftime("%Y-%m-%d") if tx.recurring_end_date else None,
            "due_date": due,
            "due_label": (
                "Today" if due == today
                else f"{calendar.month_abbr[due.month]} {due.day}" if due
                else None
            ),
            # Matches the dashboard's "renewals this week" and the
            # calendar's upcoming-recurring banner — same 7-day window
            # counts as "soon" everywhere it's shown in the app.
            "due_soon": due is not None and 0 <= (due - today).days <= 7,
        })
    # Soonest due first. A rule with no resolvable due date now genuinely
    # happens whenever its recurring_end_date has already passed (see
    # next_due_date()'s own docstring) — sorts last rather than crashing
    # the comparison, same as the defensive "shouldn't happen" case this
    # already handled before end dates existed.
    # sorts last rather than crashing the comparison.
    bills.sort(key=lambda b: b["due_date"] or date.max)
    return bills


def months_with_transactions(uid: int, today: date) -> list[dict]:
    """Every calendar month with at least one of `uid`'s transactions,
    newest first, as {"value": "2026-10", "label": "Oct 2026"}, for month
    pickers (Reports' periods, Activity's filter). This month is always
    offered, even before anything is in it."""
    year_col = func.extract("year", Transaction.timestamp)
    month_col = func.extract("month", Transaction.timestamp)
    rows = (
        db.session.query(year_col, month_col)
        .filter(Transaction.user_id == uid, Transaction.timestamp.isnot(None))
        .distinct()
        .all()
    )
    keys = sorted({(int(y), int(m)) for y, m in rows} | {(today.year, today.month)}, reverse=True)
    return [{"value": f"{y:04d}-{m:02d}", "label": f"{calendar.month_abbr[m]} {y}"} for y, m in keys]


@finance_bp.route("/finance")
@login_required
def finance_page():
    """Reports: Spending, Income, Cash Flow and Recurring, each fetched on
    demand from /finance/breakdown for any month, quarter-ish window or
    year. The page used to carry an Overview (budget, savings rate) and a
    full Transactions list too; since the 2026-10 redesign those live on
    Plan and Activity, so this only needs the period pickers' options.
    ?tab=spending|income|cashflow|recurring opens that tab."""
    uid = current_user.id
    today = user_today(current_user)

    available_months = months_with_transactions(uid, today)
    available_years = sorted({int(m["value"][:4]) for m in available_months}, reverse=True)

    return render_template(
        "finance.html",
        today_date=today.strftime("%Y-%m-%d"),
        categories=TRANSACTION_CATEGORIES,
        available_years=available_years,
        available_months=available_months,
    )


# ---------------------------------------------------------------------------
# Cash Flow / Spending / Income tabs — period-flexible aggregation, fetched
# on demand (JSON) so switching tabs or periods never reloads the page.
# Three "views" share one endpoint since they all start from the same
# date-bounded transaction query:
#   - cashflow: income vs. expense per month bucket within the period
#   - spending: expense total broken down by category
#   - income:   income total broken down by category
# ---------------------------------------------------------------------------

def _period_bounds(period: str, year: int, today: date, month_str: str | None = None) -> tuple[date, date, str]:
    """(start, end_exclusive, label) for a period key.

    "3m"/"6m"/"12m" are trailing windows anchored to today (matches how
    banking apps show "Last 3 Months" — always relative to now, not to
    whatever year is selected). "year" reads the `year` param, and "month"
    reads `month_str` ("YYYY-MM") — the two periods that let a user look
    back at a specific past year or a specific past month rather than only
    ever seeing today's.
    """
    if period == "month":
        target = _month_start(today.year, today.month)
        if month_str:
            try:
                m_year, m_month = (int(part) for part in month_str.split("-", 1))
                target = _month_start(m_year, m_month)
            except (ValueError, TypeError):
                pass
        start = target
        end = _month_start(target.year + 1, 1) if target.month == 12 else _month_start(target.year, target.month + 1)
        return start, end, f"{calendar.month_name[target.month]} {target.year}"

    if period in ("3m", "6m", "12m"):
        n = {"3m": 3, "6m": 6, "12m": 12}[period]
        end = _month_start(today.year + 1, 1) if today.month == 12 else _month_start(today.year, today.month + 1)
        cursor_year, cursor_month = today.year, today.month
        for _ in range(n - 1):
            cursor_month -= 1
            if cursor_month == 0:
                cursor_month = 12
                cursor_year -= 1
        start = _month_start(cursor_year, cursor_month)
        return start, end, f"Last {n} Months"

    # "year" (and any unrecognized value, so the route never 500s on a bad param).
    # A past year runs Jan-Dec in full; the current year stops at the end of
    # *this* month rather than running out to December, so "2026" doesn't
    # render 4 empty trailing months — it's year-to-date, same as every
    # other period here being "as of today" rather than a fixed future window.
    start = date(year, 1, 1)
    if year == today.year:
        end = _month_start(today.year + 1, 1) if today.month == 12 else _month_start(today.year, today.month + 1)
        label = f"{year} (Year to Date)"
    else:
        end = date(year + 1, 1, 1)
        label = str(year)
    return start, end, label


@finance_bp.route("/finance/breakdown")
@login_required
def finance_breakdown():
    uid = current_user.id
    # Every total this endpoint returns gets converted to this, right
    # where each is finalized below, same "USD internally, converted once
    # right before use" pattern as finance_page() — a fixed rate fetched
    # once per request rather than once per view/bucket.
    display_currency = current_user.preferred_currency or "USD"
    rates = get_rates() if display_currency != "USD" else None
    view = (request.args.get("view") or "spending").strip().lower()
    period = (request.args.get("period") or "month").strip().lower()
    today = user_today(current_user)
    try:
        year = int(request.args.get("year") or today.year)
    except (TypeError, ValueError):
        year = today.year
    month_param = (request.args.get("month") or "").strip()

    if view not in ("spending", "income", "cashflow", "recurring"):
        view = "spending"
    if period not in ("month", "3m", "6m", "12m", "year"):
        period = "month"

    start, end, period_label = _period_bounds(period, year, today, month_param or None)

    if view == "recurring":
        # Recurring rules aren't bounded by a real transaction date range
        # the way the other three views are — there's nothing to query by
        # date, since a rule always describes "the current commitment,"
        # not a historical record. Instead the period picker here scales
        # a *projection*: the number of calendar months between `start`
        # and `end` (1 for a month, 3/6/12 for those windows, and — reusing
        # the exact same _period_bounds() "Year to Date" logic Cash Flow
        # already has — however many months have elapsed for the current
        # year, or a full 12 for a past one) multiplied by the monthly
        # commitment, so "Last 3 Months" reads as "3 months of this bill
        # load," consistent with what every other period label means
        # elsewhere on this page.
        months = (end.year - start.year) * 12 + (end.month - start.month)
        bills = _recurring_bills(uid, today)
        # usd_amount, not amount: bills can be in different currencies,
        # so summing native amounts across them would be meaningless —
        # each bill's own row below still shows its exact native amount.
        # Converted to display_currency once here since it's what
        # income_total/expense_total/net below actually return.
        monthly_income = float(convert_usd_to(
            Decimal(str(sum(b["usd_amount"] for b in bills if b["type"] == "income"))), display_currency, rates
        ))
        monthly_expense = float(convert_usd_to(
            Decimal(str(sum(b["usd_amount"] for b in bills if b["type"] == "expense"))), display_currency, rates
        ))
        items = [
            {
                "description": b["description"],
                "type": b["type"],
                "amount": b["amount"],
                "currency": b["currency"],
                "category": b["category"],
                "due_label": b["due_label"],
                "due_soon": b["due_soon"],
            }
            for b in bills
        ]
        return jsonify({
            "view": view,
            "period": period,
            "period_label": period_label,
            "months": months,
            "monthly_income": monthly_income,
            "monthly_expense": monthly_expense,
            "income_total": monthly_income * months,
            "expense_total": monthly_expense * months,
            "net": (monthly_income - monthly_expense) * months,
            "items": items,
        })

    if view == "cashflow":
        # Bucketed by services.cash_flow, the same code the native app's
        # month-by-month view uses (GET /api/v1/history).
        buckets = []
        income_total = Decimal("0")
        expense_total = Decimal("0")
        month_count = len(month_starts(start, end))
        for m in monthly_totals(uid, start, end):
            income = convert_usd_to(m.income_usd, display_currency, rates)
            expense = convert_usd_to(m.expense_usd, display_currency, rates)
            income_total += income
            expense_total += expense
            buckets.append({
                "label": f"{calendar.month_abbr[m.start.month]} {m.start.year}" if month_count > 12 else calendar.month_abbr[m.start.month],
                "income": float(income),
                "expense": float(expense),
                "net": float(income - expense),
            })

        return jsonify({
            "view": view,
            "period": period,
            "period_label": period_label,
            "buckets": buckets,
            "income_total": float(income_total),
            "expense_total": float(expense_total),
            "net": float(income_total - expense_total),
        })

    start_dt = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
    end_dt = datetime(end.year, end.month, end.day, tzinfo=timezone.utc)

    txs = Transaction.query.filter(
        Transaction.user_id == uid,
        Transaction.timestamp >= start_dt,
        Transaction.timestamp < end_dt,
    ).all()

    # spending / income — same shape, just filtered to the opposite tx type.
    want_type = "expense" if view == "spending" else "income"
    totals: dict[str, Decimal] = defaultdict(Decimal)
    for t in txs:
        if t.type != want_type:
            continue
        key = t.category if (t.category and t.category in TRANSACTION_CATEGORIES) else "_uncategorized"
        totals[key] += t.value_in_usd

    # Percentages are ratios (scale-invariant, correct either way) but the
    # amounts themselves are what's actually rendered — convert those.
    total = sum(totals.values(), Decimal("0"))
    categories = []
    for key, amount in sorted(totals.items(), key=lambda kv: kv[1], reverse=True):
        meta = _UNCATEGORIZED if key == "_uncategorized" else TRANSACTION_CATEGORIES[key]
        pct = float((amount / total) * 100) if total > 0 else 0.0
        categories.append({
            "key": key,
            "label": meta["label"],
            "icon": meta["icon"],
            "color": meta["color"],
            "amount": float(convert_usd_to(amount, display_currency, rates)),
            "pct": pct,
        })

    return jsonify({
        "view": view,
        "period": period,
        "period_label": period_label,
        "total": float(convert_usd_to(total, display_currency, rates)),
        "categories": categories,
    })


# ---------------------------------------------------------------------------
# CSV import — bank statement upload. One generic, bank-agnostic parser
# (not a per-bank integration) plus a client-side preview/edit step as the
# safety net for whatever the parser gets wrong, rather than trying to
# perfectly special-case every bank's export format up front.
# ---------------------------------------------------------------------------

_IMPORT_DATE_FORMATS = [
    "%Y-%m-%d",
    "%m/%d/%Y",
    "%d/%m/%Y",
    "%m-%d-%Y",
    "%d-%m-%Y",
    "%b %d, %Y",
    "%d %b %Y",
    "%d %B %Y",
    "%d-%b-%Y",
    "%m/%d/%y",
    "%d/%m/%y",
]
# Same formats, slash/dash ones reordered day-first. A bare "03/04" is
# genuinely ambiguous (March 4 or April 3?) with nothing in the text to
# settle it — used instead of _IMPORT_DATE_FORMATS, whole-document, when
# _detect_day_first finds a currency symbol whose country overwhelmingly
# writes dates day-first (see that function).
_IMPORT_DATE_FORMATS_DAY_FIRST = [
    "%Y-%m-%d",
    "%d/%m/%Y",
    "%m/%d/%Y",
    "%d-%m-%Y",
    "%m-%d-%Y",
    "%b %d, %Y",
    "%d %b %Y",
    "%d %B %Y",
    "%d-%b-%Y",
    "%d/%m/%y",
    "%m/%d/%y",
]

# Header names banks use, in English and the languages statements most
# often arrive in. Compared after lower-casing and dropping anything in
# brackets ("Amount (USD)", "Betrag (EUR)").
_DATE_HEADER_ALIASES = {
    "date", "transaction date", "posted date", "posting date", "trans date", "post date", "value date",
    "booking date", "completed date", "started date", "date posted", "txn date", "tran date",
    "datum", "buchungstag", "buchungsdatum", "valuta", "wertstellung", "fecha", "fecha operación",
    "fecha operacion", "fecha valor", "date opération", "date operation", "date d'opération",
    "date de valeur", "data", "data operazione", "data contabile", "data valuta", "data movimento",
    "transactiedatum", "boekdatum", "datum transactie",
}
_DESC_HEADER_ALIASES = {
    "description", "memo", "narrative", "narration", "details", "payee", "merchant", "particulars",
    "transaction description", "transaction details", "name", "beneficiary", "counterparty", "reference",
    "verwendungszweck", "buchungstext", "beschreibung", "empfänger", "auftraggeber/empfänger",
    "zahlungsempfänger*in", "concepto", "descripción", "descripcion", "libellé", "libelle",
    "libellé opération", "détail", "descrizione", "causale", "omschrijving", "naam / omschrijving",
    "naam/omschrijving", "descrição", "descricao", "histórico", "historico",
}
# Picked over a "Reference" or "Name" column when a file has both.
_DESC_HEADER_PREFERRED = {
    "description", "transaction description", "transaction details", "details", "narrative", "narration",
    "particulars", "memo", "verwendungszweck", "buchungstext", "concepto", "libellé", "libelle",
    "descrizione", "omschrijving", "descripción", "descripcion", "descrição", "descricao",
}
_AMOUNT_HEADER_ALIASES = {
    "amount", "transaction amount", "amt", "betrag", "umsatz", "importe", "montant", "importo",
    "bedrag", "valor", "montante", "net amount",
}
_DEBIT_HEADER_ALIASES = {
    "debit", "withdrawal", "withdrawals", "money out", "dr", "paid out", "debit amount",
    "withdrawal amount", "soll", "belastung", "cargo", "cargos", "débit", "addebiti", "addebito",
    "af", "uitgaven", "débito", "debito",
}
_CREDIT_HEADER_ALIASES = {
    "credit", "deposit", "deposits", "money in", "cr", "paid in", "credit amount", "deposit amount",
    "haben", "gutschrift", "abono", "abonos", "crédit", "accrediti", "accredito", "bij", "inkomsten",
    "crédito", "credito",
}
# A column saying which way each (unsigned) amount went: "Debit"/"Credit",
# "DR"/"CR", Apple Card's "Purchase"/"Payment".
_DIRECTION_HEADER_ALIASES = {
    "type", "transaction type", "credit/debit", "debit/credit", "cr/dr", "dr/cr", "debit or credit",
    "soll/haben", "s/h", "direction", "transaktionsart",
}
_DIRECTION_OUT = {"debit", "dr", "d", "withdrawal", "purchase", "sale", "soll", "s", "fee", "charge", "out", "payment sent"}
_DIRECTION_IN = {"credit", "cr", "c", "deposit", "payment", "refund", "return", "haben", "h", "in", "payment received"}
_CURRENCY_HEADER_ALIASES = {"currency", "währung", "devise", "moneda", "valuta della transazione", "valuta transazione", "munt"}

# £ (UK), € (most of the Eurozone) and ৳ (Bangladesh) all overwhelmingly
# write dates day-first. Deliberately NOT keying off a bare "$" — that
# currency spans the US, Canada, Australia and others with no single
# dominant order, so a $-only statement keeps the existing US-first
# default rather than risk flipping it wrong for the far more common USD
# case. Only a fallback now — see _detect_day_first — since plenty of
# banks spell the currency out as text ("BDT") rather than printing its
# Unicode symbol, which this alone would miss entirely.
_DAY_FIRST_CURRENCY_SYMBOLS = ("£", "€", "৳")
_SLASH_DATE_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/\d{2,4}\b")


def _detect_day_first(text: str) -> bool:
    """
    Whole-document hint for which way to resolve a genuinely ambiguous
    slash date (both parts <=12, e.g. "03/08" — March 8 or August 3?).
    A single statement never mixes date conventions within itself, so one
    answer covers the whole document. Two signals, tried in order:

    1. Any UNAMBIGUOUS slash date elsewhere in the same statement — one
       where a part is 13-31, which can only ever be a day, never a
       month. This is the strongest possible signal because it comes
       from the statement's own data rather than a guess, and it
       generalizes to any bank/currency without a hardcoded list —
       critical, since a currency-symbol check alone misses every bank
       that spells its currency out as text ("BDT") rather than
       printing the Unicode symbol, which is what actually broke this
       for a real Bangladeshi bank's statement (see test coverage).
    2. A currency symbol from a day-first-writing country (see
       _DAY_FIRST_CURRENCY_SYMBOLS) — only reached when the statement
       has no unambiguous date to go on at all (e.g. one that happens
       to cover only the 1st-12th of a month).

    Defaults to False (US month-first) if neither signal applies.
    """
    for first_str, second_str in _SLASH_DATE_RE.findall(text):
        first, second = int(first_str), int(second_str)
        if first > 12 and second <= 12:
            return True  # day/month/year
        if second > 12 and first <= 12:
            return False  # month/day/year
        # Both <=12 (ambiguous) or both >12 (malformed) — inconclusive,
        # keep scanning for a date elsewhere that actually settles it.

    return any(sym in text for sym in _DAY_FIRST_CURRENCY_SYMBOLS)


# Ordered so an unambiguous symbol (£/€/৳ — each essentially one country's
# currency) is checked before a bare "$", which spans the US, Canada, and
# Australia with no dominant country — the exact same asymmetry
# _DAY_FIRST_CURRENCY_SYMBOLS already relies on for date-order detection,
# reused here rather than re-litigated.
_CURRENCY_SYMBOL_TO_CODE = {"£": "GBP", "€": "EUR", "৳": "BDT"}
_CURRENCY_SYMBOL_PRIORITY = ("£", "€", "৳")


def _detect_import_currency(text: str, fallback: str) -> str:
    """Whole-document currency guess, same one-scan-covers-the-whole-
    statement idea as _detect_day_first (a bank export never mixes
    currencies mid-document) — and the same deliberate exclusion of a
    bare "$" as a signal: it spans the US, Canada, and Australia with no
    single dominant currency, so seeing one tells us nothing reliable
    (see _DAY_FIRST_CURRENCY_SYMBOLS's own comment for the identical
    reasoning about date order). Falls back to the caller-supplied
    default (the importing user's own preferred_currency, or "USD") for
    a $-only or symbol-less statement — a statement spelling its
    currency out as text ("BDT") rather than printing the symbol isn't
    caught here either; a real gap, but a smaller one than guessing
    wrong, and the preview step still shows it before anything saves."""
    for symbol in _CURRENCY_SYMBOL_PRIORITY:
        if symbol in text:
            return _CURRENCY_SYMBOL_TO_CODE[symbol]
    return fallback


def _parse_import_date(raw: str, day_first: bool = False) -> date | None:
    """
    Tries each statement date format banks commonly export in, in order.
    Ambiguous day/month ordering (e.g. "03/04") is resolved US-first by
    default, day-first when `day_first` is set (see _detect_day_first) —
    and the preview step (every parsed date is shown before anything is
    saved) is what catches anything still wrong, not this function.
    Critically, this NEVER falls back to today: a row whose date can't be
    parsed is skipped (see _parse_statement_csv), not silently dated to
    the day of the upload — a March statement imported in July must file
    under March, or the whole point of importing history is defeated.
    """
    raw = (raw or "").strip()
    # "2026-09-05 14:22:01" or "2026-09-05T14:22:01Z" (Revolut, Wise,
    # Monzo): the time doesn't matter here.
    raw = re.sub(r"(?<=\d)[T ]\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?\s*(?:Z|[AP]M|[+-]\d{2}:?\d{2})?$", "", raw).strip()
    if not raw:
        return None
    formats = _IMPORT_DATE_FORMATS_DAY_FIRST if day_first else _IMPORT_DATE_FORMATS
    for fmt in formats:
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    # Everything else banks print: 05.09.2026, 2026/09/05, 5 sept. 2026,
    # 2026年9月5日 (statement_reader.parse_date).
    return statement_reader.parse_date(raw, day_first)


def _normalize_header(h: str) -> str:
    h = re.sub(r"\(.*?\)|\[.*?\]", "", (h or "").strip().strip('"').lstrip("\ufeff")).strip().lower()
    return re.sub(r"\s+", " ", h)


def _detect_import_columns(header: list[str]) -> dict:
    """Maps a CSV header row to column indices by role, matching common
    bank export header names case-insensitively. A role missing from the
    return dict means it wasn't found — the caller decides what's required."""
    normalized = [_normalize_header(h) for h in header]
    roles: dict[str, int] = {}
    for role, aliases in (
        ("date", _DATE_HEADER_ALIASES), ("description", _DESC_HEADER_PREFERRED), ("description", _DESC_HEADER_ALIASES),
        ("amount", _AMOUNT_HEADER_ALIASES), ("debit", _DEBIT_HEADER_ALIASES),
        ("credit", _CREDIT_HEADER_ALIASES), ("direction", _DIRECTION_HEADER_ALIASES),
        ("currency", _CURRENCY_HEADER_ALIASES),
    ):
        if role in roles:
            continue
        for i, h in enumerate(normalized):
            if h in aliases and i not in roles.values():
                roles[role] = i
                break
    return roles


def _parse_import_amount(raw: str) -> Decimal | None:
    """A statement amount as a signed Decimal, written any way banks write
    one: $1,234.56, 1.234,56 €, (12.00), 12.00-, 12.00 DR (see
    statement_reader.parse_amount)."""
    return statement_reader.parse_amount(raw)[0]


def _parse_statement_csv(file_stream, fallback_currency: str = "USD") -> tuple[list[dict], list[str]]:
    """
    Parses an uploaded bank-statement CSV into row dicts, each carrying
    its own statement date (see _parse_import_date's docstring on why
    that's never defaulted) and a best-effort category guess. Rows with
    an unparseable date/amount are excluded and reported as warnings
    rather than silently dropped or defaulted. Returns (rows, warnings);
    nothing is written to the database here.

    Reads exports from banks anywhere: comma, semicolon (most of Europe,
    where the comma is the decimal point), tab or pipe separated; with a
    few lines of account details above the header row; headers in the
    main European languages; one signed amount, separate debit and
    credit columns, or an unsigned amount plus a column saying which way
    it went; and a currency column, when a multi-currency account has one.
    """
    raw = file_stream.read()
    if isinstance(raw, str):
        text = raw
    else:
        text = None
        for encoding in ("utf-8-sig", "cp1252", "latin-1"):
            try:
                text = raw.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        if text is None:
            return [], ["Could not read file. Please export it as CSV (UTF-8 or Latin-1)."]

    day_first = _detect_day_first(text)
    currency = _detect_import_currency(text, fallback_currency)

    if not text.strip():
        return [], ["File is empty."]
    # The separator that turns up a real header row wins; Python's own
    # sniffer gives up on a file with account details above the header.
    sample = "\n".join(text.splitlines()[:40])
    try:
        sniffed = [csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter]
    except csv.Error:
        sniffed = []
    all_rows, header_index, roles = [], 0, {}
    for delimiter in sniffed + [",", ";", "\t", "|"]:
        candidate_rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
        for i, candidate in enumerate(candidate_rows[:25]):
            found = _detect_import_columns(candidate)
            if "date" in found and ("description" in found or "amount" in found or "debit" in found):
                all_rows, header_index, roles = candidate_rows, i, found
                break
        if roles:
            break
    if not all_rows:
        all_rows = list(csv.reader(io.StringIO(text)))
        roles = _detect_import_columns(all_rows[0]) if all_rows else {}

    if "date" not in roles or "description" not in roles:
        return [], [
            "Couldn't find date and description columns. Expected headers like "
            "\"Date\", \"Description\", and \"Amount\" (or \"Debit\"/\"Credit\")."
        ]
    if "amount" not in roles and "debit" not in roles and "credit" not in roles:
        return [], ["Couldn't find an amount column (\"Amount\", or \"Debit\"/\"Credit\")."]

    warnings: list[str] = []
    rows: list[dict] = []
    for line_num, raw_row in enumerate(all_rows[header_index + 1:], start=header_index + 2):
        if not raw_row or all(not cell.strip() for cell in raw_row):
            continue

        def cell(role: str) -> str:
            idx = roles.get(role)
            return raw_row[idx].strip() if idx is not None and idx < len(raw_row) else ""

        parsed_date = _parse_import_date(cell("date"), day_first=day_first)
        description = cell("description")
        if parsed_date is None or not description:
            warnings.append(f"Row {line_num}: skipped, missing or unreadable date or description.")
            continue

        amount = None
        if "amount" in roles and cell("amount"):
            value, explicit = statement_reader.parse_amount(cell("amount"))
            if value is not None:
                direction = cell("direction").strip().lower()
                if direction in _DIRECTION_OUT and value > 0:
                    value = -value
                elif direction in _DIRECTION_IN and value < 0:
                    value = -value
                amount, t_type = abs(value), ("expense" if value < 0 else "income")
        if amount is None and ("debit" in roles or "credit" in roles):
            debit = _parse_import_amount(cell("debit"))
            credit = _parse_import_amount(cell("credit"))
            if debit:
                amount, t_type = abs(debit), "expense"
            elif credit:
                amount, t_type = abs(credit), "income"
        if amount is None:
            warnings.append(f"Row {line_num}: skipped, no amount.")
            continue
        if amount == 0:
            continue

        row_currency = cell("currency").upper()
        if not re.fullmatch(r"[A-Z]{3}", row_currency) or row_currency not in CURRENCY_CODES:
            row_currency = currency

        rows.append({
            "date": parsed_date.strftime("%Y-%m-%d"),
            "description": description,
            "amount": float(amount),
            "type": t_type,
            "category": _guess_category(description, t_type),
            "currency": row_currency,
        })

    return rows, warnings


_MONEY = statement_reader.MONEY
# Any date a statement prints at the start of a line: 09/08, 09/08/26,
# 05.09.2026, 2026-09-05, 05 Sep, 5-Sep-26, Sep 5, 2026年9月5日, in English
# and the main European languages (statement_reader.DATE).
_PDF_DATE_PATTERN = statement_reader.DATE
# Some banks (Bank Asia, among others printing a numbered ledger rather than
# a plain table) prefix every row with its own row index before the date —
# "2 27/07/2026 VISA-POS ... 2,640.00 0.00 60,282.62" — which would
# otherwise break the ^(date) anchor below on every single line. Optional,
# so it's a no-op for every statement that doesn't do this. Card
# statements often print a second (posting) date right after the first;
# that's optional too, and the first date is the one kept.
_PDF_ROW_NUM_PREFIX = r"(?:\d+\s+)?"
_PDF_LEAD = rf"^{_PDF_ROW_NUM_PREFIX}({_PDF_DATE_PATTERN})(?:\s+{_PDF_DATE_PATTERN})?\s+"
# Separate withdraw/deposit columns plus a running balance — "19-Sep-2024
# CARD ANNUAL FEE **8562 FOR 2024-25 600.00 0.00 4,400.00" — instead of one
# signed amount. Whichever of the two money columns is non-zero is the
# transaction; tried first, or the line below would misread the deposit
# column as a running balance.
_PDF_TX_LINE_WITHDRAW_DEPOSIT_RE = re.compile(
    rf"{_PDF_LEAD}(.+?)\s+({_MONEY})\s+({_MONEY})\s+({_MONEY})$", re.IGNORECASE
)
# An amount and a running balance — "... -21.30 15,937.21" (Chase) — so
# the pair is matched together, or a naive "last number on the line" grabs
# the balance instead of the amount. When the amount has no sign (UK,
# Indian and most European statements, whose money-out and money-in
# columns collapse into one once the empty one vanishes from the text),
# the balance going up or down says which way it went.
_PDF_TX_LINE_WITH_BALANCE_RE = re.compile(
    rf"{_PDF_LEAD}(.+?)\s+({_MONEY})\s+({_MONEY})$", re.IGNORECASE
)
_PDF_TX_LINE_RE = re.compile(rf"{_PDF_LEAD}(.+?)\s+({_MONEY})$", re.IGNORECASE)
# A statement's own summary lines ("28 25/08/2026 Total Transaction Amount
# 50,118.30 48,304.29", "Beginning balance on September 5, 2026 $326.44")
# can be shaped like a real transaction line once a row number or a date
# is allowed in front — matching one would silently import a total as a
# huge expense. Specific on purpose: "Total Wine & More" is a real store.
_PDF_SUMMARY_ROW_RE = re.compile(
    r"total\s+(?:transaction|deposits|withdrawals|credits|debits|fees|purchases|payments|for\s+this)|"
    r"sub-?total|(?:beginning|ending|opening|closing|previous|starting)\s+balance|new\s+balance|"
    r"balance\s+(?:brought|carried)\s+forward|brought\s+forward|carried\s+forward|^balance$|"
    r"saldo\s+(?:anterior|inicial|final|iniziale|finale)|anfangssaldo|endsaldo|ancien\s+solde|nouveau\s+solde",
    re.IGNORECASE,
)


class PdfPasswordRequired(Exception):
    """Raised by _parse_statement_pdf when the PDF is encrypted and either
    no password or the wrong password was supplied — the route catches
    this specifically so the client can prompt for a password and retry,
    instead of surfacing it as a generic "corrupted file" error."""


# How long reading one statement may take in all, AI included. The web
# server stops a request at 30 seconds and runs one worker, so a statement
# that took longer used to fail outright (a real Bank of America statement
# did, 2026-10-23) and hold up every other request while it ran. Past this
# budget the AI steps are skipped and what the offline reader found is
# shown as it is.
_IMPORT_TIME_BUDGET_SECONDS = 22.0


def _parse_statement_pdf(
    file_stream, password: str | None = None, fallback_currency: str = "USD", deadline: float | None = None,
) -> tuple[list[dict], list[str], bool, str | None]:
    """
    Returns (rows, warnings, ai_assisted, totals). ai_assisted is True only
    when AI extraction added rows (the route then shows a "read with AI
    assistance, please double-check" notice); totals is
    statement_reader.check_totals' verdict: "match" when the rows add up to
    the statement's own opening and closing balance (or its money-in and
    money-out totals), "mismatch" when they don't, None when it prints
    neither.

    Reads each page's plain text and matches transaction lines with a
    regex, rather than pdfplumber's table-extraction — real bank
    statements almost never draw table gridlines pdfplumber can detect,
    and fragment badly under its "text" strategy (a header cell like
    "Description" splitting into "Descr"/"iption"). A statement line,
    across every real sample seen, starts with a date and ends with an
    amount (plus, often, a running balance), which is what this matches.
    Continuation lines (an "ID:...PPD" trace line, wrapped city/state
    text) don't fit that shape and are simply not matched.

    Which way the money went, in order of trust: two money columns (one
    of them zero); the amount's own sign ("-12.00", "(12.00)", "12.00-",
    "12.00 DR"/"CR"); the running balance moving up or down; and only
    then a default, income on a bank statement, a purchase on a credit
    card statement. A section header ("Withdrawals") is never used: a
    summary block near the top once set it for every line after it.

    AI is a fallback, not a step: it runs only when the offline pass found
    nothing, or left date-led lines with an amount unread, or its rows
    don't add up to the statement's totals; never when they do. A Bank of
    America statement the offline pass read perfectly used to go to AI
    anyway over its "Beginning balance $326.44" line and time out.

    Only works on text-based PDFs (the normal case for a downloaded or
    emailed e-statement); a scanned page has no text layer and is flagged
    clearly rather than silently returning nothing.
    """
    # Imported lazily: pdfplumber pulls in Pillow/pypdfium2, real import
    # weight that only this one path needs.
    import pdfplumber
    from pdfminer.pdfdocument import PDFPasswordIncorrect
    from pdfplumber.utils.exceptions import PdfminerException

    try:
        pdf = pdfplumber.open(file_stream, password=password or "")
    except PdfminerException as exc:
        if isinstance(exc.args[0] if exc.args else None, PDFPasswordIncorrect):
            raise PdfPasswordRequired() from None
        return [], ["Could not read that PDF. It may be encrypted or damaged."], False, None
    except Exception:
        return [], ["Could not read that PDF."], False, None

    with pdf:
        pages_text = [page.extract_text() or "" for page in pdf.pages]
        if not any(text.strip() for text in pages_text):
            # A scan: no text layer, so the pages themselves go to AI.
            try:
                images = [_image_for_ai(page.to_image(resolution=150).original)
                          for page in pdf.pages[:_SCAN_MAX_PAGES]]
            except Exception:
                log.warning("Couldn't render a scanned statement's pages", exc_info=True)
                images = []
    if not any(text.strip() for text in pages_text):
        if not images:
            return [], ["Couldn't read that PDF. Try the PDF or CSV your bank lets you download."], False, None
        rows, warnings, totals = _read_scanned_statement(images, fallback_currency, deadline)
        return rows, warnings, ("scan" if rows else False), totals

    full_text = "\n".join(pages_text)

    day_first = _detect_day_first(full_text)
    currency = _detect_import_currency(full_text, fallback_currency)
    card = statement_reader.is_credit_card_statement(full_text)
    anchor = statement_reader.statement_end_date(full_text, day_first)

    entries: list[dict] = []
    warnings: list[str] = []
    # Lines that start with a date and carry an amount but matched none of
    # the shapes above, most often a page break splitting one transaction
    # across two pages. Only date-led lines count: a phone number or a
    # "Beginning balance" line has an amount too, and used to send every
    # statement to AI.
    unmatched = 0

    for text in pages_text:
        for raw_line in text.split("\n"):
            line = raw_line.strip()
            if not line:
                continue
            entry = _read_statement_line(line, day_first, anchor)
            if entry == "skip":
                continue
            if entry is None:
                if (statement_reader.starts_with_date(line) and statement_reader.amounts_in(line)
                        and not _PDF_SUMMARY_ROW_RE.search(line)):
                    unmatched += 1
                continue
            if isinstance(entry, str):
                warnings.append(entry)
                continue
            entries.append(entry)

    _decide_directions(entries, card, statement_reader.opening_balance(full_text))
    rows = [{
        "date": e["date"].strftime("%Y-%m-%d"),
        "description": e["description"],
        "amount": float(abs(e["value"])),
        "type": e["type"],
        "category": _guess_category(e["description"], e["type"]),
        "currency": currency,
    } for e in entries]

    totals = statement_reader.check_totals(full_text, rows, card)
    if rows and (totals == "match" or (not unmatched and totals != "mismatch")):
        return rows, warnings, False, totals
    if deadline is not None and time.monotonic() > deadline - 4:
        return rows, warnings, False, totals

    ai_rows, ai_warnings = _parse_statement_pdf_with_ai(pages_text, anchor=anchor, deadline=deadline)
    for r in ai_rows:
        r["currency"] = currency

    if rows:
        # Only what the offline pass missed: same date and amount is the
        # same transaction, whatever each pass called it.
        seen = {(r["date"], f"{r['amount']:.2f}") for r in rows}
        new_ai_rows = [r for r in ai_rows if (r["date"], f"{r['amount']:.2f}") not in seen]
        merged = rows + new_ai_rows
        return merged, warnings, bool(new_ai_rows), statement_reader.check_totals(full_text, merged, card)

    if ai_rows:
        return ai_rows, ai_warnings, True, statement_reader.check_totals(full_text, ai_rows, card)

    return [], [
        "Found text in that PDF, but nothing that looked like a transaction line "
        "(a date, description, and amount together). A CSV, OFX or QIF export, if "
        "your bank offers one, will work more reliably than a PDF."
    ], False, None


def _read_statement_line(line: str, day_first: bool, anchor):
    """One PDF text line as an entry dict (date, description, value,
    explicit, card_credit, balance, type or None), None when it isn't a
    transaction line, "skip" for a summary or zero line, or a warning
    string for a transaction whose name wrapped away."""
    wd = _PDF_TX_LINE_WITHDRAW_DEPOSIT_RE.match(line)
    if wd:
        date_str, description, out_str, in_str, balance_str = wd.groups()
        when = statement_reader.parse_date(date_str, day_first, anchor)
        money_out, _ = statement_reader.parse_amount(out_str)
        money_in, _ = statement_reader.parse_amount(in_str)
        balance, _ = statement_reader.parse_amount(balance_str)
        if when is not None and money_out is not None and money_in is not None:
            if money_out != 0 and money_in == 0:
                value, t_type = -abs(money_out), "expense"
            elif money_in != 0 and money_out == 0:
                value, t_type = abs(money_in), "income"
            elif money_out != 0:
                value, t_type = -abs(money_out), "expense"
            else:
                return "skip"
            return _entry(when, description, value, True, False, balance, t_type)

    m = _PDF_TX_LINE_WITH_BALANCE_RE.match(line) or _PDF_TX_LINE_RE.match(line)
    if not m:
        return None
    groups = m.groups()
    date_str, description, amount_str = groups[0], groups[1], groups[2]
    balance = statement_reader.parse_amount(groups[3])[0] if len(groups) > 3 else None
    when = statement_reader.parse_date(date_str, day_first, anchor)
    if when is None:
        return None
    value, explicit = statement_reader.parse_amount(amount_str)
    if value is None:
        return None
    if value == 0:
        return "skip"
    credit_marker = bool(re.search(r"CR\.?$", amount_str.strip(), re.IGNORECASE))
    return _entry(when, description, value, explicit, credit_marker, balance, None)


def _entry(when, description, value, explicit, credit_marker, balance, t_type):
    description = description.strip()
    # A description that wrapped onto the line before or after leaves a
    # line that's nothing but a date and numbers; no real merchant name is
    # ever pure digits and punctuation.
    if not description or not any(ch.isalpha() for ch in description):
        return (f"Skipped a transaction on {when.strftime('%b %d, %Y')}: "
                "its description wrapped onto another line and couldn't be matched.")
    if _PDF_SUMMARY_ROW_RE.search(description):
        return "skip"
    return {
        "date": when, "description": description, "value": value, "explicit": explicit,
        "credit_marker": credit_marker, "balance": balance, "type": t_type,
    }


def _decide_directions(entries: list[dict], card: bool, opening) -> None:
    """Sets each entry's type (see _parse_statement_pdf's docstring for the
    order of trust). On a card statement an unsigned amount is a purchase,
    unless that card prints its purchases negative, which the majority of
    its own lines shows."""
    single = [e for e in entries if e["type"] is None]
    purchases_unsigned = card and (
        sum(e["value"] > 0 for e in single) >= sum(e["value"] < 0 for e in single)
    )
    for e in single:
        if purchases_unsigned:
            e["type"] = "income" if (e["value"] < 0 or e["credit_marker"]) else "expense"
        else:
            e["type"] = "expense" if e["value"] < 0 else "income"
    if not card:
        statement_reader.resolve_signs_from_balances(entries, opening)


# Statement text per AI call. A whole statement in one call meant one long
# reply (a 70-row statement took most of the server's 30 seconds); a few
# pages per call, read side by side, finish together in a fraction of it.
_AI_CHUNK_CHARS = 9000
_AI_PARALLEL_CALLS = 4


def _ai_chunks(pages_text: list[str]) -> list[list[str]]:
    chunks: list[list[str]] = []
    current: list[str] = []
    size = 0
    for page in pages_text:
        pieces = [page]
        if len(page) > _AI_CHUNK_CHARS:
            lines = page.split("\n")
            pieces, buf = [], []
            for line in lines:
                if buf and sum(len(x) + 1 for x in buf) + len(line) > _AI_CHUNK_CHARS:
                    pieces.append("\n".join(buf))
                    buf = []
                buf.append(line)
            if buf:
                pieces.append("\n".join(buf))
        for piece in pieces:
            if current and size + len(piece) > _AI_CHUNK_CHARS:
                chunks.append(current)
                current, size = [], 0
            current.append(piece)
            size += len(piece)
    if current:
        chunks.append(current)
    return chunks


def _parse_statement_pdf_with_ai(
    pages_text: list[str], anchor: date | None = None, deadline: float | None = None,
) -> tuple[list[dict], list[str]]:
    """
    AI-assisted fallback for _parse_statement_pdf, for a statement whose
    lines the offline reader can't make out (a row's date and its amounts
    on separate lines, a layout no pattern fits). Reads the same per-page
    text a human would, a few pages per call, the calls side by side, each
    allowed only the time left in the import's budget.

    Degrades silently on any failure (missing API key, network error,
    timeout, malformed model output) rather than surfacing an AI-specific
    error: from the person's side it's just a layout Artha couldn't read,
    and the caller's message already says so.
    """
    from concurrent.futures import ThreadPoolExecutor

    from ...services.ai_service import AIService  # local: ai_service imports
    # TRANSACTION_CATEGORIES from this module, so a top-level import here
    # would be circular — safe once both modules have finished loading.

    chunks = [c for c in _ai_chunks(pages_text) if "".join(c).strip()]
    if not chunks:
        return [], []
    timeout = None
    if deadline is not None:
        timeout = deadline - time.monotonic() - 1
        if timeout < 4:
            return [], []
    # Later pages don't repeat the statement's dates; this keeps a bare
    # "09/08" on page 5 in the right year.
    if anchor is not None:
        context = f"(This statement's period ends {anchor.strftime('%B %d, %Y')}.)\n"
        chunks = [chunks[0]] + [[context + c[0]] + c[1:] for c in chunks[1:]]

    with ThreadPoolExecutor(max_workers=min(_AI_PARALLEL_CALLS, len(chunks))) as pool:
        results = list(pool.map(lambda c: AIService.extract_pdf_transactions(c, timeout=timeout), chunks))

    rows: list[dict] = []
    warnings: list[str] = []
    for result in results:
        if "error" in result:
            log.warning("AI statement-extraction fallback failed: %s", result["error"])
            continue
        _rows_from_ai(result, rows, warnings)
        if result.get("truncated"):
            warnings.append(
                "This statement was long enough that only the first part of it was read. "
                "Later transactions may be missing."
            )
    return rows, warnings


def _rows_from_ai(result: dict, rows: list[dict], warnings: list[str]) -> None:
    """Appends the usable transactions in one AI extraction result to
    `rows`, and a warning for each one it had to leave out."""
    for raw in result.get("transactions") or []:
        if not isinstance(raw, dict):
            continue
        try:
            parsed_date = datetime.strptime(str(raw.get("date", "")), "%Y-%m-%d").date()
        except ValueError:
            warnings.append("Skipped a transaction: couldn't make sense of its date.")
            continue
        description = str(raw.get("description") or "").strip()
        if not description or not any(ch.isalpha() for ch in description):
            warnings.append(
                f"Skipped a transaction on {parsed_date.strftime('%b %d, %Y')}: no usable description."
            )
            continue
        t_type = raw.get("type")
        if t_type not in ("income", "expense"):
            continue
        try:
            amount = abs(float(raw.get("amount")))
        except (TypeError, ValueError):
            continue
        if amount == 0:
            continue
        rows.append({
            "date": parsed_date.strftime("%Y-%m-%d"),
            "description": description,
            "amount": amount,
            "type": t_type,
            "category": _guess_category(description, t_type),
        })


# ---------------------------------------------------------------------------
# Scanned statements and photos: no text to read, so AI reads the pages
# themselves, a couple per call, the calls side by side, inside the same
# time budget, with the same totals check (it reads the opening and closing
# balance off the page too).
# ---------------------------------------------------------------------------

_SCAN_MAX_PAGES = 12
_SCAN_PAGES_PER_CALL = 2
_SCAN_PARALLEL_CALLS = 6
# Long enough edge for small print to stay readable, small enough to keep
# each request quick.
_SCAN_LONG_EDGE = 1600
_IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".gif")


class UnreadablePhoto(ValueError):
    """A photo in a format Artha can't open (HEIC without a converter)."""


def _looks_like_image(data: bytes, name: str) -> bool:
    if name.endswith(_IMAGE_EXTENSIONS):
        return True
    head = data[:16]
    return (head.startswith(b"\xff\xd8\xff") or head.startswith(b"\x89PNG")
            or (head[:4] == b"RIFF" and head[8:12] == b"WEBP") or head[4:12] in (b"ftypheic", b"ftypheix", b"ftypmif1"))


def _image_for_ai(image) -> tuple[str, str]:
    """A PIL image (or raw bytes of one) as a JPEG for the AI: upright,
    RGB, its long edge at most _SCAN_LONG_EDGE."""
    import base64
    from PIL import Image, ImageOps, UnidentifiedImageError

    if isinstance(image, (bytes, bytearray)):
        try:
            image = Image.open(io.BytesIO(image))
            image.load()
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
            raise UnreadablePhoto() from None
    image = ImageOps.exif_transpose(image).convert("RGB")
    image.thumbnail((_SCAN_LONG_EDGE, _SCAN_LONG_EDGE))
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=82)
    return "image/jpeg", base64.standard_b64encode(buffer.getvalue()).decode("ascii")


def _read_scanned_statement(images: list[tuple[str, str]], fallback_currency: str,
                            deadline: float | None) -> tuple[list[dict], list[str], str | None]:
    """Transactions from images of statement pages. Returns (rows,
    warnings, totals) like the PDF reader."""
    from concurrent.futures import ThreadPoolExecutor

    from ...services.ai_service import AIService  # local: circular import, see above

    if not images:
        return [], [], None
    timeout = None
    if deadline is not None:
        timeout = deadline - time.monotonic() - 1
        if timeout < 6:
            return [], ["Reading that scan took too long. Try again in a moment."], None
    chunks = [images[i:i + _SCAN_PAGES_PER_CALL] for i in range(0, len(images), _SCAN_PAGES_PER_CALL)]
    with ThreadPoolExecutor(max_workers=min(_SCAN_PARALLEL_CALLS, len(chunks))) as pool:
        results = list(pool.map(lambda c: AIService.extract_scanned_statement(c, timeout=timeout), chunks))

    rows: list[dict] = []
    warnings: list[str] = []
    readable = [r for r in results if "error" not in r]
    for result in results:
        if "error" in result:
            log.warning("Reading a scanned statement failed: %s", result["error"])
            continue
        _rows_from_ai(result, rows, warnings)
    if not readable:
        return [], ["Artha couldn't read that scan. Try again in a moment, or upload the PDF your bank emails."], None
    if len(readable) < len(results):
        warnings.append("Some pages couldn't be read, so transactions may be missing.")

    currency = next((str(r["currency"]).upper() for r in readable
                     if str(r.get("currency") or "").upper() in CURRENCY_CODES), fallback_currency)
    for row in rows:
        row["currency"] = currency
    opening = next((r["opening_balance"] for r in readable if isinstance(r.get("opening_balance"), (int, float))), None)
    closing = next((r["closing_balance"] for r in reversed(readable) if isinstance(r.get("closing_balance"), (int, float))), None)
    totals = None
    if opening is not None and closing is not None:
        card = any(r.get("is_credit_card") is True for r in readable)
        totals = statement_reader.check_totals(
            f"Opening balance {opening:.2f}\nClosing balance {closing:.2f}", rows, card)
    if len(images) >= _SCAN_MAX_PAGES:
        warnings.append(f"Only the first {_SCAN_MAX_PAGES} pages were read.")
    return rows, warnings, totals


# Bounds one categorize_transactions call — a very large statement's tail
# just keeps whatever _guess_category already gave it (None, most likely)
# rather than this growing into an unbounded-cost call. Comfortably above
# what any normal single statement's uncategorized count looks like in
# practice; there mainly for the pathological case.
_CATEGORIZE_MAX_ITEMS = 200


def _fill_uncategorized_via_ai(rows: list[dict], timeout: float | None = None) -> None:
    """
    Mutates `rows` in place, filling in `category` for expense rows
    _guess_category's keyword match left as None — called from
    import_preview() for both CSV and PDF imports alike (the same gap
    hits either source: a keyword list is necessarily finite, and some
    statements never print a merchant name at all). Degrades silently on
    any failure, same as _parse_statement_pdf_with_ai — an import that
    can't reach the AI service still works, just with whatever
    _guess_category already found.
    """
    uncategorized_idx = [
        i for i, r in enumerate(rows)
        if r.get("type") == "expense" and r.get("category") is None
    ][:_CATEGORIZE_MAX_ITEMS]
    if not uncategorized_idx:
        return

    from ...services.ai_service import AIService  # local: see the matching
    # comment in _parse_statement_pdf_with_ai — same circular-import reason.

    result = AIService.categorize_transactions(
        [rows[i]["description"] for i in uncategorized_idx], timeout=timeout
    )
    if "error" in result:
        log.warning("AI import categorization failed: %s", result["error"])
        return

    for i, category in zip(uncategorized_idx, result.get("categories", [])):
        if category is not None:
            rows[i]["category"] = category


# Deliberately much smaller than import's own _CATEGORIZE_MAX_ITEMS
# (200): the model is asked to return one category per description, in
# the same order, and categorize_transactions() throws the *entire*
# response away the moment that count doesn't line up exactly (safer
# than risking a silently-misaligned answer) -- import rarely has
# enough uncategorized rows in one statement to ever hit that ceiling,
# but a real backlog easily does, and at 200 items a single dropped or
# duplicated entry in the model's own output loses the whole batch, not
# just one row. A smaller batch is both less likely to misalign at all
# and caps the damage to a few rows when it does.
_BULK_CATEGORIZE_BATCH_SIZE = 40
# 10 * 40 = 400 per click -- smaller than the old 3 * 200 = 600 ceiling,
# trading a bit of per-click coverage for keeping worst-case request
# time (up to 10 sequential AI calls) further from whatever timeout the
# hosting platform's own reverse proxy enforces. A backlog bigger than
# 400 just takes one more click, same as any other remainder already
# does.
_BULK_CATEGORIZE_MAX_AI_BATCHES = 10


@finance_bp.route("/finance/categorize_uncategorized", methods=["POST"])
@login_required
@ai_limit(AI_BULK_CATEGORIZE_LIMIT)
def categorize_uncategorized():
    """
    Retroactive version of the same auto-categorization add_transaction()
    now applies to every new row: every transaction this user already has
    sitting at category=None gets one more pass through the identical
    two-tier keyword-then-AI logic, so catching up a backlog of history
    never means hand-editing rows one at a time. Never touches a
    transaction that already has a category, however that category got
    there.
    """
    uid = current_user.id
    uncategorized = Transaction.query.filter_by(user_id=uid, category=None).all()
    if not uncategorized:
        return jsonify({"categorized": 0, "remaining": 0, "message": "Nothing to categorize."})

    merchant_rules = _load_merchant_rules(uid)
    keyword_hits = 0
    ai_candidates: list[Transaction] = []
    for tx in uncategorized:
        guess = _guess_category(tx.description, tx.type, rules=merchant_rules)
        if guess is not None:
            tx.category = guess
            keyword_hits += 1
        elif tx.type == "expense":
            ai_candidates.append(tx)

    # Committed here, before a single AI call -- the free keyword pass
    # must never be lost to a later AI batch failing, which is exactly
    # what a single commit at the very end used to risk.
    db.session.commit()

    ai_hits = 0
    ai_failures = 0
    batches_run = 0
    for i in range(0, len(ai_candidates), _BULK_CATEGORIZE_BATCH_SIZE):
        if batches_run >= _BULK_CATEGORIZE_MAX_AI_BATCHES:
            break
        chunk = ai_candidates[i:i + _BULK_CATEGORIZE_BATCH_SIZE]
        rows = [{"type": tx.type, "description": tx.description, "category": None} for tx in chunk]
        try:
            _fill_uncategorized_via_ai(rows)
        except Exception as exc:  # noqa: BLE001 - one bad batch must not sink the rest
            log.error("Bulk AI categorization batch failed: %s", exc, exc_info=True)
            ai_failures += 1
            batches_run += 1
            continue

        batch_hits = 0
        for tx, row in zip(chunk, rows):
            if row["category"] is not None:
                tx.category = row["category"]
                batch_hits += 1
        if batch_hits:
            # Committed per batch, not once at the end -- a later batch
            # throwing (network blip, a malformed response) must never
            # roll back categories an earlier batch already found.
            db.session.commit()
        ai_hits += batch_hits
        batches_run += 1

    total = keyword_hits + ai_hits
    remaining = len(uncategorized) - total
    if total == 0 and ai_failures > 0:
        message = "AI categorization is having trouble right now. Try again in a moment."
    elif total == 0:
        message = "Couldn't confidently categorize any of them."
    elif remaining > 0:
        message = f"Categorized {total}, {remaining} left. Click again to continue."
    else:
        message = f"Categorized {total} transaction{'s' if total != 1 else ''}."
    return jsonify({"categorized": total, "remaining": remaining, "message": message})


@finance_bp.route("/finance/export")
@login_required
def export_csv():
    """
    Downloads the signed-in user's transactions as CSV.

    Honors the same ?month=YYYY-MM / ?month=all convention as finance_page()
    so an "Export" link on a filtered view exports exactly what's on
    screen, not a surprise full-history dump.
    """
    uid = current_user.id
    month_param = (request.args.get("month") or "").strip()
    all_time = month_param == "all"

    query = Transaction.query.filter_by(user_id=uid)
    if all_time:
        filename_part = "all-time"
    elif month_param:
        try:
            sel_year, sel_month = (int(part) for part in month_param.split("-", 1))
            selected_date = _month_start(sel_year, sel_month)
        except (ValueError, TypeError):
            today = user_today(current_user)
            selected_date = _month_start(today.year, today.month)
        next_month = (
            _month_start(selected_date.year + 1, 1)
            if selected_date.month == 12
            else _month_start(selected_date.year, selected_date.month + 1)
        )
        query = query.filter(
            Transaction.timestamp >= selected_date,
            Transaction.timestamp < next_month,
        )
        filename_part = selected_date.strftime("%Y-%m")
    else:
        today = user_today(current_user)
        selected_date = _month_start(today.year, today.month)
        next_month = (
            _month_start(today.year + 1, 1) if today.month == 12 else _month_start(today.year, today.month + 1)
        )
        query = query.filter(
            Transaction.timestamp >= selected_date,
            Transaction.timestamp < next_month,
        )
        filename_part = selected_date.strftime("%Y-%m")

    rows = query.order_by(Transaction.timestamp.asc(), Transaction.id.asc()).all()

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["Date", "Description", "Type", "Amount", "Currency", "Category", "Recurring"])
    for t in rows:
        writer.writerow([
            t.timestamp.strftime("%Y-%m-%d") if t.timestamp else "",
            csv_formula_safe(t.description),
            t.type,
            f"{t.amount:.2f}",
            t.native_currency,
            TRANSACTION_CATEGORIES.get(t.category, {}).get("label", "") if t.category else "",
            "yes" if t.is_recurring else "no",
        ])

    response = Response(buffer.getvalue(), mimetype="text/csv")
    response.headers["Content-Disposition"] = f"attachment; filename=artha-transactions-{filename_part}.csv"
    return response


def preview_statement(user, file, password: str | None = None) -> dict:
    """
    Parses an uploaded statement (a werkzeug FileStorage) and returns what
    WOULD be imported; nothing is saved here. Shared by the website's
    import sheet and the app's POST /api/v1/imports/preview, so both read
    statements the same way: the free offline reader first, AI only for
    what it misses, the file read from the upload in memory and never
    written anywhere. Raises PdfPasswordRequired for a locked PDF and
    ValidationError when nothing could be read.

    Reads PDF, CSV, OFX/QFX and QIF, recognized by their contents as well
    as their names. Finishes inside _IMPORT_TIME_BUDGET_SECONDS: past it,
    the AI steps are skipped rather than letting the request run into the
    server's own limit.

    Returns {"rows", "warnings", "notice"?, "totals"}: each row has date,
    description (the bank's line as a person would name it, see
    statement_reader.clean_description), amount (a float, as the parsers
    produce it), type, category, currency, duplicate (already in Artha)
    and transfer (between the person's own accounts); the review starts
    both of those unticked. totals is "match" when the
    rows add up to the statement's own totals, "mismatch" when they
    don't, None when the file doesn't say.
    """
    if file is None or not file.filename:
        raise ValidationError("No file uploaded")

    deadline = time.monotonic() + _IMPORT_TIME_BUDGET_SECONDS
    data = file.stream.read()
    name = file.filename.lower()
    # A statement that doesn't say its currency is read in the person's own.
    fallback = user.preferred_currency or "USD"
    ai_assisted = False
    totals = None

    if _looks_like_image(data, name):
        # A photo of a paper statement: read like a scanned PDF.
        try:
            images = [_image_for_ai(data)]
        except UnreadablePhoto:
            raise ValidationError(
                "Artha can't open that kind of photo. Save it as a JPEG or PNG, or take a screenshot of it."
            ) from None
        rows, warnings, totals = _read_scanned_statement(images, fallback, deadline)
        ai_assisted = "scan" if rows else False
    elif name.endswith((".ofx", ".qfx")) or statement_reader.looks_like_ofx(data):
        rows, warnings, _ = statement_reader.parse_ofx(data, fallback)
    elif name.endswith(".qif") or statement_reader.looks_like_qif(data):
        rows, warnings = statement_reader.parse_qif(data, fallback)
    elif name.endswith(".pdf") or file.mimetype == "application/pdf" or data[:5] == b"%PDF-":
        rows, warnings, ai_assisted, totals = _parse_statement_pdf(
            io.BytesIO(data), password=password, fallback_currency=fallback, deadline=deadline)
    else:
        rows, warnings = _parse_statement_csv(io.BytesIO(data), fallback_currency=fallback)
    if not rows:
        raise ValidationError(warnings[0] if warnings else "No transactions found in file.")

    for row in rows:
        row["bank_text"] = row["description"]
        row["description"] = statement_reader.clean_description(row["description"])
        if row.get("category") is None:
            row["category"] = _guess_category(row["description"], row["type"])

    remaining = deadline - time.monotonic()
    if remaining > 4:
        _fill_uncategorized_via_ai(rows, timeout=remaining - 1)

    _flag_already_in_artha(user, rows)
    # Accounts the person has told Artha are theirs, by leaving lines to or
    # from them out of an earlier import (remember_transfer_accounts).
    own_accounts = {a.key for a in TransferAccount.query.filter_by(user_id=user.id)}
    for row in rows:
        ref = statement_reader.account_reference(row["bank_text"])
        row["account_ref"] = ref[1] if ref else None
        row["transfer"] = bool(ref and ref[0] in own_accounts) or statement_reader.is_own_transfer(
            row["bank_text"], row["description"], user.first_name, user.last_name)
        row.pop("bank_text", None)

    result = {"rows": rows, "warnings": warnings, "totals": totals}
    if ai_assisted == "scan":
        result["notice"] = (
            "This was a scan or a photo, so Artha read it with AI. Double-check "
            "the dates and amounts below before importing."
        )
    elif ai_assisted:
        # Distinct from "warnings" (which the client renders as "N rows
        # skipped") — this isn't a skip, it's a heads-up that these rows
        # came from AI-assisted reading rather than direct parsing, worth
        # a closer look before importing but not a sign anything is wrong.
        result["notice"] = (
            "This PDF's layout didn't match the usual pattern, so these were read "
            "with AI assistance instead of parsed directly. Double-check the "
            "dates and amounts below before importing."
        )
    return result


def _flag_already_in_artha(user, rows: list[dict]) -> None:
    """Marks rows Artha already has, so the review starts them unticked
    and re-uploading an overlapping statement doesn't double anything.
    Exact (same date, name, amount and type, the bank's own text or the
    cleaned name), or close: the same amount and type within three days
    under a name sharing a real word ("Starbucks" logged with Apple Pay,
    "MOBILE PURCHASE 0909 STARBUCKS STORE 51971" on the statement). A bill
    Artha generated that no charge has landed on yet never counts as
    close: the import should pay it (bill_matching), not skip it."""
    if not rows:
        return
    dates = [datetime.strptime(r["date"], "%Y-%m-%d").date() for r in rows]
    start = datetime.combine(min(dates) - timedelta(days=4), datetime.min.time())
    end = datetime.combine(max(dates) + timedelta(days=4), datetime.min.time())
    rows_in_window = Transaction.query.filter(
        Transaction.user_id == user.id, Transaction.timestamp >= start, Transaction.timestamp < end,
    ).all()
    # A split purchase is compared as the purchase it was, at its total.
    split_parts = defaultdict(list)
    existing = []
    for t in rows_in_window:
        (split_parts[t.split_group] if t.split_group else existing).append(t)
    for parts in split_parts.values():
        first = min(parts, key=lambda p: p.id)
        existing.append(SimpleNamespace(
            type=first.type, description=first.description, timestamp=first.timestamp,
            amount=sum((Decimal(p.amount) for p in parts), Decimal("0")),
            is_recurring=False, bill_matched=False,
        ))
    exact = {
        (t.timestamp.strftime("%Y-%m-%d"), t.description, f"{t.amount:.2f}", t.type) for t in existing
    }
    for row, when in zip(rows, dates):
        amount = f"{row['amount']:.2f}"
        if (row["date"], row["description"], amount, row["type"]) in exact or \
                (row["date"], row.get("bank_text"), amount, row["type"]) in exact:
            row["duplicate"] = True
            continue
        row["duplicate"] = any(
            t.type == row["type"] and f"{t.amount:.2f}" == amount
            and abs((t.timestamp.date() - when).days) <= 3
            and not (t.is_recurring and not t.bill_matched)
            and (statement_reader.names_match(t.description, row["description"])
                 or statement_reader.names_match(t.description, row.get("bank_text") or ""))
            for t in existing
        )


def remember_transfer_accounts(user, left_out) -> list[str]:
    """Remembers the accounts named on lines the person left out of an
    import ("SAV 7837"), so lines to or from them start unticked next time,
    the way Copilot learns from what you do instead of asking you to set
    rules up. Only a line that names an account (statement_reader.
    account_reference) teaches anything. Returns the labels newly learned.
    The caller commits."""
    if not isinstance(left_out, list):
        return []
    known = {a.key for a in TransferAccount.query.filter_by(user_id=user.id)}
    learned = []
    for row in left_out[:200]:
        if not isinstance(row, dict) or row.get("duplicate"):
            continue
        ref = statement_reader.account_reference(str(row.get("account_ref") or ""))
        if ref is None or ref[0] in known:
            continue
        known.add(ref[0])
        db.session.add(TransferAccount(user_id=user.id, key=ref[0], label=ref[1]))
        learned.append(ref[1])
    return learned


def forget_transfer_account(user, account_id) -> bool:
    """Stops treating one remembered account as the person's own (Settings'
    Forget). False when there's no such account of theirs."""
    account = db.session.get(TransferAccount, account_id) if isinstance(account_id, int) else None
    if account is None or account.user_id != user.id:
        return False
    db.session.delete(account)
    db.session.commit()
    return True


@finance_bp.route("/settings/transfer-accounts/<int:account_id>/forget", methods=["POST"])
@login_required
def settings_forget_transfer_account(account_id):
    if forget_transfer_account(current_user, account_id):
        flash("Artha will count that account's transfers again.", "success")
    return redirect(url_for("auth.settings_page") + "#s-transfers")


def import_statement_rows(user, rows, left_out=None) -> tuple[int, int]:
    """
    Saves the (possibly edited) rows a preview produced, for the website
    and the app alike, and returns (imported, skipped). Each row keeps its
    own statement date exactly as parsed — a March statement imported in
    July is filed under March, never today. A row that pays a tracked bill
    fills in that bill instead of counting twice (bill_matching). Raises
    ValidationError for an empty list; any other exception is a real
    failure (already rolled back).
    """
    if not isinstance(rows, list) or not rows:
        raise ValidationError("No rows to import")

    uid = user.id
    max_pos = db.session.query(func.max(Transaction.position)).filter_by(user_id=uid).scalar() or 0

    imported = 0
    skipped = 0
    for row in rows:
        if not isinstance(row, dict):
            skipped += 1
            continue

        description = (row.get("description") or "").strip()
        t_type = row.get("type")
        date_str = (row.get("date") or "").strip()

        if not description or t_type not in ("income", "expense"):
            skipped += 1
            continue

        try:
            amount = Decimal(str(row.get("amount", "")))
            if amount < 0:
                raise InvalidOperation
        except InvalidOperation:
            skipped += 1
            continue

        try:
            datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            skipped += 1
            continue

        category = row.get("category") or None
        if category not in TRANSACTION_CATEGORIES:
            category = None

        # Each row already carries the whole document's detected currency
        # (see _detect_import_currency, called once per statement in
        # _parse_statement_csv/_parse_statement_pdf and visible in the
        # preview step before this commit ever runs) — an unrecognized
        # value falls back the same way manual entry does, rather than
        # skipping an otherwise-valid row over it.
        currency = (row.get("currency") or "").strip().upper()
        if currency not in CURRENCY_CODES:
            currency = user.preferred_currency or "USD"
        usd_value, usd_rate_locked = lock_usd_value(amount, currency)

        # A statement line that pays a tracked bill fills in that bill's
        # occurrence rather than counting a second time (bill_matching).
        timestamp = _resolve_transaction_timestamp(date_str)
        occurrence = bill_matching.find_open_occurrence(uid, t_type, currency, amount, description, timestamp.date())
        if occurrence is not None:
            bill_matching.absorb_charge(
                occurrence, description=description, category=category,
                amount=amount, currency=currency, usd_value=usd_value,
                usd_rate_locked=usd_rate_locked, timestamp=timestamp,
                import_source="csv", needs_review=False)
            imported += 1
            continue

        max_pos += 1
        db.session.add(Transaction(
            description=description,
            amount=amount,
            type=t_type,
            user_id=uid,
            position=int(max_pos),
            # Reuses the same date_str -> noon-UTC construction every other
            # transaction date in the app goes through — date_str is
            # already confirmed valid above, so this never hits that
            # helper's own "fall back to today" branch.
            timestamp=timestamp,
            category=category,
            import_source="csv",
            currency=currency,
            usd_value=usd_value,
            usd_rate_locked=usd_rate_locked,
        ))
        imported += 1

    # Lines the person left out that name one of their accounts teach
    # Artha that account (remember_transfer_accounts).
    remember_transfer_accounts(user, left_out)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise

    if imported:
        _check_budget_overage_alerts(uid)
    return imported, skipped


@finance_bp.route("/finance/import/preview", methods=["POST"])
@login_required
@ai_limit(AI_IMPORT_LIMIT)
def import_preview():
    """The website's import sheet: preview_statement() as JSON, plus the
    category labels its editable table offers. Nothing is saved here."""
    password = request.form.get("pdf_password") or None
    try:
        result = preview_statement(current_user, request.files.get("statement"), password)
    except PdfPasswordRequired:
        message = "That PDF needs a password to open." if password is None else "That password didn't work."
        return jsonify({"message": message, "needs_password": True}), 400
    except ValidationError as exc:
        return jsonify({"message": str(exc)}), 400
    result["categories"] = {key: val["label"] for key, val in TRANSACTION_CATEGORIES.items()}
    return jsonify(result)


@finance_bp.route("/finance/import/commit", methods=["POST"])
@login_required
def import_commit():
    """Saves the rows the website's import sheet reviewed
    (import_statement_rows)."""
    data = request.get_json(silent=True) or {}
    try:
        imported, skipped = import_statement_rows(current_user, data.get("rows"), data.get("left_out"))
    except ValidationError as exc:
        return jsonify({"message": str(exc)}), 400
    except Exception as e:
        log.error("Error committing CSV import: %s", e, exc_info=True)
        return jsonify({"message": "Database error"}), 500
    return jsonify({
        "message": f"Imported {imported} transaction{'s' if imported != 1 else ''}.",
        "imported": imported,
        "skipped": skipped,
    })


def _category_spending_usd(user_id: int, start, end) -> dict[str, Decimal]:
    usd_amount = func.coalesce(Transaction.usd_value, Transaction.amount)
    rows = (
        db.session.query(Transaction.category, func.sum(usd_amount))
        .filter(
            Transaction.user_id == user_id,
            Transaction.type == "expense",
            Transaction.category.isnot(None),
            Transaction.timestamp >= start,
            Transaction.timestamp < end,
        )
        .group_by(Transaction.category)
        .all()
    )
    return {category: Decimal(str(total or 0)) for category, total in rows}


def category_rollover_carry(user_id: int, rows, today: date, currency: str, rates) -> dict[str, Decimal]:
    """For each limit with rollover on: last month's cap minus last
    month's spending in that category, in `currency` (positive when money
    was left over, negative when it went over). Carried one month only,
    never a running balance, so one unusual month can't snowball. Last
    month's cap is the limit as it is now (caps keep no history)."""
    rolling = [row for row in rows if getattr(row, "rollover", False)]
    if not rolling:
        return {}
    this_start = date(today.year, today.month, 1)
    last_start, last_end = current_month_bounds(this_start - timedelta(days=1))
    spent_usd = _category_spending_usd(user_id, last_start, last_end)
    carry = {}
    for row in rolling:
        cap = convert_amount(row.monthly_cap, row.currency or "USD", currency, rates)
        spent = convert_usd_to(spent_usd.get(row.category, Decimal("0")), currency, rates)
        carry[row.category] = (Decimal(cap) - Decimal(spent)).quantize(Decimal("0.01"))
    return carry


def category_budget_statuses(rows, month_transactions, display_currency: str, rates, carry=None) -> list[dict]:
    """budget_status() for each CategoryBudget row against this month's
    spending in that category, in display_currency. Shared by Plan on the
    web and the native app's Plan tab (services/screens.py) so both agree.
    `carry` (category_rollover_carry) adjusts each rolling limit's cap for
    this month; the cap itself never goes below zero."""
    carry = carry or {}
    # Same current-month scope as the overall budget, just grouped by the
    # raw category slug instead of summed.
    category_expense_totals: dict[str, Decimal] = defaultdict(Decimal)
    for t in month_transactions:
        if t.type == "expense" and t.category:
            category_expense_totals[t.category] += t.value_in_usd
    # Converted once here (not per-transaction above) -- compared against
    # each CategoryBudget.monthly_cap converted into display_currency too.
    for cat in category_expense_totals:
        category_expense_totals[cat] = convert_usd_to(category_expense_totals[cat], display_currency, rates)

    return [
        {
            "category": row.category,
            "label": TRANSACTION_CATEGORIES.get(row.category, {}).get("label", row.category),
            "icon": TRANSACTION_CATEGORIES.get(row.category, {}).get("icon"),
            "rollover": bool(getattr(row, "rollover", False)),
            "base_cap": convert_amount(row.monthly_cap, row.currency or "USD", display_currency, rates),
            "carry": carry.get(row.category),
            "status": _limit_status(
                Decimal(convert_amount(row.monthly_cap, row.currency or "USD", display_currency, rates))
                + carry.get(row.category, Decimal("0")),
                category_expense_totals.get(row.category, Decimal("0")),
            ),
        }
        for row in rows
    ]


def _limit_status(cap: Decimal, spent: Decimal) -> dict:
    """budget_status() for a category limit, except that a limit whose
    rollover wiped it out this month (cap at or below zero) still shows,
    as already over, instead of vanishing like an unset budget."""
    if cap > 0:
        return budget_status(cap, spent)
    return {"has_budget": True, "cap": 0.0, "spent": float(spent), "pct": 100.0, "tier": "over"}


def save_budget(user, raw_cap) -> Decimal:
    """Upserts `user`'s single monthly spending cap from what they typed
    (blank or 0 clears it) and commits. Shared by the web form below and
    the native app's API. Raises ValidationError for a bad amount; any
    other exception is a real failure (already rolled back)."""
    raw = str(raw_cap if raw_cap is not None else "").strip()
    cap = _validate_amount(raw) if raw else Decimal("0")

    # Whatever currency was active right now, when this number was typed
    # in -- same signal a manual transaction's own currency comes from.
    # Re-captured on every save (not just creation), since editing the
    # cap number means re-typing it while looking at whatever's
    # currently selected, same as a fresh entry.
    currency = user.preferred_currency or "USD"

    row = Budget.query.filter_by(user_id=user.id).first()
    if row is None:
        row = Budget(user_id=user.id, monthly_cap=cap, currency=currency)
        db.session.add(row)
    else:
        row.monthly_cap = cap
        row.currency = currency

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    return cap


def save_category_budget(user, category, raw_cap, rollover=None) -> Decimal:
    """Upserts one category's monthly cap for `user` and commits. Shared
    by the web form below and the native app's API. Raises ValidationError
    for an unknown category or an amount that isn't above zero."""
    category = str(category or "").strip()
    if category not in TRANSACTION_CATEGORIES or category == "income":
        raise ValidationError("Choose a valid category to budget.")

    raw = str(raw_cap if raw_cap is not None else "").strip()
    cap = _validate_amount(raw) if raw else Decimal("0")
    if cap <= 0:
        raise ValidationError("Enter an amount greater than zero.")

    # Same currency-capture as save_budget() above.
    currency = user.preferred_currency or "USD"

    row = CategoryBudget.query.filter_by(user_id=user.id, category=category).first()
    if row is None:
        row = CategoryBudget(user_id=user.id, category=category, monthly_cap=cap, currency=currency)
        db.session.add(row)
    else:
        row.monthly_cap = cap
        row.currency = currency
    # Only changed when sent, so saving a new amount from an older client
    # (or the web form without the switch) never turns rollover off.
    if rollover is not None:
        row.rollover = _truthy(rollover)

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    return cap


def remove_category_budget(user, category) -> bool:
    """Removes one category's cap for `user`. False when there wasn't one."""
    row = CategoryBudget.query.filter_by(user_id=user.id, category=str(category or "")).first()
    if row is None:
        return False
    try:
        db.session.delete(row)
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    return True


@finance_bp.route("/finance/budget", methods=["POST"])
@login_required
def set_budget():
    """Upsert the signed-in user's single monthly spending cap."""
    try:
        cap = save_budget(current_user, request.form.get("monthly_cap"))
        msg = "Budget updated." if cap > 0 else "Budget cleared."
        if is_ajax_request():
            return jsonify({"message": msg}), 200
        flash(msg, "success")
    except ValidationError as exc:
        msg = str(exc)
        if is_ajax_request():
            return jsonify({"message": msg}), 400
        flash(msg, "error")
    except Exception as e:
        log.error("Error saving budget: %s", e, exc_info=True)
        msg = "Error saving budget."
        if is_ajax_request():
            return jsonify({"message": msg}), 500
        flash(msg, "error")

    return redirect(url_for("finance.finance_page"))


@finance_bp.route("/finance/category-budget", methods=["POST"])
@login_required
def set_category_budget():
    """Upsert one category's monthly spending cap. Separate rows per
    category (CategoryBudget), alongside the single overall Budget above,
    not replacing it."""
    category = (request.form.get("category") or "").strip()
    try:
        save_category_budget(current_user, category, request.form.get("monthly_cap"), request.form.get("rollover"))
        msg = f"{TRANSACTION_CATEGORIES[category]['label']} budget saved."
        if is_ajax_request():
            return jsonify({"message": msg}), 200
        flash(msg, "success")
    except ValidationError as exc:
        msg = str(exc)
        if is_ajax_request():
            return jsonify({"message": msg}), 400
        flash(msg, "error")
    except Exception as e:
        log.error("Error saving category budget: %s", e, exc_info=True)
        msg = "Error saving budget."
        if is_ajax_request():
            return jsonify({"message": msg}), 500
        flash(msg, "error")

    return redirect(url_for("finance.finance_page"))


@finance_bp.route("/finance/category-budget/<category>/delete", methods=["POST"])
@login_required
def delete_category_budget(category):
    try:
        removed = remove_category_budget(current_user, category)
    except Exception as e:
        log.error("Error deleting category budget: %s", e, exc_info=True)
        if is_ajax_request():
            return jsonify({"message": "Error removing budget."}), 500
        flash("Error removing budget.", "error")
        return redirect(url_for("finance.finance_page"))

    if is_ajax_request():
        if not removed:
            return jsonify({"message": "There's no limit on that category."}), 404
        return jsonify({"message": "Category budget removed."}), 200
    if removed:
        flash("Category budget removed.", "success")
    return redirect(url_for("finance.finance_page"))


@finance_bp.app_context_processor
def inject_transaction_categories():
    """For the Add sheet every page carries (partials/add_sheet.html)."""
    return {"transaction_categories": TRANSACTION_CATEGORIES}


def mark_transactions_reviewed(user, ids) -> int:
    """Clears needs_review on these of `user`'s transactions (they look
    right as they are; editing one counts as a review on its own, see
    update_transaction_fields). Ids that aren't theirs are ignored, at
    most 500 at a time. Returns how many are still waiting. Shared by the
    web Activity page and the native app's API."""
    if ids:
        Transaction.query.filter(
            Transaction.user_id == user.id, Transaction.id.in_(list(ids)[:500])
        ).update({"needs_review": False}, synchronize_session=False)
        db.session.commit()
    return Transaction.query.filter_by(user_id=user.id, needs_review=True).count()


# ---------------------------------------------------------------------------
# Activity and Plan: the redesigned web pages (2026-10). Both render the
# same dicts the native app's API returns (services/screens.py), so the
# website and the app always agree.
# ---------------------------------------------------------------------------

ACTIVITY_PAGE_SIZE = 60


def _day_label(day: date, today: date) -> str:
    if day == today:
        return "Today"
    if day == today - timedelta(days=1):
        return "Yesterday"
    if day > today:
        return day.strftime("%A, %b %-d") if day.year == today.year else day.strftime("%a, %b %-d, %Y")
    if (today - day).days < 7:
        return day.strftime("%A")
    if day.year == today.year:
        return day.strftime("%A, %b %-d")
    return day.strftime("%a, %b %-d, %Y")


def group_transactions_by_day(transactions: list[dict], today: date) -> list[dict]:
    """The flat, newest-first list from screens.transactions_screen,
    grouped under one header per day, each with that day's spending in the
    display currency (a foreign purchase counts at its converted
    display_amount, never its foreign number)."""
    groups: list[dict] = []
    for tx in transactions:
        day = date.fromisoformat(tx["date"]) if tx.get("date") else today
        if not groups or groups[-1]["date"] != day:
            groups.append({"date": day, "iso": day.isoformat(), "label": _day_label(day, today),
                           "spent": Decimal("0"), "items": []})
        group = groups[-1]
        group["items"].append(tx)
        if tx["type"] == "expense":
            group["spent"] += Decimal(tx["display_amount"] or tx["amount"])
        tx["upcoming"] = day > today
    return groups


def _activity_filters(args) -> dict:
    """The filters Activity understands, cleaned, for both the query and
    the page's own controls."""
    month = (args.get("month") or "").strip()
    t_type = (args.get("type") or "").strip()
    return {
        "q": (args.get("q") or "").strip()[:100],
        "type": t_type if t_type in ("expense", "income") else "",
        "month": month if re.fullmatch(r"\d{4}-\d{2}", month) else "",
        "category": (args.get("category") or "").strip(),
        "review": "1" if args.get("review") == "1" else "",
    }


@finance_bp.route("/activity")
@login_required
def activity_page():
    from ...services.screens import ScreenError, transactions_screen

    try:
        generate_due_recurring(current_user)
    except Exception:
        db.session.rollback()

    filters = _activity_filters(request.args)
    try:
        offset = max(0, int(request.args.get("offset", 0)))
    except ValueError:
        offset = 0
    try:
        screen = transactions_screen(current_user, {**filters, "limit": ACTIVITY_PAGE_SIZE, "offset": offset})
    except ScreenError:
        filters = {key: "" for key in filters}
        screen = transactions_screen(current_user, {"limit": ACTIVITY_PAGE_SIZE})
    today = date.fromisoformat(screen["today"])
    groups = group_transactions_by_day(screen["transactions"], today)

    # Only the rows, for "Show more" (static/js/activity.js appends them).
    if request.args.get("partial") == "1":
        return render_template("partials/activity_rows.html", groups=groups, screen=screen,
                               categories=TRANSACTION_CATEGORIES)

    month_label = None
    if filters["month"]:
        month_label = datetime.strptime(filters["month"], "%Y-%m").strftime("%B %Y")
    category_labels = [
        TRANSACTION_CATEGORIES.get(key, {}).get("label", "Uncategorized" if key == "none" else key)
        for key in filters["category"].split(",") if key
    ]
    return render_template(
        "activity.html",
        screen=screen,
        groups=groups,
        filters=filters,
        month_label=month_label,
        category_labels=category_labels,
        to_review=Transaction.query.filter_by(user_id=current_user.id, needs_review=True).count(),
        uncategorized=Transaction.query.filter_by(user_id=current_user.id, category=None).count(),
        months=months_with_transactions(current_user.id, today),
        categories=TRANSACTION_CATEGORIES,
        selected=request.args.get("selected", type=int),
    )


def unpair_transaction_record(user, tx: Transaction) -> tuple[Transaction, Transaction]:
    """Splits a bill and the charge Artha paired on `tx` back into two
    transactions (bill_matching.unpair), for the web Activity editor and
    the app alike. Raises bill_matching.NotPaired when there's nothing to
    split; any other exception is a real failure (already rolled back)."""
    try:
        bill, charge = bill_matching.unpair(tx)
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    if tx.type == "expense":
        _check_budget_overage_alerts(user.id)
    return bill, charge


@finance_bp.route("/activity/<int:tx_id>/unpair", methods=["POST"])
@login_required
def activity_unpair(tx_id):
    tx = db.session.get(Transaction, tx_id)
    if tx is None or tx.user_id != current_user.id:
        return jsonify({"message": "That transaction doesn't exist anymore."}), 404
    try:
        bill, charge = unpair_transaction_record(current_user, tx)
    except bill_matching.NotPaired as exc:
        return jsonify({"message": str(exc)}), 400
    except Exception as e:
        log.error("Error unpairing transaction %s: %s", tx_id, e, exc_info=True)
        return jsonify({"message": "Artha couldn't split those. Try again in a moment."}), 500
    return jsonify({"bill_id": bill.id, "charge_id": charge.id})


def bill_choices(user, tx: Transaction) -> list[dict]:
    """The bills `tx` could be paired with by hand, likeliest first, for
    the web Activity editor and the app alike."""
    from ...services.screens import serialize_transaction

    currency = user.preferred_currency or "USD"
    rates = get_rates() if currency != "USD" else None
    return [serialize_transaction(bill, currency, rates) for bill in bill_matching.bills_for(tx)]


def pair_transaction_record(user, tx: Transaction, bill_id) -> Transaction:
    """Pairs charge `tx` with the bill the person picked
    (bill_matching.pair_by_hand). Raises bill_matching.CannotPair for a
    bill that isn't one of its choices; any other exception is a real
    failure (already rolled back). Returns the bill, which now holds the
    charge."""
    bill = db.session.get(Transaction, bill_id) if isinstance(bill_id, int) and not isinstance(bill_id, bool) else None
    if bill is None or bill.user_id != user.id:
        raise bill_matching.CannotPair("That bill doesn't exist anymore.")
    try:
        paired = bill_matching.pair_by_hand(tx, bill)
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    return paired


@finance_bp.route("/activity/<int:tx_id>/bills")
@login_required
def activity_bills(tx_id):
    tx = db.session.get(Transaction, tx_id)
    if tx is None or tx.user_id != current_user.id:
        return jsonify({"message": "That transaction doesn't exist anymore."}), 404
    return jsonify({"bills": bill_choices(current_user, tx)})


@finance_bp.route("/activity/<int:tx_id>/pair", methods=["POST"])
@login_required
def activity_pair(tx_id):
    tx = db.session.get(Transaction, tx_id)
    if tx is None or tx.user_id != current_user.id:
        return jsonify({"message": "That transaction doesn't exist anymore."}), 404
    data = request.get_json(silent=True) or {}
    try:
        bill = pair_transaction_record(current_user, tx, data.get("bill_id"))
    except bill_matching.CannotPair as exc:
        return jsonify({"message": str(exc)}), 400
    except Exception as e:
        log.error("Error pairing transaction %s: %s", tx_id, e, exc_info=True)
        return jsonify({"message": "Artha couldn't pair those. Try again in a moment."}), 500
    return jsonify({"bill_id": bill.id})


def keep_new_price_record(user, tx: Transaction) -> Transaction:
    """The person says the amount a bill was just charged is its price from
    now on (bill_matching.keep_new_price), for the web Activity editor and
    the app alike. Raises bill_matching.NotAPriceChange when there's no new
    amount to keep."""
    try:
        bill_matching.keep_new_price(tx)
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    if tx.type == "expense":
        _check_budget_overage_alerts(user.id)
    return tx


@finance_bp.route("/activity/<int:tx_id>/new-price", methods=["POST"])
@login_required
def activity_new_price(tx_id):
    tx = db.session.get(Transaction, tx_id)
    if tx is None or tx.user_id != current_user.id:
        return jsonify({"message": "That transaction doesn't exist anymore."}), 404
    try:
        keep_new_price_record(current_user, tx)
    except bill_matching.NotAPriceChange as exc:
        return jsonify({"message": str(exc)}), 400
    except Exception as e:
        log.error("Error keeping a new price on %s: %s", tx_id, e, exc_info=True)
        return jsonify({"message": "Artha couldn't save that. Try again in a moment."}), 500
    return jsonify({"id": tx.id})


@finance_bp.route("/activity/<int:tx_id>/split", methods=["POST"])
@login_required
def activity_split(tx_id):
    tx = db.session.get(Transaction, tx_id)
    if tx is None or tx.user_id != current_user.id:
        return jsonify({"message": "That transaction doesn't exist anymore."}), 404
    data = request.get_json(silent=True) or {}
    try:
        lead = split_transaction_record(current_user, tx, data.get("parts"))
    except splits.SplitError as exc:
        return jsonify({"message": str(exc)}), 400
    except Exception as e:
        log.error("Error splitting transaction %s: %s", tx_id, e, exc_info=True)
        return jsonify({"message": "Artha couldn't split that. Try again in a moment."}), 500
    return jsonify({"id": lead.id})


@finance_bp.route("/activity/<int:tx_id>/unsplit", methods=["POST"])
@login_required
def activity_unsplit(tx_id):
    tx = db.session.get(Transaction, tx_id)
    if tx is None or tx.user_id != current_user.id:
        return jsonify({"message": "That transaction doesn't exist anymore."}), 404
    try:
        joined = unsplit_transaction_record(current_user, tx)
    except Exception as e:
        log.error("Error unsplitting transaction %s: %s", tx_id, e, exc_info=True)
        return jsonify({"message": "Artha couldn't join that back. Try again in a moment."}), 500
    return jsonify({"id": joined.id})


@finance_bp.route("/activity/reviewed", methods=["POST"])
@login_required
def activity_reviewed():
    data = request.get_json(silent=True) or {}
    ids = data.get("ids")
    if not isinstance(ids, list) or not all(isinstance(i, int) and not isinstance(i, bool) for i in ids):
        return jsonify({"message": "Send the ids as a list of numbers."}), 400
    return jsonify({"to_review": mark_transactions_reviewed(current_user, ids)})


@finance_bp.route("/plan")
@login_required
def plan_page():
    from ...services.screens import history_screen, plan_screen

    try:
        generate_due_recurring(current_user)
    except Exception:
        db.session.rollback()

    plan = plan_screen(current_user)
    history = history_screen(current_user, 6)
    today = date.fromisoformat(plan["date"])
    for bill in plan["bills"]:
        due = date.fromisoformat(bill["date"])
        bill["weekday"] = due.strftime("%a").upper()
        bill["day"] = due.day
        bill["days_away"] = (due - today).days
    for goal in plan["goals"]:
        if goal["target_date"]:
            goal["target_label"] = date.fromisoformat(goal["target_date"]).strftime("%b %Y")
    return render_template(
        "plan.html",
        plan=plan,
        history=history,
        categories=TRANSACTION_CATEGORIES,
    )


# ---------------------------------------------------------------------------
# Plan: savings goals and spotted bills (services/goals.py and
# services/recurring_detect.py, shared with the native app's API). JSON in
# and out; the page reloads after each, like the rest of Plan.
# ---------------------------------------------------------------------------

def _own_goal(goal_id: int):
    from ...models import Goal

    goal = db.session.get(Goal, goal_id)
    if goal is None or goal.user_id != current_user.id:
        return None
    return goal


def _payload():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else request.form


@finance_bp.route("/plan/goals", methods=["POST"])
@login_required
def create_goal():
    from ...services.goals import GoalError, describe_goal, save_goal

    try:
        goal = save_goal(current_user, None, _payload())
    except GoalError as exc:
        return jsonify({"message": str(exc)}), 400
    return jsonify({"goal": describe_goal(goal, user_today(current_user))}), 201


@finance_bp.route("/plan/goals/<int:goal_id>", methods=["POST"])
@login_required
def update_goal(goal_id):
    from ...services.goals import GoalError, describe_goal, save_goal

    goal = _own_goal(goal_id)
    if goal is None:
        return jsonify({"message": "That goal is gone."}), 404
    try:
        save_goal(current_user, goal, _payload(), partial=True)
    except GoalError as exc:
        return jsonify({"message": str(exc)}), 400
    return jsonify({"goal": describe_goal(goal, user_today(current_user))})


@finance_bp.route("/plan/goals/<int:goal_id>/add", methods=["POST"])
@login_required
def add_money_to_goal(goal_id):
    from ...services.goals import GoalError, add_to_goal, describe_goal

    goal = _own_goal(goal_id)
    if goal is None:
        return jsonify({"message": "That goal is gone."}), 404
    try:
        add_to_goal(goal, _payload().get("amount"))
    except GoalError as exc:
        return jsonify({"message": str(exc)}), 400
    return jsonify({"goal": describe_goal(goal, user_today(current_user))})


@finance_bp.route("/plan/goals/<int:goal_id>/delete", methods=["POST"])
@login_required
def remove_goal(goal_id):
    from ...services.goals import delete_goal

    goal = _own_goal(goal_id)
    if goal is None:
        return jsonify({"message": "That goal is gone."}), 404
    delete_goal(goal)
    return jsonify({"message": "Goal deleted."})


@finance_bp.route("/plan/bills/track", methods=["POST"])
@login_required
def track_bill_suggestion():
    from ...services.recurring_detect import SuggestionError, track_suggestion

    try:
        tx = track_suggestion(current_user, str(_payload().get("key") or ""), user_today(current_user))
    except SuggestionError as exc:
        return jsonify({"message": str(exc)}), 404
    return jsonify({"message": f"{tx.description} is a bill now.", "id": tx.id})


@finance_bp.route("/plan/bills/dismiss", methods=["POST"])
@login_required
def dismiss_bill_suggestion():
    from ...services.recurring_detect import SuggestionError, dismiss_suggestion

    try:
        dismiss_suggestion(current_user, str(_payload().get("key") or ""))
    except SuggestionError as exc:
        return jsonify({"message": str(exc)}), 400
    return jsonify({"message": "Got it."})
