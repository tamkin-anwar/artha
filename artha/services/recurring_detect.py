"""Spotting repeating charges nobody marked as a bill: a streaming service
paid every month, a gym every two weeks, imported from a statement or
logged with Apple Pay one at a time. Rocket Money and Copilot are loved
for finding these; Artha only ever suggests, and nothing changes until the
person taps "Track it" (or "Not a bill", which it remembers).

Deliberately strict, because a wrong suggestion is worse than a missed
one: at least three charges from the same merchant, a steady weekly,
every-two-weeks or monthly rhythm, amounts that barely move, and the last
one recent enough that it's probably still going.
"""

from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from statistics import median

from ..extensions import db
from ..models import DismissedBillSuggestion, Transaction

LOOKBACK_DAYS = 200
MIN_CHARGES = 3
# How far each charge may wander from the median amount (a price rise, a
# little tax) and still count as the same bill.
AMOUNT_TOLERANCE = Decimal("0.15")
INTERVALS = {
    # name: (smallest gap, largest gap, typical gap) in days
    "weekly": (6, 8, 7),
    "biweekly": (13, 16, 14),
    "monthly": (26, 35, 30),
}
MAX_SUGGESTIONS = 8


def _merchant_key(description: str) -> str:
    from ..blueprints.finance.routes import _merchant_key as key

    return key(description)


def _interval_for(gaps: list[int]) -> str | None:
    """The rhythm every gap fits (one late or early charge allowed in a
    longer run), or None."""
    for name, (low, high, _) in INTERVALS.items():
        fitting = sum(1 for g in gaps if low <= g <= high)
        allowed_misses = 1 if len(gaps) >= 4 else 0
        if fitting >= len(gaps) - allowed_misses and fitting >= 2:
            return name
    return None


def suggest_bills(user, today: date) -> list[dict]:
    """Repeating charges worth offering to track, newest first. Each is
    {key, description, amount, currency, interval, count, last_date,
    next_date, category, transaction_id}, money as a two-decimal string in
    the charge's own currency."""
    since = datetime(today.year, today.month, today.day) - timedelta(days=LOOKBACK_DAYS)
    rows = (
        Transaction.query.filter(
            Transaction.user_id == user.id,
            Transaction.type == "expense",
            Transaction.timestamp >= since,
        )
        .order_by(Transaction.timestamp.asc(), Transaction.id.asc())
        .all()
    )
    tracked = {
        key
        for (key,) in db.session.query(Transaction.description)
        .filter(Transaction.user_id == user.id, Transaction.is_recurring.is_(True))
        .all()
    }
    tracked_keys = {_merchant_key(d) for d in tracked}
    dismissed = {
        key for (key,) in db.session.query(DismissedBillSuggestion.merchant_key)
        .filter(DismissedBillSuggestion.user_id == user.id).all()
    }

    groups: dict[str, list[Transaction]] = defaultdict(list)
    for tx in rows:
        if tx.timestamp is None or tx.is_recurring or tx.timestamp.date() > today:
            continue
        key = _merchant_key(tx.description)
        if not key or key in tracked_keys or key in dismissed:
            continue
        groups[key].append(tx)

    suggestions = []
    for key, txs in groups.items():
        # One charge per day: a second purchase the same day is a second
        # coffee, not a bill.
        by_day: dict[date, Transaction] = {}
        for tx in txs:
            by_day.setdefault(tx.timestamp.date(), tx)
        charges = [by_day[d] for d in sorted(by_day)]
        if len(charges) < MIN_CHARGES:
            continue
        # Same currency throughout, or it isn't one bill.
        if len({tx.native_currency for tx in charges}) != 1:
            continue
        amounts = [Decimal(tx.amount) for tx in charges]
        middle = Decimal(median(amounts))
        if middle <= 0 or any(abs(a - middle) > middle * AMOUNT_TOLERANCE for a in amounts):
            continue
        days = [tx.timestamp.date() for tx in charges]
        gaps = [(b - a).days for a, b in zip(days, days[1:])]
        interval = _interval_for(gaps)
        if interval is None:
            continue
        typical = INTERVALS[interval][2]
        last = days[-1]
        # Stopped a while ago: more than one and a half rhythms with no charge.
        if (today - last).days > typical * 1.5:
            continue
        latest = charges[-1]
        suggestions.append({
            "key": key,
            "description": latest.description,
            "amount": str(Decimal(latest.amount).quantize(Decimal("0.01"))),
            "currency": latest.native_currency,
            "interval": interval,
            "count": len(charges),
            "last_date": last.isoformat(),
            "next_date": (last + timedelta(days=typical)).isoformat(),
            "category": latest.category,
            "transaction_id": latest.id,
        })

    suggestions.sort(key=lambda s: s["last_date"], reverse=True)
    return suggestions[:MAX_SUGGESTIONS]


class SuggestionError(ValueError):
    """The suggestion can't be acted on (gone, or never was)."""


def track_suggestion(user, key: str, today: date) -> Transaction:
    """Makes the latest charge from that merchant the start of a repeating
    bill (finance.routes.set_recurring), at the rhythm it was spotted
    with, and commits. Raises SuggestionError if there's nothing to
    track for that key any more."""
    from ..blueprints.finance.routes import set_recurring

    match = next((s for s in suggest_bills(user, today) if s["key"] == key), None)
    if match is None:
        raise SuggestionError("That charge isn't showing as a repeating one any more.")
    tx = db.session.get(Transaction, match["transaction_id"])
    if tx is None or tx.user_id != user.id:
        raise SuggestionError("That charge isn't showing as a repeating one any more.")
    set_recurring(tx, True)
    tx.recurrence_interval = None if match["interval"] == "monthly" else match["interval"]
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    return tx


def dismiss_suggestion(user, key: str) -> None:
    """Stops suggesting this merchant. Idempotent."""
    key = (key or "").strip()[:120]
    if not key:
        raise SuggestionError("Nothing to dismiss.")
    if DismissedBillSuggestion.query.filter_by(user_id=user.id, merchant_key=key).first() is None:
        db.session.add(DismissedBillSuggestion(user_id=user.id, merchant_key=key))
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise
