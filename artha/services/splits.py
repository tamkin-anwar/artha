"""
One purchase across categories: a Costco run that's part groceries, part
household. Copilot, Monarch and YNAB all do this; here each part is its
own Transaction sharing a split_group, so every total (budgets, Safe to
Spend, Where the month went, the AI's snapshot) counts each part under
its own category without knowing splits exist. Activity shows the parts
as one purchase (screens.transactions_screen).

The parts always add up to the purchase exactly, to the cent: that's
checked here, and the clients help with it the way YNAB does (what's
left to assign, and splitting the rest evenly).
"""

import uuid
from decimal import Decimal, InvalidOperation

from ..extensions import db
from ..models import Transaction

MAX_PARTS = 20
CENT = Decimal("0.01")


class SplitError(ValueError):
    """A split that can't be saved; the message is meant for the person."""


def parts_of(tx: Transaction) -> list[Transaction]:
    """Every row of the purchase `tx` belongs to, oldest first (the first
    is the one Activity shows), or just `tx` when it isn't split."""
    if not tx.split_group:
        return [tx]
    return (
        Transaction.query
        .filter_by(user_id=tx.user_id, split_group=tx.split_group)
        .order_by(Transaction.id)
        .all()
    )


def lead_of(tx: Transaction) -> Transaction:
    return parts_of(tx)[0]


def total_of(rows: list[Transaction]) -> Decimal:
    return sum((Decimal(r.amount) for r in rows), Decimal("0"))


def can_split(tx: Transaction) -> bool:
    """A bill's own row isn't split: its amount is what the series expects
    each month, and a bill paired with its charge is one unit."""
    return not tx.is_recurring and not tx.bill_matched


def _parse_parts(raw_parts, categories: dict) -> list[tuple[Decimal, str | None]]:
    if not isinstance(raw_parts, list) or len(raw_parts) < 2:
        raise SplitError("A split needs at least two parts.")
    if len(raw_parts) > MAX_PARTS:
        raise SplitError(f"A split can have at most {MAX_PARTS} parts.")
    parsed = []
    for part in raw_parts:
        if not isinstance(part, dict):
            raise SplitError("Each part needs an amount and a category.")
        try:
            amount = Decimal(str(part.get("amount", "")).strip())
        except InvalidOperation:
            raise SplitError("Each part needs an amount.") from None
        if not amount.is_finite() or amount <= 0:
            raise SplitError("Each part needs an amount above zero.")
        if amount != amount.quantize(CENT):
            raise SplitError("Amounts can't go past the cent.")
        category = part.get("category") or None
        if category is not None and category not in categories:
            raise SplitError("That category doesn't exist.")
        parsed.append((amount, category))
    return parsed


def split(tx: Transaction, raw_parts, categories: dict) -> Transaction:
    """Splits the purchase `tx` belongs to into `raw_parts` ([{"amount":
    "12.50", "category": "groceries"}, ...]), or re-splits one that
    already is. The parts must add up to the purchase exactly. Returns the
    purchase's first row. The caller commits."""
    rows = parts_of(tx)
    lead = rows[0]
    if not can_split(lead):
        raise SplitError("A bill can't be split. Split the charge that paid it instead.")
    parts = _parse_parts(raw_parts, categories)
    total = total_of(rows)
    given = sum((amount for amount, _ in parts), Decimal("0"))
    if given != total:
        left = total - given
        raise SplitError(
            f"The parts add up to {given:.2f}, not {total:.2f}. "
            + (f"{left:.2f} is left to assign." if left > 0 else f"They're {-left:.2f} over.")
        )

    # The purchase's USD value was locked when it was saved; each part
    # gets its share of that same locked value, the last part the
    # remainder, so the parts' USD values add up to the purchase's.
    usd_total = sum((Decimal(r.usd_value) for r in rows if r.usd_value is not None), Decimal("0"))
    has_usd = all(r.usd_value is not None for r in rows)
    group = lead.split_group or uuid.uuid4().hex

    kept = list(rows)
    usd_left = usd_total
    for i, (amount, category) in enumerate(parts):
        if i < len(kept):
            row = kept[i]
        else:
            row = Transaction(
                user_id=lead.user_id, description=lead.description, type=lead.type,
                timestamp=lead.timestamp, position=lead.position, currency=lead.currency,
                import_source=lead.import_source, usd_rate_locked=lead.usd_rate_locked,
                is_recurring=False,
            )
            db.session.add(row)
        row.amount = amount
        row.category = category
        row.split_group = group
        row.needs_review = False
        if has_usd:
            if i == len(parts) - 1:
                row.usd_value = usd_left
            else:
                share = (usd_total * amount / total).quantize(CENT) if total else Decimal("0")
                row.usd_value = share
                usd_left -= share
    for extra in kept[len(parts):]:
        db.session.delete(extra)
    return lead


def unsplit(tx: Transaction) -> Transaction:
    """Joins a split purchase back into one transaction under the category
    of its largest part. Returns it. The caller commits."""
    rows = parts_of(tx)
    lead = rows[0]
    if len(rows) == 1:
        lead.split_group = None
        return lead
    largest = max(rows, key=lambda r: Decimal(r.amount))
    lead.category = largest.category
    lead.amount = total_of(rows)
    if all(r.usd_value is not None for r in rows):
        lead.usd_value = sum((Decimal(r.usd_value) for r in rows), Decimal("0"))
    lead.split_group = None
    for row in rows[1:]:
        db.session.delete(row)
    return lead


def share_edit(tx: Transaction, fields: dict) -> None:
    """A name, date or type change on a split purchase applies to every
    part: it's still one purchase."""
    for row in parts_of(tx):
        for name, value in fields.items():
            setattr(row, name, value)
