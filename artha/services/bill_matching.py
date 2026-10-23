"""
artha/services/bill_matching.py
--------------------------------
Keeps a tracked bill and the real charge for it from counting twice.

A tracked bill (a recurring series, see finance.routes.generate_due_recurring)
gets a generated row for each occurrence, and a monthly bill's row is created
up front at the start of the month. The real charge then arrives separately:
Apple Pay logs "NETFLIX.COM", a statement says "NETFLIX.COM 866-579-7172",
or someone types it in. The old duplicate check only recognized a row with
exactly the bill's own description, so the bill and the charge both counted,
overstating spending and pulling Safe to Spend down.

Two directions, one rule set (modeled on YNAB, which pairs an entered or
scheduled transaction with an imported one when the amounts match and the
dates are within 10 days):

- Charge arrives after the bill's row exists: `find_open_occurrence` finds
  that unpaid generated row and `absorb_charge` writes the charge's amount,
  date and currency onto it, so there's one transaction, under the bill's
  own name and category. A new price carries forward, since the next
  occurrence copies the latest row.
- Charge arrived before the row would be generated: `find_charge_for` finds
  it and generation adopts it as that occurrence instead of adding a copy.

It only matches when it's sure: the same type and currency, the same
calendar month as the occurrence, a date within the window for that
cadence, and either a similar name with an amount within 10%, or the exact
amount when it has cents ($9.99 is nearly a fingerprint; a round $60.00
needs the name too). Two equally good candidates means no match. Leaving a real duplicate
for the person to delete is far better than silently merging two genuine
purchases.

Every pairing can be undone (`unpair`, YNAB's Unmatch): the row keeps what
the bill and the charge each were in Transaction.pairing, and splitting
them puts both back, the bill as planned and the charge under its own
name, which is then never paired with a bill on its own again.

When it misses a pair, the person can make it (`bills_for` and
`pair_by_hand`, like Copilot's "tap Recurring" or YNAB's manual Match):
pick the bill a charge paid from the open ones nearby, closest first.
"""

from __future__ import annotations

import calendar
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from ..extensions import db
from ..models import Transaction
from .exchange_rate_service import lock_usd_value

# Where a transaction came from when it's already the real charge.
BANK_SOURCES = ("csv", "apple_pay")

# How far from the due date a charge can land and still be that occurrence.
WINDOW_DAYS = {"monthly": 10, "weekly": 2, "biweekly": 4}

# With a matching name, the amount may differ this much (a price change,
# tax, a currency conversion on the card statement).
CLOSE_AMOUNT = Decimal("0.10")

# Words that say nothing about which bill a charge is.
_GENERIC_WORDS = {
    "payment", "bill", "card", "online", "purchase", "monthly", "subscription",
    "debit", "credit", "auto", "autopay", "recurring", "transfer", "the", "and",
}


def _words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) >= 3 and w not in _GENERIC_WORDS]


def names_match(bill_name: str, charge_description: str) -> bool:
    """Whether a charge's text plausibly names the bill: "Netflix" in
    "NETFLIX.COM 866-579-7172", "Planet Fitness" in "PLANET FIT CLUB FEES".
    Compares without spaces or punctuation, so "Blue Bottle" still finds
    "BLUEBOTTLECOFFEE"."""
    squashed_charge = re.sub(r"[^a-z0-9]", "", (charge_description or "").lower())
    squashed_bill = re.sub(r"[^a-z0-9]", "", (bill_name or "").lower())
    if len(squashed_bill) >= 4 and squashed_bill in squashed_charge:
        return True
    if len(squashed_charge) >= 4 and squashed_charge in squashed_bill:
        return True
    words = _words(bill_name)
    return any(len(w) >= 4 and w[:4] in squashed_charge for w in words)


def _occurrence_day(row: Transaction) -> date | None:
    """The due date a generated row stands for (a monthly copy's own date,
    a weekly copy's recurring_month)."""
    if row.recurrence_interval in ("weekly", "biweekly") and row.recurring_month:
        return row.recurring_month
    return row.timestamp.date() if row.timestamp else None


