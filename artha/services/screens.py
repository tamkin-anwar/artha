"""What each of Artha's money screens shows (Today, Plan, Month by month,
Wealth and the transaction list), built once and used twice: the native
app's JSON API (blueprints/api/routes.py) returns these dicts as-is, and
the website's redesigned pages (2026-10) render the very same dicts. So a
figure can never read one way on the phone and another in the browser,
and a fix to one is a fix to both (CLAUDE.md's "extract the web route's
logic into a shared function" rule, applied the other way round, since
the app's screens came first).

Money is always a two-decimal string ("1284.01"), never a float. A single
transaction or account is in its own currency; any total is in the
person's display currency, named in the payload. Templates format these
strings with the `money` filter (utils.format_money).
"""

import calendar as cal
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

from ..extensions import db
from ..models import Transaction
from ..models.account import ACCOUNT_TYPES, Account, NetWorthSnapshot
from ..models.category_budget import CategoryBudget
from ..utils import CURRENCY_CODES, current_month_bounds, next_due_date, user_today
from . import bill_matching
from .cash_flow import monthly_totals, months_back
from .exchange_rate_service import convert_amount, convert_usd_to, get_rates
from .month_plan import bills_this_month, typical_spending
from .month_snapshot import build_month_snapshot

# How far ahead "Coming up" looks for recurring bills, and how many it
# returns. The app's Today card and widgets show only the first three; the
# rest are there so the app can schedule a reminder the day before each
# bill (a reminder for a bill it never heard about can't be set).
COMING_UP_WINDOW_DAYS = 31
COMING_UP_LIMIT = 30

RECENT_LIMIT = 5
TRANSACTIONS_PAGE_DEFAULT = 50
TRANSACTIONS_PAGE_MAX = 200

HISTORY_MONTHS_DEFAULT = 6
HISTORY_MONTHS_MAX = 12

# How the app groups accounts, in the order it shows them.
ACCOUNT_GROUPS = {
    "checking": "cash", "savings": "cash",
    "investment": "investments",
    "real_estate": "property", "vehicle": "property", "other_asset": "property",
    "credit_card": "debts", "loan": "debts", "other_liability": "debts",
}

_CENT = Decimal("0.01")


class ScreenError(ValueError):
    """A request the screen can't answer as asked (a malformed month, an
    unknown type). The message is written for the person."""


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


def _transaction_categories() -> dict:
    # Imported lazily: finance.routes imports from services, so a
    # module-level import here would be circular.
    from ..blueprints.finance.routes import TRANSACTION_CATEGORIES

    return TRANSACTION_CATEGORIES


def category_label(key: str | None) -> str:
    if key is None:
        return "Uncategorized"
    return _transaction_categories().get(key, {}).get("label", key)


def month_info(snap) -> dict:
    return {
        "name": cal.month_name[snap.today.month],
        "start": snap.month_start.date().isoformat(),
        "end": (snap.month_end - timedelta(days=1)).date().isoformat(),
        "day": snap.today.day,
        "days_in_month": snap.days_in_month,
        "days_left": snap.days_left,
    }


def _paired_charge(tx: Transaction) -> str | None:
    pairing = tx.pairing if tx.bill_matched else None
    if not isinstance(pairing, dict) or pairing.get("kind") not in ("absorbed", "adopted"):
        return None
    return (pairing.get("charge") or {}).get("description") or tx.description


def serialize_transaction(tx: Transaction, display_currency: str, rates) -> dict:
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
        "category_label": category_label(tx.category),
        "date": tx.timestamp.date().isoformat() if tx.timestamp else None,
        "is_recurring": bool(tx.is_recurring),
        "recurrence_interval": (tx.recurrence_interval or "monthly") if tx.is_recurring else None,
        "recurring_end_date": tx.recurring_end_date.isoformat() if tx.is_recurring and tx.recurring_end_date else None,
        # "manual", "csv" (statement import), "apple_pay", or null (older rows).
        "source": tx.import_source,
        "needs_review": bool(tx.needs_review),
        # The real charge's own name when Artha paired it with this bill
        # (services/bill_matching.py) and the pairing can still be split;
        # null otherwise.
        "paired_charge": _paired_charge(tx),
        # A real charge that could still be paired with a bill by hand
        # (GET .../bills lists which).
        "can_pair": bill_matching.can_pair(tx),
        "display_amount": None,
    }
    if tx.native_currency != display_currency:
        data["display_amount"] = money(convert_usd_to(tx.value_in_usd, display_currency, rates))
    return data


def serialize_account(account: Account, display_currency: str, rates, now: datetime) -> dict:
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


def apple_pay_status(user) -> dict | None:
    """The newest purchase Apple Pay logged on its own, so the app can say
    the automation is working ("Last logged Shell, Oct 21") instead of
    leaving someone to wonder. None until the first one arrives."""
    last = (
        Transaction.query
        .filter_by(user_id=user.id, import_source="apple_pay")
        .order_by(Transaction.timestamp.desc(), Transaction.id.desc())
        .first()
    )
    if last is None:
        return None
    return {
        "description": last.description,
        "date": last.timestamp.date().isoformat() if last.timestamp else None,
        "count": Transaction.query.filter_by(user_id=user.id, import_source="apple_pay").count(),
    }


