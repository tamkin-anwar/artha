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
cadence, and either the exact amount or a similar name with an amount within
10%. Two equally good candidates means no match. Leaving a real duplicate
for the person to delete is far better than silently merging two genuine
purchases.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from decimal import Decimal

from ..extensions import db
from ..models import Transaction

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
        return (0 if same_name else 1, distance)
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
        if score is not None:
            scored.append((score, row))
    return _best(scored)


def absorb_charge(occurrence: Transaction, *, amount: Decimal, currency: str, usd_value, usd_rate_locked,
                  timestamp, import_source: str | None, needs_review: bool) -> Transaction:
    """Writes a real charge onto the bill occurrence it pays. The bill keeps
    its own name and category; the amount, currency and date become the
    real ones. A weekly copy's due date stays in recurring_month, so the
    occurrence still reads as posted even when the charge lands a day off."""
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


def find_charge_for(template: Transaction, due: date) -> Transaction | None:
    """An existing real charge that already pays this occurrence of
    `template`'s series, if exactly one fits: one nobody tracked as a bill,
    not yet matched, same type and currency, near the due date in the
    same month. Used by generate_due_recurring() before it adds a copy."""
    window = WINDOW_DAYS.get(template.recurrence_interval or "monthly", WINDOW_DAYS["monthly"])
    start = datetime(due.year, due.month, due.day) - timedelta(days=window + 1)
    end = datetime(due.year, due.month, due.day) + timedelta(days=window + 2)
    candidates = Transaction.query.filter(
        Transaction.user_id == template.user_id,
        Transaction.type == template.type,
        Transaction.is_recurring.is_(False),
        Transaction.recurring_month.is_(None),
        Transaction.bill_matched.is_(False),
        Transaction.description != template.description,
        Transaction.timestamp >= start,
        Transaction.timestamp < end,
    ).all()

    scored = []
    for row in candidates:
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
    return _best(scored)


def adopt_charge(charge: Transaction, template: Transaction, recurring_month: date) -> Transaction:
    """Makes an existing real charge this occurrence of the series: it takes
    the bill's name, category and cadence, keeps its own amount, date and
    currency. Then it's the series' latest row like any generated copy."""
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