def _score(bill_name: str, bill_amount: Decimal, charge_description: str, charge_amount: Decimal,
           distance: int) -> tuple | None:
    """None when it isn't a match; otherwise a sort key, best first: a
    matching name before an amount-only match, then the closer date."""
    same_name = names_match(bill_name, charge_description)
    if charge_amount == bill_amount:
        if same_name:
            return (0, distance)
        # An exact amount alone is only trusted when it has cents ($9.99,
        # $65.33): close to a fingerprint. A round one ($60.00) is too common
        # to tell bills apart by, and needs the name to match as well (a
        # "Zelle payment to ..." for $60 isn't the $60 Verizon bill, as the
        # first real cleanup run showed).
        if charge_amount % 1 != 0:
            return (1, distance)
        return None
    if same_name and bill_amount > 0 and abs(charge_amount - bill_amount) <= bill_amount * CLOSE_AMOUNT:
        return (0, distance)
    return None


def _best(scored: list[tuple[tuple, Transaction]]) -> Transaction | None:
    """The single best candidate, or None when the top two tie (no way to
    know which bill this was, so it isn't guessed)."""
    if not scored:
        return None
    scored.sort(key=lambda item: item[0])
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        return None
    return scored[0][1]


def find_open_occurrence(user_id: int, t_type: str, currency: str, amount: Decimal,
                         description: str, day: date) -> Transaction | None:
    """The bill occurrence a new real charge pays, if exactly one fits:
    a row in a bill series (one generate_due_recurring() created, or the
    one someone entered and marked as repeating) that no charge has
    matched yet and that didn't come from a bank itself. A row imported
    from a statement or logged by Apple Pay already is the real charge."""
    widest = max(WINDOW_DAYS.values())
    start = datetime(day.year, day.month, day.day) - timedelta(days=widest + 1)
    end = datetime(day.year, day.month, day.day) + timedelta(days=widest + 2)
    candidates = Transaction.query.filter(
        Transaction.user_id == user_id,
        Transaction.type == t_type,
        db.or_(Transaction.recurring_month.isnot(None), Transaction.is_recurring.is_(True)),
        Transaction.bill_matched.is_(False),
        db.or_(Transaction.import_source.is_(None), Transaction.import_source.notin_(BANK_SOURCES)),
        Transaction.timestamp >= start,
        Transaction.timestamp < end,
    ).all()

    scored = []
    for row in candidates:
        if row.native_currency != currency:
            continue
        due = _occurrence_day(row)
        if due is None or (due.year, due.month) != (day.year, day.month):
            continue
        distance = abs((due - day).days)
        if distance > WINDOW_DAYS.get(row.recurrence_interval or "monthly", WINDOW_DAYS["monthly"]):
            continue
        score = _score(row.description, row.amount, description, amount, distance)
        if score is None and amount == _last_charged(row) and names_match(row.description, description):
            # The price the bill was last charged: a rise seen once before.
            score = (0, distance)
        if score is not None:
            scored.append((score, row))
    return _best(scored)


# ---------------------------------------------------------------------------
# What a bill should cost next time
# ---------------------------------------------------------------------------
#
# A charge that pays a bill writes its real amount onto the bill's row, and
# the next occurrence copies the latest row. Copied blindly, one odd month
# (a late fee, a prorated first month, a one-time surcharge) became what
# Artha expected every month after. So the next occurrence expects the new
# amount only once it's a real price: the person says so ("Expect $17.99
# from now on"), or two charges in a row came to it. Until then a one-off
# is paid as charged and the bill still expects its usual amount. A charge
# at the amount the bill was last charged still matches it, even past the
# usual 10%, so a genuine price rise pairs again on the second month
# instead of counting twice.

def planned_amount(row: Transaction) -> Decimal | None:
    """What a paid occurrence was expecting before its charge landed, or
    None when nothing was paid on it (or it was paired before this was
    kept)."""
    if not row.bill_matched or not isinstance(row.pairing, dict):
        return None
    return _decimal((row.pairing.get("bill") or {}).get("amount"))


def previous_occurrence(row: Transaction) -> Transaction | None:
    if row.timestamp is None:
        return None
    return Transaction.query.filter(
        Transaction.user_id == row.user_id,
        Transaction.description == row.description,
        Transaction.type == row.type,
        db.or_(Transaction.recurring_month.isnot(None), Transaction.is_recurring.is_(True)),
        Transaction.timestamp < row.timestamp,
        Transaction.id != row.id,
    ).order_by(Transaction.timestamp.desc()).first()


def expected_amount(template: Transaction) -> Decimal:
    """What the bill's next occurrence should expect, given its latest
    row: the amount it was charged when that's the usual amount, a price
    the person confirmed, or one two charges in a row agree on; otherwise
    its usual amount."""
    planned = planned_amount(template)
    if planned is None or planned == template.amount:
        return template.amount
    if template.pairing.get("new_price"):
        return template.amount
    previous = previous_occurrence(template)
    if previous is not None and previous.bill_matched and previous.amount == template.amount:
        return template.amount
    return planned


def expected_usd(template: Transaction) -> Decimal:
    amount = expected_amount(template)
    if amount == template.amount:
        return template.value_in_usd
    return lock_usd_value(amount, template.native_currency)[0]


def price_change(row: Transaction) -> dict | None:
    """For a paid bill charged something other than it expected: what it
    was charged, what it usually is, and what comes next, so the person can
    tell Artha it's the new price. None otherwise."""
    planned = planned_amount(row)
    if planned is None or planned == row.amount:
        return None
    following = Transaction.query.filter(
        Transaction.user_id == row.user_id,
        Transaction.description == row.description,
        Transaction.type == row.type,
        db.or_(Transaction.recurring_month.isnot(None), Transaction.is_recurring.is_(True)),
        Transaction.timestamp > row.timestamp,
    ).first() if row.timestamp else None
    return {
        "charged": _money(row.amount),
        "usual": _money(planned),
        "next": _money(expected_amount(row)),
        # Only the latest occurrence decides what's next.
        "can_set": following is None,
    }


class NotAPriceChange(ValueError):
    """There's no new price on this transaction to keep."""


def keep_new_price(row: Transaction) -> Transaction:
    """The person says this charge's amount is the bill's price from now
    on. The caller commits."""
    if planned_amount(row) is None or planned_amount(row) == row.amount:
        raise NotAPriceChange("This bill was charged what it usually is.")
    pairing = dict(row.pairing)
    pairing["new_price"] = True
    row.pairing = pairing
    return row


def _last_charged(row: Transaction) -> Decimal | None:
    """The amount this bill was most recently charged, before `row`."""
    previous = previous_occurrence(row)
    return previous.amount if previous is not None and previous.bill_matched else None


def _money(value) -> str | None:
    return None if value is None else str(value)


def _decimal(value) -> Decimal | None:
    return None if value is None else Decimal(value)


def _when(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def absorb_charge(occurrence: Transaction, *, description: str, category: str | None, amount: Decimal,
                  currency: str, usd_value, usd_rate_locked, timestamp, import_source: str | None,
                  needs_review: bool) -> Transaction:
    """Writes a real charge onto the bill occurrence it pays. The bill keeps
    its own name and category; the amount, currency and date become the
    real ones. A weekly copy's due date stays in recurring_month, so the
    occurrence still reads as posted even when the charge lands a day off.
    What the bill was, and the charge's own name and category, are kept in
    `pairing` so `unpair` can split them again."""
    occurrence.pairing = {
        "kind": "absorbed",
        "bill": {
            "amount": _money(occurrence.amount),
            "currency": occurrence.currency,
            "usd_value": _money(occurrence.usd_value),
            "usd_rate_locked": _money(occurrence.usd_rate_locked),
            "timestamp": _when(occurrence.timestamp),
            "import_source": occurrence.import_source,
            "needs_review": bool(occurrence.needs_review),
        },
        "charge": {"description": description, "category": category},
    }
    occurrence.amount = amount
    occurrence.currency = currency
    occurrence.usd_value = usd_value
    occurrence.usd_rate_locked = usd_rate_locked
    if timestamp is not None:
        occurrence.timestamp = timestamp
    occurrence.import_source = import_source
    occurrence.needs_review = needs_review
    occurrence.bill_matched = True
    return occurrence


def _series_templates(user_id: int, t_type: str) -> dict[str, Transaction]:
    """The latest row of each of this person's bill series, by name."""
    rows = Transaction.query.filter_by(user_id=user_id, type=t_type, is_recurring=True).all()
    latest: dict[str, Transaction] = {}
    for row in rows:
        current = latest.get(row.description)
        if current is None or (row.timestamp and current.timestamp
                               and row.timestamp.replace(tzinfo=None) > current.timestamp.replace(tzinfo=None)):
            latest[row.description] = row
    return latest


def _distance_to_series(template: Transaction, day: date) -> int:
    """Days between `day` and the nearest occurrence of `template`'s series
    in that month: a monthly bill's day of the month, or the nearest step
    of a weekly or biweekly one."""
    if template.recurrence_interval in ("weekly", "biweekly"):
        period = 7 if template.recurrence_interval == "weekly" else 14
        offset = (day - template.timestamp.date()).days % period
        return min(offset, period - offset)
    last = calendar.monthrange(day.year, day.month)[1]
    due = date(day.year, day.month, min(template.timestamp.day, last))
    return abs((day - due).days)


def _contested(charge: Transaction, chosen_bill: str, best: tuple) -> bool:
    """Whether another tracked bill could claim this charge just as well,
    in which case it isn't given to either (no guessing between two $30
    bills for one $30 payment)."""
    day = charge.timestamp.date()
    for name, template in _series_templates(charge.user_id, charge.type).items():
        if name == chosen_bill or template.native_currency != charge.native_currency or not template.timestamp:
            continue
        distance = _distance_to_series(template, day)
        if distance > WINDOW_DAYS.get(template.recurrence_interval or "monthly", WINDOW_DAYS["monthly"]):
            continue
        score = _score(template.description, template.amount, charge.description, charge.amount, distance)
        if score is not None and score[0] <= best[0]:
            return True
    return False


def find_charge_for(template: Transaction, due: date, *, allow_same_name: bool = False,
                    exclude_ids: set[int] | None = None) -> Transaction | None:
    """An existing real charge that already pays this occurrence of
    `template`'s series, if exactly one fits: one nobody tracked as a bill,
    not yet matched, same type and currency, near the due date in the
    same month. Used by generate_due_recurring() before it adds a copy
    (where a charge with the bill's own name is already handled by its
    own check) and by the one-time cleanup of past duplicates (where it
    isn't: allow_same_name)."""
    window = WINDOW_DAYS.get(template.recurrence_interval or "monthly", WINDOW_DAYS["monthly"])
    start = datetime(due.year, due.month, due.day) - timedelta(days=window + 1)
    end = datetime(due.year, due.month, due.day) + timedelta(days=window + 2)
    candidates = Transaction.query.filter(
        Transaction.user_id == template.user_id,
        Transaction.type == template.type,
        Transaction.is_recurring.is_(False),
        Transaction.recurring_month.is_(None),
        Transaction.bill_matched.is_(False),
        # Split off a bill by hand once: never paired on its own again.
        Transaction.bill_match_declined.is_(False),
        # A part of a split purchase isn't a whole charge.
        Transaction.split_group.is_(None),
        Transaction.timestamp >= start,
        Transaction.timestamp < end,
    ).all()

    scored = []
    for row in candidates:
        if not allow_same_name and row.description == template.description:
            continue
        if exclude_ids and row.id in exclude_ids:
            continue
        if row.native_currency != template.native_currency or row.timestamp is None:
            continue
        day = row.timestamp.date()
        if (day.year, day.month) != (due.year, due.month):
            continue
        distance = abs((day - due).days)
        if distance > window:
            continue
        score = _score(template.description, template.amount, row.description, row.amount, distance)
        if score is not None:
            scored.append((score, row))
    charge = _best(scored)
    if charge is None:
        return None
    best = min(item[0] for item in scored if item[1] is charge)
    return None if _contested(charge, template.description, best) else charge


def adopt_charge(charge: Transaction, template: Transaction, recurring_month: date, due: date) -> Transaction:
    """Makes an existing real charge this occurrence of the series: it takes
    the bill's name, category and cadence, keeps its own amount, date and
    currency. Then it's the series' latest row like any generated copy.
    The charge's own name and category, and the copy that would have been
    generated (due on `due`), are kept in `pairing` for `unpair`."""
    charge.pairing = {
        "kind": "adopted",
        "charge": {"description": charge.description, "category": charge.category},
        "bill": {
            "amount": _money(template.amount),
            "currency": template.native_currency,
            "category": template.category,
            "due": due.isoformat(),
            "recurring_month": recurring_month.isoformat(),
            "recurrence_interval": template.recurrence_interval,
            "recurring_end_date": template.recurring_end_date.isoformat() if template.recurring_end_date else None,
        },
    }
    charge.description = template.description
    if template.category:
        charge.category = template.category
    charge.is_recurring = True
    charge.recurring_month = recurring_month
    charge.recurrence_interval = template.recurrence_interval
    charge.recurring_end_date = template.recurring_end_date
    charge.bill_matched = True
    db.session.flush()
    return charge


# --- One-time cleanup of duplicates from before this existed -----------------

def past_duplicate_pairs(user_id: int) -> list[tuple[Transaction, Transaction]]:
    """Every (bill occurrence, real charge) pair in this person's history
    that counted twice before matching existed, by the same rules: each
    bill row that came from Artha or was typed in (not from a bank), with
    no charge matched yet, paired with the one real charge that fits it.
    A charge pairs with at most one bill; anything ambiguous is left out."""
    occurrences = Transaction.query.filter(
        Transaction.user_id == user_id,
        db.or_(Transaction.recurring_month.isnot(None), Transaction.is_recurring.is_(True)),
        Transaction.bill_matched.is_(False),
        db.or_(Transaction.import_source.is_(None), Transaction.import_source.notin_(BANK_SOURCES)),
        Transaction.timestamp.isnot(None),
    ).order_by(Transaction.timestamp).all()

    pairs = []
    used: set[int] = set()
    for occurrence in occurrences:
        due = _occurrence_day(occurrence)
        charge = find_charge_for(occurrence, due, allow_same_name=True, exclude_ids=used)
        if charge is not None:
            pairs.append((occurrence, charge))
            used.add(charge.id)
    return pairs


def merge_pair(occurrence: Transaction, charge: Transaction) -> None:
    """The live rule, after the fact: the charge's real amount, date and
    currency go onto the bill occurrence, and the charge row goes."""
    absorb_charge(occurrence, description=charge.description, category=charge.category,
                  amount=charge.amount, currency=charge.native_currency,
                  usd_value=charge.usd_value, usd_rate_locked=charge.usd_rate_locked,
                  timestamp=charge.timestamp, import_source=charge.import_source,
                  needs_review=charge.needs_review)
    db.session.delete(charge)


# --- Undoing a pairing ---------------------------------------------------------

class NotPaired(ValueError):
    """The row isn't a pairing Artha can split: never paired, or paired
    before the original values were kept."""


def _next_position(user_id: int) -> int:
    top = db.session.query(db.func.max(Transaction.position)).filter_by(user_id=user_id).scalar()
    return int(top or 0) + 1


def unpair(row: Transaction) -> tuple[Transaction, Transaction]:
    """Splits a paired row back into the bill occurrence and the real
    charge, the way each was before: the bill at its planned amount and
    date (open for a charge again), the charge under its own name and
    category with whatever amount, date and source the row has now. The
    charge is marked bill_match_declined so the same pairing doesn't come
    straight back. Returns (bill, charge); the caller commits."""
    snapshot = row.pairing
    if not row.bill_matched or not isinstance(snapshot, dict) or snapshot.get("kind") not in ("absorbed", "adopted"):
        raise NotPaired("This transaction isn't paired with a bill.")
    bill_was = snapshot.get("bill") or {}
    charge_was = snapshot.get("charge") or {}

    if snapshot["kind"] == "absorbed":
        # The row is the bill; the charge comes back as its own row.
        charge = Transaction(
            description=charge_was.get("description") or row.description,
            amount=row.amount,
            type=row.type,
            category=charge_was.get("category"),
            user_id=row.user_id,
            position=_next_position(row.user_id),
            timestamp=row.timestamp,
            import_source=row.import_source,
            needs_review=row.needs_review,
            currency=row.currency,
            usd_value=row.usd_value,
            usd_rate_locked=row.usd_rate_locked,
            bill_match_declined=True,
        )
        row.amount = _decimal(bill_was.get("amount")) or row.amount
        row.currency = bill_was.get("currency")
        row.usd_value = _decimal(bill_was.get("usd_value"))
        row.usd_rate_locked = _decimal(bill_was.get("usd_rate_locked"))
        if bill_was.get("timestamp"):
            row.timestamp = datetime.fromisoformat(bill_was["timestamp"])
        row.import_source = bill_was.get("import_source")
        row.needs_review = bool(bill_was.get("needs_review"))
        row.bill_matched = False
        row.pairing = None
        db.session.add(charge)
        db.session.flush()
        return row, charge

    # Adopted: the row is the charge; the copy it stood in for comes back.
    bill_name = row.description
    still_repeating = bool(row.is_recurring)
    row.description = charge_was.get("description") or row.description
    row.category = charge_was.get("category")
    row.is_recurring = False
    row.recurring_month = None
    row.recurrence_interval = None
    row.recurring_end_date = None
    row.bill_matched = False
    row.pairing = None
    row.bill_match_declined = True
    # The bill row below reuses this row's (name, recurring_month) slot, so
    # the change has to reach the database first.
    db.session.flush()

    due = date.fromisoformat(bill_was["due"])
    amount = _decimal(bill_was.get("amount"))
    currency = bill_was.get("currency") or "USD"
    usd_value, usd_rate_locked = lock_usd_value(amount, currency)
    ends = bill_was.get("recurring_end_date")
    bill = Transaction(
        description=bill_name,
        amount=amount,
        type=row.type,
        category=bill_was.get("category"),
        user_id=row.user_id,
        position=_next_position(row.user_id),
        is_recurring=still_repeating,
        timestamp=datetime(due.year, due.month, due.day, 12, 0, 0, tzinfo=timezone.utc),
        recurring_month=date.fromisoformat(bill_was["recurring_month"]),
        recurrence_interval=bill_was.get("recurrence_interval"),
        recurring_end_date=date.fromisoformat(ends) if ends else None,
        currency=currency,
        usd_value=usd_value,
        usd_rate_locked=usd_rate_locked,
    )
    db.session.add(bill)
    db.session.flush()
    return bill, row


# --- Pairing by hand ----------------------------------------------------------------

# How far a bill's due date can be from the charge to be offered at all.
# Wider than the automatic windows: a person choosing knows better than a
# date rule, so this only keeps the list short.
HAND_WINDOW_DAYS = 25
HAND_LIMIT = 8


class CannotPair(ValueError):
    """The charge or the chosen bill can't be paired by hand."""


def can_pair(tx: Transaction) -> bool:
    """A real charge: not a bill occurrence itself, not already paired,
    not part of a split purchase."""
    return (not tx.is_recurring and tx.recurring_month is None and not tx.bill_matched
            and not tx.split_group)


def bills_for(charge: Transaction) -> list[Transaction]:
    """The open bill occurrences this charge could have paid, likeliest
    first: a matching name, then the closest amount, then the closest
    date. Same type and currency, not paid yet, not from a bank."""
    if not can_pair(charge) or charge.timestamp is None:
        return []
    day = charge.timestamp.date()
    start = datetime(day.year, day.month, day.day) - timedelta(days=HAND_WINDOW_DAYS + 1)
    end = datetime(day.year, day.month, day.day) + timedelta(days=HAND_WINDOW_DAYS + 2)
    rows = Transaction.query.filter(
        Transaction.user_id == charge.user_id,
        Transaction.type == charge.type,
        Transaction.id != charge.id,
        db.or_(Transaction.recurring_month.isnot(None), Transaction.is_recurring.is_(True)),
        Transaction.bill_matched.is_(False),
        db.or_(Transaction.import_source.is_(None), Transaction.import_source.notin_(BANK_SOURCES)),
        Transaction.timestamp >= start,
        Transaction.timestamp < end,
    ).all()

    ranked = []
    for row in rows:
        due = _occurrence_day(row)
        if row.native_currency != charge.native_currency or due is None:
            continue
        distance = abs((due - day).days)
        if distance > HAND_WINDOW_DAYS:
            continue
        gap = abs(row.amount - charge.amount) / max(row.amount, Decimal("1"))
        ranked.append(((0 if names_match(row.description, charge.description) else 1, gap, distance), row))
    ranked.sort(key=lambda item: item[0])
    return [row for _, row in ranked[:HAND_LIMIT]]


def pair_by_hand(charge: Transaction, bill: Transaction) -> Transaction:
    """Pairs a charge with the bill the person picked, exactly like an
    automatic pairing: the charge's amount, date and source go onto the
    bill, which keeps its name and category, the charge row goes, and the
    originals are kept so Unpair can split them again. The caller commits."""
    if bill.id not in {row.id for row in bills_for(charge)}:
        raise CannotPair("That bill can't be paired with this transaction.")
    absorb_charge(bill, description=charge.description, category=charge.category,
                  amount=charge.amount, currency=charge.native_currency,
                  usd_value=charge.usd_value, usd_rate_locked=charge.usd_rate_locked,
                  timestamp=charge.timestamp, import_source=charge.import_source,
                  needs_review=charge.needs_review)
    db.session.delete(charge)
    db.session.flush()
    return bill