# ---------------------------------------------------------------------------
# Today
# ---------------------------------------------------------------------------

def today_screen(user, today=None) -> dict:
    """Everything Today shows, in one dict. Callers run the recurring
    catch-up first (generate_due_recurring), as every screen that reads
    this month does. `today` defaults to the person's own date
    (utils.user_today)."""
    from ..blueprints.accounts.routes import net_worth_summary
    from .recurring_detect import suggest_bills

    snap = build_month_snapshot(user, today)
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
            {"key": key, "label": category_label(key), "amount": convert_usd_to(total, currency, rates)}
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

    return {
        "date": today_date.isoformat(),
        "currency": currency,
        "month": month_info(snap),
        "spent": money(snap.expense),
        "income": money(snap.income),
        "budget": budget,
        "safe_to_spend": safe_to_spend,
        "spending_by_day": spending_by_day,
        "categories": categories,
        "coming_up": coming_up,
        "recent": [serialize_transaction(tx, currency, rates) for tx in recent_rows],
        "net_worth": net_worth,
        # Purchases logged on their own and not looked at yet, from any
        # month (Transaction.needs_review).
        "to_review": Transaction.query.filter_by(user_id=user.id, needs_review=True).count(),
        "apple_pay": apple_pay_status(user),
        # How many charges look like bills nobody marked; Plan lists them.
        "bill_suggestions": len(suggest_bills(user, today_date)),
    }


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------

def transactions_screen(user, args) -> dict:
    """Newest first. `args` is anything with .get() (request.args): ?limit=
    (default 50, at most 200) and ?offset= page through the rest;
    `next_offset` is null on the last page. ?q= keeps only descriptions
    containing that text (any case), and ?type=expense or ?type=income
    keeps only that kind. ?month=YYYY-MM keeps only that calendar month
    (the same bounds Today's totals use), and ?category=dining,groceries
    keeps only those categories ("none" means uncategorized), so Today's
    "Where October went" can open the transactions behind each of its
    numbers. ?review=1 keeps only the ones waiting to be reviewed
    (Transaction.needs_review). Raises ScreenError on a malformed filter."""
    try:
        limit = int(args.get("limit", TRANSACTIONS_PAGE_DEFAULT))
        offset = int(args.get("offset", 0))
    except (TypeError, ValueError):
        raise ScreenError("limit and offset must be whole numbers.")
    limit = max(1, min(limit, TRANSACTIONS_PAGE_MAX))
    offset = max(0, offset)

    query = Transaction.query.filter_by(user_id=user.id)
    search = (args.get("q") or "").strip()[:100]
    if search:
        # % and _ are wildcards to LIKE; escape them so a search for
        # "50%" means the text "50%", not "50 followed by anything".
        escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        query = query.filter(Transaction.description.ilike(f"%{escaped}%", escape="\\"))
    t_type = (args.get("type") or "").strip()
    if t_type:
        if t_type not in ("expense", "income"):
            raise ScreenError("type must be expense or income.")
        query = query.filter(Transaction.type == t_type)
    month = (args.get("month") or "").strip()
    if month:
        try:
            start, end = current_month_bounds(datetime.strptime(month, "%Y-%m").date())
        except ValueError:
            raise ScreenError("month must look like 2026-10.")
        query = query.filter(Transaction.timestamp >= start, Transaction.timestamp < end)
    categories = [c.strip() for c in (args.get("category") or "").split(",") if c.strip()][:20]
    if categories:
        named = [c for c in categories if c != "none"]
        conditions = [Transaction.category.in_(named)] if named else []
        if "none" in categories:
            conditions.append(Transaction.category.is_(None))
        query = query.filter(db.or_(*conditions))
    if args.get("review") == "1":
        query = query.filter(Transaction.needs_review.is_(True))

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

    return {
        "currency": currency,
        # The person's own date, so the app can tell bills posted ahead for
        # later this month apart from ones already paid without trusting
        # the phone's clock or timezone.
        "today": user_today(user).isoformat(),
        "transactions": [serialize_transaction(tx, currency, rates) for tx in rows],
        "next_offset": offset + limit if has_more else None,
    }


# ---------------------------------------------------------------------------
# Month by month
# ---------------------------------------------------------------------------

