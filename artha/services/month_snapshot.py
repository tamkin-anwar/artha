"""This month's money picture for one user: spending and income in their
own display currency, the budget, bills still due, and Safe to Spend.

Extracted from the Dashboard (dashboard/routes.py's index()) when the
native app's API needed the same figures (api/routes.py's /today), so the
web Dashboard and the iPhone app compute every number the same way, from
the same code, instead of drifting apart. The comments on each step are
the Dashboard's own, moved here with the logic they explain.
"""

import calendar as cal
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func

from ..extensions import db
from ..models import Transaction
from ..models.budget import Budget
from ..utils import budget_status, current_month_bounds, next_due_date, recurring_occurrences, user_today
from .bill_matching import expected_amount, expected_usd
from .exchange_rate_service import convert_amount, convert_usd_to, get_rates

# How far ahead of an even pace (as a share of the monthly budget) spending
# can run before the month reads "ahead of pace" instead of "on pace".
AHEAD_OF_PACE_MARGIN = Decimal("0.05")


@dataclass
class MonthSnapshot:
    today: date
    month_start: datetime
    month_end: datetime
    display_currency: str
    rates: dict | None
    transactions: list
    income: Decimal
    expense: Decimal
    recurring_templates: dict
    renewals_this_week: list
    renewals_total: float
    budget: dict
    safe_to_spend: float | None
    safe_to_spend_upcoming: float
    safe_to_spend_unposted: float

    @property
    def days_in_month(self) -> int:
        return cal.monthrange(self.today.year, self.today.month)[1]

    @property
    def days_left(self) -> int:
        """Today counts as a day left: Safe to Spend still has today to
        spend in."""
        return self.days_in_month - self.today.day + 1

    @property
    def even_pace_spent(self) -> Decimal | None:
        """What spending evenly across the month would have reached by the
        end of today, or None without a budget."""
        if not self.budget.get("has_budget"):
            return None
        cap = Decimal(str(self.budget["cap"]))
        return cap * self.today.day / self.days_in_month

    @property
    def pace_status(self) -> str | None:
        """"over" once spending plus bills still due exceeds the budget,
        "ahead" when spending is running more than AHEAD_OF_PACE_MARGIN of
        the budget past an even pace, otherwise "on_pace". None without a
        budget."""
        if self.safe_to_spend is None:
            return None
        if self.safe_to_spend < 0:
            return "over"
        cap = Decimal(str(self.budget["cap"]))
        if self.expense > self.even_pace_spent + cap * AHEAD_OF_PACE_MARGIN:
            return "ahead"
        return "on_pace"


