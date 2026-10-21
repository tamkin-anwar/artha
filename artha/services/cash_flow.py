"""Money in and out, month by month.

Shared by the web Finance page's Cash Flow tab (finance.routes'
finance_breakdown) and the native app's month-by-month view
(GET /api/v1/history), so both bucket the same transactions the same way.
Totals stay in USD (each transaction's locked `value_in_usd`) until the
caller converts them to the person's display currency, the same
"USD internally, converted once right before use" rule as everywhere else.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal

from ..models import Transaction


@dataclass
class MonthTotals:
    start: date
    income_usd: Decimal = Decimal("0")
    expense_usd: Decimal = Decimal("0")
    # Spending only, by category key (None for uncategorized).
    expense_by_category_usd: dict = field(default_factory=lambda: defaultdict(Decimal))
    transaction_count: int = 0

    @property
    def key(self) -> str:
        return self.start.strftime("%Y-%m")


def month_starts(start: date, end: date) -> list[date]:
    """The first of every month from `start`'s month up to, not including,
    `end` (itself a first of the month)."""
    months = []
    year, month = start.year, start.month
    while date(year, month, 1) < end:
        months.append(date(year, month, 1))
        month += 1
        if month == 13:
            month, year = 1, year + 1
    return months


def months_back(today: date, count: int) -> tuple[date, date]:
    """(start, end_exclusive) covering the last `count` calendar months,
    this one included."""
    year, month = today.year, today.month
    for _ in range(count - 1):
        month -= 1
        if month == 0:
            month, year = 12, year - 1
    end = date(today.year + 1, 1, 1) if today.month == 12 else date(today.year, today.month + 1, 1)
    return date(year, month, 1), end


def monthly_totals(user_id: int, start: date, end: date) -> list[MonthTotals]:
    """One MonthTotals per calendar month in [start, end), oldest first,
    including months with nothing in them."""
    buckets = {d.strftime("%Y-%m"): MonthTotals(start=d) for d in month_starts(start, end)}
    rows = Transaction.query.filter(
        Transaction.user_id == user_id,
        Transaction.timestamp >= datetime(start.year, start.month, start.day, tzinfo=timezone.utc),
        Transaction.timestamp < datetime(end.year, end.month, end.day, tzinfo=timezone.utc),
    ).all()
    for tx in rows:
        if not tx.timestamp:
            continue
        bucket = buckets.get(tx.timestamp.strftime("%Y-%m"))
        if bucket is None:
            continue
        bucket.transaction_count += 1
        if tx.type == "income":
            bucket.income_usd += tx.value_in_usd
        elif tx.type == "expense":
            bucket.expense_usd += tx.value_in_usd
            bucket.expense_by_category_usd[tx.category] += tx.value_in_usd
    return list(buckets.values())
