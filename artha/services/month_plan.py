"""The month as a plan, for the native app's Plan tab (api/routes.py's
/plan): this month's bills, paid and still to come, and what someone
usually spends, built on top of services/month_snapshot.py so every figure
agrees with Today's Safe to Spend.

The plan splits the monthly budget the way people already think about it
(the "fixed vs. flexible" idea Monarch and Simplifi are loved for): bills
first, then everyday spending, then what's left. It is the same math as
Safe to Spend, just shown in its parts:

    left = cap - bills_total - everyday_spent
         = cap - (bills_posted + bills_unposted) - (expense - bills_posted)
         = cap - expense - bills_unposted        (Safe to Spend)
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func

from ..extensions import db
from ..models import Transaction
from ..utils import recurring_occurrences
from .exchange_rate_service import convert_usd_to
from .month_snapshot import MonthSnapshot

# How many full months before this one "usually spend" looks back over.
TYPICAL_MONTHS = 3


@dataclass
class Bill:
    description: str
    amount: Decimal
    currency: str
    usd_value: Decimal
    category: str | None
    due: date
    # "paid": posted and dated today or earlier. "scheduled": already
    # posted ahead for a later day this month (generate_due_recurring
    # creates a monthly bill's copy up front), so it's inside `spent`
    # already. "due": still to come and not posted yet.
    status: str


@dataclass
class MonthBills:
    bills: list[Bill] = field(default_factory=list)
    posted_usd: Decimal = Decimal("0")
    unposted_usd: Decimal = Decimal("0")
    paid_usd: Decimal = Decimal("0")

    @property
    def total_usd(self) -> Decimal:
        return self.posted_usd + self.unposted_usd

    @property
    def coming_usd(self) -> Decimal:
        return self.total_usd - self.paid_usd


def bills_this_month(snap: MonthSnapshot) -> MonthBills:
    """Every recurring expense in this calendar month: the copies already
    posted (from snap.transactions) plus the occurrences still to come that
    aren't posted yet, using exactly the rule Safe to Spend uses to decide
    what's posted (month_snapshot.build_month_snapshot), so the two never
    disagree about what's still owed."""
    keys = {key for key in snap.recurring_templates if key[1] == "expense"}
    result = MonthBills()

    posted_in_month: set[tuple[str, str]] = set()
    posted_on_day: set[tuple[str, str, date]] = set()
    for tx in snap.transactions:
        key = (tx.description, tx.type)
        if key not in keys or tx.timestamp is None:
            continue
        day = tx.timestamp.date()
        posted_in_month.add(key)
        posted_on_day.add((tx.description, tx.type, tx.occurrence_day))
        status = "paid" if day <= snap.today else "scheduled"
        result.bills.append(Bill(tx.description, tx.amount, tx.native_currency, tx.value_in_usd,
                                 tx.category, day, status))
        result.posted_usd += tx.value_in_usd
        if status == "paid":
            result.paid_usd += tx.value_in_usd

    month_last_day = (snap.month_end - timedelta(days=1)).date()
    for (desc, ttype), tx in snap.recurring_templates.items():
        if ttype != "expense":
            continue
        for due in recurring_occurrences(tx, snap.today, month_last_day):
            if tx.recurrence_interval in ("weekly", "biweekly"):
                posted = (desc, ttype, due) in posted_on_day
            else:
                posted = (desc, ttype) in posted_in_month
            if posted:
                continue
            result.bills.append(Bill(desc, tx.amount, tx.native_currency, tx.value_in_usd,
                                     tx.category, due, "due"))
            result.unposted_usd += tx.value_in_usd

    result.bills.sort(key=lambda b: (b.due, b.description))
    return result


def typical_spending(user, today: date, display_currency: str, rates) -> dict | None:
    """What `user` usually spends a month, overall and per category, in
    display_currency: the average over the last TYPICAL_MONTHS full months
    that have any spending at all (a brand-new account isn't averaged
    against empty months). None when there's no history yet. Used to
    suggest a budget instead of making someone guess one."""
    end = datetime(today.year, today.month, 1)
    start_month = today.month - TYPICAL_MONTHS
    start_year = today.year
    while start_month < 1:
        start_month += 12
        start_year -= 1
    start = datetime(start_year, start_month, 1)

    usd_amount = func.coalesce(Transaction.usd_value, Transaction.amount)
    rows = (
        db.session.query(Transaction.timestamp, Transaction.category, usd_amount)
        .filter(
            Transaction.user_id == user.id,
            Transaction.type == "expense",
            Transaction.timestamp >= start,
            Transaction.timestamp < end,
        )
        .all()
    )
    if not rows:
        return None

    months: set[tuple[int, int]] = set()
    by_category: dict[str, Decimal] = {}
    overall = Decimal("0")
    for timestamp, category, amount in rows:
        amount = Decimal(str(amount or 0))
        months.add((timestamp.year, timestamp.month))
        overall += amount
        if category:
            by_category[category] = by_category.get(category, Decimal("0")) + amount

    count = Decimal(len(months))
    return {
        "months": len(months),
        "overall": convert_usd_to(overall / count, display_currency, rates),
        "by_category": {
            key: convert_usd_to(total / count, display_currency, rates)
            for key, total in by_category.items()
        },
    }