def history_screen(user, count: int = HISTORY_MONTHS_DEFAULT) -> dict:
    """The last `count` (at most 12) calendar months, this one included,
    oldest first: what came in, what went out, and spending by category
    each month, biggest first. Months before the first one with any
    transactions are left off, so a new account doesn't open on a row of
    empty bars. `average_spent` covers only finished, full months that had
    spending (not this one, which is still going, nor a first month that
    began partway through, which carries `started`), or is null when there
    are none. Bucketed by services.cash_flow, the same code as the web
    Cash Flow tab."""
    count = max(1, min(int(count), HISTORY_MONTHS_MAX))

    currency = user.preferred_currency or "USD"
    rates = get_rates() if currency != "USD" else None
    today = user_today(user)
    start, end = months_back(today, count)
    months = monthly_totals(user.id, start, end)
    while len(months) > 1 and months[0].transaction_count == 0:
        months.pop(0)

    current_key = today.strftime("%Y-%m")
    # Someone who started partway through a month has a first month that
    # only covers a few days; counting it would drag "usual" way down
    # (a first week of $13 against $2,000 months). Past the 5th, that
    # month is marked as starting on its first day and left out.
    first_ever = (
        db.session.query(db.func.min(Transaction.timestamp)).filter(Transaction.user_id == user.id).scalar()
    )
    partial_key = first_ever.strftime("%Y-%m") if first_ever is not None and first_ever.day > 5 else None
    finished = [m for m in months if m.key not in (current_key, partial_key) and m.expense_usd > 0]
    average_usd = (sum((m.expense_usd for m in finished), Decimal("0")) / len(finished)) if finished else None

    def serialize(m):
        # Every category, biggest first: the app shows the top few, and
        # compares each against the month before, which needs that month's
        # whole list (a category outside its top few still had spending).
        top = sorted(m.expense_by_category_usd.items(), key=lambda kv: kv[1], reverse=True)
        return {
            "month": m.key,
            "label": cal.month_abbr[m.start.month],
            "name": cal.month_name[m.start.month],
            "year": m.start.year,
            "is_current": m.key == current_key,
            # Set only on a first month that began partway through.
            "started": first_ever.date().isoformat() if m.key == partial_key else None,
            "spent": money(convert_usd_to(m.expense_usd, currency, rates)),
            "income": money(convert_usd_to(m.income_usd, currency, rates)),
            "categories": [
                {"key": key, "label": category_label(key), "amount": money(convert_usd_to(total, currency, rates))}
                for key, total in top
            ],
        }

    return {
        "currency": currency,
        "months": [serialize(m) for m in months],
        "average_spent": money(convert_usd_to(average_usd, currency, rates)) if average_usd is not None else None,
    }


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------

def plan_screen(user) -> dict:
    """The month as a plan: the budget split into bills, everyday spending
    and what's left (the same math as Today's Safe to Spend, see
    services/month_plan.py), this month's bills paid and still to come,
    category limits, and what this person usually spends, to suggest
    amounts instead of making them guess. Callers run the recurring
    catch-up first."""
    from ..blueprints.finance.routes import category_budget_statuses, category_rollover_carry
    from .goals import goals_for
    from .recurring_detect import suggest_bills

    categories_meta = _transaction_categories()
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
            "category_label": category_label(bill.category),
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
    carry = category_rollover_carry(user.id, rows, snap.today, currency, rates)
    category_budgets = []
    for item in category_budget_statuses(rows, snap.transactions, currency, rates, carry):
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
            # Rollover (CategoryBudget.rollover): the limit as set, and
            # what last month added (positive) or took off (negative).
            "rollover": item["rollover"],
            "base_cap": money(item["base_cap"]),
            "carried": money(item["carry"]) if item["carry"] is not None else None,
        })
    category_budgets.sort(key=lambda c: -c["percent_used"])
    budgeted = {c["key"] for c in category_budgets}

    typical = typical_spending(user, snap.today, currency, rates)

    return {
        "date": snap.today.isoformat(),
        "currency": currency,
        "month": month_info(snap),
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
            for key, meta in categories_meta.items()
            if key != "income" and key not in budgeted
        ],
        "goals": goals_for(user, snap.today),
        # Charges that look like bills nobody marked (services/
        # recurring_detect.py), each in its own currency.
        "bill_suggestions": suggest_bills(user, snap.today),
        "typical": None if typical is None else {
            "months": typical["months"],
            "overall": money(typical["overall"]),
            "by_category": {key: money(value) for key, value in typical["by_category"].items()},
        },
    }


# ---------------------------------------------------------------------------
# Wealth
# ---------------------------------------------------------------------------

def wealth_screen(user) -> dict:
    """Net worth now and over time, and every account behind it. The same
    accounts and math as the web Accounts page (accounts/routes.py); each
    balance stays in its own currency, with totals and history in the
    person's display currency. History is one point per day a balance
    changed (NetWorthSnapshot), plus today's live figure."""
    from ..blueprints.accounts.routes import _account_balances_usd

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

    return {
        "currency": currency,
        "net_worth": money(assets - liabilities) if accounts else None,
        "assets": money(assets),
        "liabilities": money(liabilities),
        "history": history,
        "accounts": [serialize_account(a, currency, rates, now) for a in accounts],
        "account_types": [
            {"key": key, "label": meta["label"], "is_liability": meta["liability"], "group": ACCOUNT_GROUPS[key]}
            for key, meta in ACCOUNT_TYPES.items()
        ],
        "currencies": sorted(CURRENCY_CODES),
    }
