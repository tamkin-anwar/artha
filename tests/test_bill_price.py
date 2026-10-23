"""A one-off charge doesn't reset a bill (bill_matching.expected_amount):
the next occurrence expects the usual amount until a new price is
confirmed, by the person or by two charges in a row."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from artha.extensions import db
from artha.models import Transaction
from artha.services import bill_matching

from .test_bill_matching import _add, _day_this_month, _expenses_this_month, _generate, _last_month


def _paid(user, description, charged, usual, day: date):
    """A bill occurrence a real charge paid: it holds the charge's amount
    and remembers what it was expecting."""
    row = Transaction(
        description=description, amount=Decimal(charged), type="expense", user_id=user.id,
        is_recurring=True, category="utilities", currency="USD", bill_matched=True,
        usd_value=Decimal(charged),
        timestamp=datetime(day.year, day.month, day.day, 12, tzinfo=timezone.utc),
        pairing={"kind": "absorbed", "bill": {"amount": usual}, "charge": {"description": description.upper(), "category": None}},
    )
    db.session.add(row)
    db.session.commit()
    return row


def _two_months_ago(day: date) -> date:
    return _last_month(_last_month(day))


def test_a_one_off_charge_doesnt_change_next_month(auth_client, user):
    due = _day_this_month()
    _paid(user, "Electric", "75.00", "60.00", _last_month(due))  # a late fee
    _generate(auth_client)
    [this_month] = _expenses_this_month(user)
    assert this_month.amount == Decimal("60.00")


def test_two_charges_in_a_row_are_the_new_price(auth_client, user):
    due = _day_this_month()
    _paid(user, "Electric", "75.00", "60.00", _two_months_ago(due))
    _paid(user, "Electric", "75.00", "60.00", _last_month(due))
    _generate(auth_client)
    [this_month] = _expenses_this_month(user)
    assert this_month.amount == Decimal("75.00")


def test_the_person_can_say_its_the_new_price(client, user, auth_client):
    due = _day_this_month()
    row = _paid(user, "Electric", "75.00", "60.00", _last_month(due))
    token = client.post("/api/v1/sessions", json={"username": user.username, "password": "password123"}).get_json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    before = client.get(f"/api/v1/transactions?limit=5", headers=headers).get_json()["transactions"][0]
    assert before["price_change"] == {"charged": "75.00", "usual": "60.00", "next": "60.00", "can_set": True}

    resp = client.post(f"/api/v1/transactions/{row.id}/new-price", headers=headers)
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["transaction"]["price_change"]["next"] == "75.00"

    _generate(auth_client)
    assert _expenses_this_month(user)[0].amount == Decimal("75.00")


def test_a_bill_charged_its_usual_amount_has_nothing_to_keep(auth_client, user):
    row = _paid(user, "Electric", "60.00", "60.00", _last_month(_day_this_month()))
    assert bill_matching.price_change(row) is None
    assert auth_client.post(f"/activity/{row.id}/new-price").status_code == 400


def test_a_price_rise_seen_once_still_pairs_the_next_month(auth_client, user):
    """$15.49 to $17.99 is more than the usual 10%: without remembering
    last month's charge, the second $17.99 counted twice."""
    due = _day_this_month()
    _paid(user, "Netflix", "17.99", "15.49", _last_month(due))
    _generate(auth_client)
    assert _expenses_this_month(user)[0].amount == Decimal("15.49")

    _add(auth_client, "NETFLIX.COM", "17.99", due)

    rows = _expenses_this_month(user)
    assert len(rows) == 1 and rows[0].amount == Decimal("17.99") and rows[0].bill_matched
    assert bill_matching.expected_amount(rows[0]) == Decimal("17.99")  # two in a row now