def build_month_snapshot(user, today: date | None = None) -> MonthSnapshot:
    uid = user.id
    today = today or user_today(user)

    # Dashboard cards are scoped to the current calendar month — same
    # default the /finance page's month tabs use. This used to sum every
    # transaction the user ever entered, all-time, which made the cards
    # both misleading and inconsistent with the rest of the app.
    month_start, month_end = current_month_bounds(today)

    transactions = (
        Transaction.query.filter(
            Transaction.user_id == uid,
            Transaction.timestamp >= month_start,
            Transaction.timestamp < month_end,
        )
        .order_by(Transaction.position.asc(), Transaction.id.asc())
        .all()
    )
    # coalesce(usd_value, amount), not bare amount: transactions can now
    # carry different currencies, so summing raw amount across them would
    # be meaningless. A legacy/USD row (usd_value NULL) still just sums
    # its own amount, unchanged — see Transaction.value_in_usd's docstring
    # for why that fallback is correct, not a workaround.
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
        or 0
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
        or 0
    )
    # Converted from the USD-pivot query result to this user's own
    # preferred_currency (their currency-less monthly budget cap, further
    # down, is implicitly in that same currency — see budget_status()'s
    # call site) so the stat cards, and the budget comparison, both work
    # in the currency the user actually sees everywhere else.
    display_currency = user.preferred_currency or "USD"
    rates = get_rates() if display_currency != "USD" else None
    income = convert_usd_to(Decimal(income), display_currency, rates)
    expense = convert_usd_to(Decimal(expense), display_currency, rates)

    # ------------------------------------------------------------------
    # Recurring renewals landing within the next 7 days — same
    # dedup-by-(description,type)-then-next_due_date() approach as the
    # calendar page's "upcoming recurring" banner, just widened from one
    # nearest hit to the whole week for a dashboard callout.
    # ------------------------------------------------------------------
    recurring_rows = Transaction.query.filter_by(user_id=uid, is_recurring=True).all()
    templates_by_key: dict[tuple[str, str], Transaction] = {}
    for t in recurring_rows:
        key = (t.description, t.type)
        current = templates_by_key.get(key)
        if current is None or (t.timestamp and current.timestamp and t.timestamp > current.timestamp):
            templates_by_key[key] = t

    renewals_this_week = []
    for (desc, ttype), tx in templates_by_key.items():
        due = next_due_date(tx, today)
        if due is not None and 0 <= (due - today).days <= 7:
            renewals_this_week.append({
                "description": desc,
                "type": ttype,
                # What the bill expects next, not a one-off it was last
                # charged (bill_matching.expected_amount).
                "amount": float(expected_amount(tx)),
                "currency": tx.native_currency,
                "usd_amount": float(expected_usd(tx)),
                "due": due,
                "due_label": "Today" if due == today else f"{cal.month_abbr[due.month]} {due.day}",
            })
    renewals_this_week.sort(key=lambda e: e["due"])
    # usd_amount, not amount: bills due this week can be in different
    # currencies now, so a raw sum across them would be meaningless.
    renewals_total_usd = sum(e["usd_amount"] for e in renewals_this_week if e["type"] == "expense")
    renewals_total = float(convert_usd_to(Decimal(str(renewals_total_usd)), display_currency, rates))

    budget_row = Budget.query.filter_by(user_id=uid).first()
    # The cap's own currency (its NULL-means-USD fallback, same as
    # Transaction) converted to display_currency before comparing --
    # expense is already in display_currency, and a cap left
    # unconverted here is exactly the bug a real user hit the day this
    # shipped: a $5,000 cap silently misread as a 5,000-of-whatever-
    # you-just-switched-to cap.
    budget_cap = (
        convert_amount(budget_row.monthly_cap, budget_row.currency or "USD", display_currency, rates)
        if budget_row else None
    )
    budget = budget_status(budget_cap, expense)

    # ------------------------------------------------------------------
    # "Safe to spend" -- the YNAB-inspired figure from CLAUDE.md's
    # Competitive ambition section (Batch 2), done passively: no
    # methodology to opt into, it just reflects the rest of this
    # month's budget headroom. Deliberately not just cap-minus-spent
    # (that's already the existing Monthly Budget bar) -- it also nets
    # out recurring bills still expected to land before month-end,
    # reusing templates_by_key (built above for renewals_this_week)
    # with a wider window: through the last day of the month rather
    # than the next 7 days. Every remaining occurrence counts, not just
    # the next one (a weekly bill can still land 3-4 more times this
    # month), and any occurrence that already exists as a real
    # transaction is skipped -- it's already inside `spent`. That second
    # rule matters: generate_due_recurring() creates each monthly bill's
    # copy for this month up front, so counting it again here would
    # subtract the same bill twice. Only set when a budget actually
    # exists -- there's no ceiling to be "safe" against otherwise.
    # ------------------------------------------------------------------
    safe_to_spend = None
    safe_to_spend_upcoming = 0.0
    unposted = 0.0
    if budget["has_budget"]:
        month_last_day = (month_end - timedelta(days=1)).date()
        posted_in_month = {(t.description, t.type) for t in transactions}
        # occurrence_day: a weekly bill's due date, which stays put when its
        # real charge landed a day or two off (services/bill_matching.py).
        posted_on_day = {(t.description, t.type, t.occurrence_day) for t in transactions}
        # Two totals: everything still dated today-or-later this month
        # (what the "after $X in upcoming bills" line tells the user), and
        # just the part of that not yet posted (what actually gets
        # subtracted, since a posted-but-future-dated copy is already in
        # `spent`). Same final figure either way you add it up.
        upcoming_usd = Decimal("0")
        unposted_usd = Decimal("0")
        for (desc, ttype), tx in templates_by_key.items():
            if ttype != "expense":
                continue
            bill_usd = expected_usd(tx)
            for due in recurring_occurrences(tx, today, month_last_day):
                upcoming_usd += bill_usd
                if tx.recurrence_interval in ("weekly", "biweekly"):
                    posted = (desc, ttype, due) in posted_on_day
                else:
                    posted = (desc, ttype) in posted_in_month
                if not posted:
                    unposted_usd += bill_usd
        safe_to_spend_upcoming = float(convert_usd_to(upcoming_usd, display_currency, rates))
        unposted = float(convert_usd_to(unposted_usd, display_currency, rates))
        safe_to_spend = budget["cap"] - budget["spent"] - unposted

    return MonthSnapshot(
        today=today,
        month_start=month_start,
        month_end=month_end,
        display_currency=display_currency,
        rates=rates,
        transactions=transactions,
        income=income,
        expense=expense,
        recurring_templates=templates_by_key,
        renewals_this_week=renewals_this_week,
        renewals_total=renewals_total,
        budget=budget,
        safe_to_spend=safe_to_spend,
        safe_to_spend_upcoming=safe_to_spend_upcoming,
        safe_to_spend_unposted=unposted,
    )
