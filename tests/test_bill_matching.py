"""
A tracked bill and the real charge for it count once (services/bill_matching.py).

Before this, generate_due_recurring() only recognized an occurrence by the
bill's exact description, so Apple Pay's "NETFLIX.COM", a statement's
"NETFLIX.COM 866-579-7172" or a typed "Rent via Zelle" all counted a second
time next to the generated "Netflix" or "Rent" row, overstating spending and
pulling Safe to Spend down.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from artha.extensions import db
from artha.models import Transaction
from artha.services import bill_matching


def _day_this_month() -> date:
    """A due day comfortably inside this month, so a charge a day either
    side stays in the same month whenever the suite runs."""
    today = date.today()
    return date(today.year, today.month, min(max(today.day, 3), 26))


def _last_month(day: date) -> date:
    idx = day.year * 12 + day.month - 2
    return date(idx // 12, idx % 12 + 1, day.day)


def _track(user, description, amount, due: date, interval=None, category="subscriptions", currency="USD"):
    """A bill tracked from last month's charge, then this month's
    occurrence generated the way the app does."""
    first = _last_month(due) if interval is None else due - timedelta(days=7 if interval == "weekly" else 14)
    db.session.add(Transaction(
        description=description, amount=Decimal(amount), type="expense", user_id=user.id,
        is_recurring=True, recurrence_interval=interval, category=category, currency=currency,
        timestamp=datetime(first.year, first.month, first.day, 12, tzinfo=timezone.utc),
    ))
    db.session.commit()


def _generate(auth_client):
    return auth_client.post("/finance/generate-recurring").get_json()


def _expenses_this_month(user):
    today = date.today()
    start = datetime(today.year, today.month, 1)
    return Transaction.query.filter(Transaction.user_id == user.id, Transaction.type == "expense",
                                    Transaction.timestamp >= start).all()


def _add(auth_client, description, amount, day: date, **extra):
    return auth_client.post("/add_transaction", data={
        "description": description, "amount": amount, "type": "expense",
        "date": day.isoformat(), "currency": "USD", **extra,
    }, headers={"X-Requested-With": "XMLHttpRequest"})


# --- names --------------------------------------------------------------------

def test_names_match_the_way_banks_write_them():
    assert bill_matching.names_match("Netflix", "NETFLIX.COM 866-579-7172")
    assert bill_matching.names_match("Planet Fitness", "PLANET FIT CLUB FEES")
    assert bill_matching.names_match("Blue Bottle", "BLUEBOTTLECOFFEE")
    assert not bill_matching.names_match("Netflix", "Whole Foods Market")
    # Generic words like "payment" never make a match on their own.
    assert not bill_matching.names_match("Card payment", "Payment received")


# --- the charge arrives after the bill's row exists ------------------------------

def test_an_apple_pay_charge_fills_in_the_bill_instead_of_counting_twice(auth_client, user):
    due = _day_this_month()
    _track(user, "Netflix", "15.49", due)
    _generate(auth_client)
    assert len(_expenses_this_month(user)) == 1

    resp = _add(auth_client, "NETFLIX.COM", "15.49", due + timedelta(days=1), source="apple_pay")

    assert resp.status_code == 200
    rows = _expenses_this_month(user)
    assert len(rows) == 1
    bill = rows[0]
    assert bill.description == "Netflix"            # the bill keeps its own name
    assert bill.category == "subscriptions"         # and category
    assert bill.timestamp.date() == due + timedelta(days=1)
    assert bill.bill_matched and bill.needs_review   # Apple Pay still waits for a look
    assert bill.import_source == "apple_pay"
    assert resp.get_json()["id"] == bill.id


def test_a_new_price_is_taken_and_carried_forward(auth_client, user):
    due = _day_this_month()
    _track(user, "Netflix", "15.49", due)
    _generate(auth_client)

    _add(auth_client, "NETFLIX.COM", "16.99", due)

    rows = _expenses_this_month(user)
    assert len(rows) == 1 and rows[0].amount == Decimal("16.99")


def test_a_bill_paid_under_another_name_matches_on_an_exact_amount_with_cents(auth_client, user):
    due = _day_this_month()
    _track(user, "Electric", "87.43", due, category="utilities")
    _generate(auth_client)

    _add(auth_client, "AUTOPAY UTILCO 4471", "87.43", due - timedelta(days=2))

    rows = _expenses_this_month(user)
    assert len(rows) == 1 and rows[0].description == "Electric" and rows[0].bill_matched


def test_a_round_amount_under_another_name_is_not_enough(auth_client, user):
    # From the first real cleanup run: a $60 Zelle payment to someone else
    # is not the $60.00 Verizon bill.
    due = _day_this_month()
    _track(user, "Verizon Bill", "60.00", due, category="utilities")
    _generate(auth_client)

    _add(auth_client, "Zelle payment to ATTERNA LLC", "60.00", due + timedelta(days=2))

    assert len(_expenses_this_month(user)) == 2


def test_an_unrelated_purchase_is_left_alone(auth_client, user):
    due = _day_this_month()
    _track(user, "Netflix", "15.49", due)
    _generate(auth_client)

    _add(auth_client, "Whole Foods", "84.12", due)

    assert len(_expenses_this_month(user)) == 2


def test_a_close_amount_without_a_matching_name_is_not_guessed(auth_client, user):
    due = _day_this_month()
    _track(user, "Netflix", "15.49", due)
    _generate(auth_client)

    _add(auth_client, "Spotify", "15.99", due)

    assert len(_expenses_this_month(user)) == 2


def test_two_bills_with_the_same_amount_are_not_guessed_between(auth_client, user):
    due = _day_this_month()
    _track(user, "Gym", "30.00", due)
    _track(user, "Phone", "30.00", due)
    _generate(auth_client)

    _add(auth_client, "Online payment", "30.00", due)

    assert len(_expenses_this_month(user)) == 3


def test_a_second_charge_does_not_land_on_an_already_paid_bill(auth_client, user):
    due = _day_this_month()
    _track(user, "Netflix", "15.49", due)
    _generate(auth_client)

    _add(auth_client, "NETFLIX.COM", "15.49", due)
    _add(auth_client, "NETFLIX.COM", "15.49", due)

    assert len(_expenses_this_month(user)) == 2


def test_a_charge_in_another_currency_does_not_match(auth_client, user):
    due = _day_this_month()
    _track(user, "Netflix", "15.49", due)
    _generate(auth_client)

    auth_client.post("/add_transaction", data={
        "description": "NETFLIX.COM", "amount": "15.49", "type": "expense",
        "date": due.isoformat(), "currency": "EUR",
    }, headers={"X-Requested-With": "XMLHttpRequest"})

    assert len(_expenses_this_month(user)) == 2


def test_a_statement_import_fills_in_the_bill(auth_client, user):
    due = _day_this_month()
    _track(user, "Netflix", "15.49", due)
    _generate(auth_client)

    resp = auth_client.post("/finance/import/commit", json={"rows": [
        {"date": due.isoformat(), "description": "NETFLIX.COM 866-579-7172", "amount": 15.49, "type": "expense"},
        {"date": due.isoformat(), "description": "Trader Joe's", "amount": 42.10, "type": "expense"},
    ]})

    assert resp.status_code == 200 and resp.get_json()["imported"] == 2
    rows = _expenses_this_month(user)
    assert sorted(r.description for r in rows) == ["Netflix", "Trader Joe's"]
    netflix = next(r for r in rows if r.description == "Netflix")
    assert netflix.import_source == "csv" and netflix.bill_matched


# --- the charge arrived before the bill's row would be generated ------------

def test_generation_takes_an_earlier_charge_as_the_bill(auth_client, user):
    due = _day_this_month()
    _track(user, "Netflix", "15.49", due)
    charge_day = due - timedelta(days=1)
    db.session.add(Transaction(
        description="NETFLIX.COM", amount=Decimal("15.49"), type="expense", user_id=user.id,
        currency="USD", category="entertainment",
        timestamp=datetime(charge_day.year, charge_day.month, charge_day.day, 12, tzinfo=timezone.utc),
    ))
    db.session.commit()

    data = _generate(auth_client)

    assert data["generated"] == 0
    rows = _expenses_this_month(user)
    assert len(rows) == 1
    bill = rows[0]
    assert bill.description == "Netflix" and bill.is_recurring and bill.bill_matched
    assert bill.category == "subscriptions"
    # And a second pass doesn't add the copy after all.
    assert _generate(auth_client)["generated"] == 0
    assert len(_expenses_this_month(user)) == 1


# --- weekly bills ----------------------------------------------------------

def test_a_weekly_bill_paid_a_day_late_still_counts_as_paid_once(auth_client, user, app):
    from artha.services.month_plan import bills_this_month
    from artha.services.month_snapshot import build_month_snapshot

    today = date.today()
    due = today - timedelta(days=1)
    if due.month != today.month or (due + timedelta(days=1)).month != today.month:
        return  # first or last day of a month: the window would cross it
    _track(user, "Yoga", "18.00", due, interval="weekly", category="health")
    _generate(auth_client)
    assert any(r.description == "Yoga" and r.recurring_month == due for r in _expenses_this_month(user))

    _add(auth_client, "YOGA STUDIO", "18.00", due + timedelta(days=1))

    yoga = [r for r in _expenses_this_month(user) if r.description == "Yoga" and r.timestamp.date() >= due]
    assert len(yoga) == 1 and yoga[0].occurrence_day == due
    with app.test_request_context():
        snap = build_month_snapshot(user, today)
        paid_due = [b for b in bills_this_month(snap).bills if b.description == "Yoga" and b.status == "due"
                    and b.due == due]
        assert paid_due == []


def test_a_bill_entered_by_hand_is_matched_by_the_statement_line(auth_client, user):
    # This month's bill typed in and marked as repeating, then the same
    # payment arrives on the statement under the bank's name.
    due = _day_this_month()
    db.session.add(Transaction(
        description="Spotify", amount=Decimal("11.99"), type="expense", user_id=user.id,
        is_recurring=True, category="subscriptions", currency="USD", import_source="manual",
        timestamp=datetime(due.year, due.month, due.day, 12, tzinfo=timezone.utc),
    ))
    db.session.commit()

    auth_client.post("/finance/import/commit", json={"rows": [
        {"date": due.isoformat(), "description": "SPOTIFY P3A2B1 STOCKHOLM", "amount": 11.99, "type": "expense"},
    ]})

    rows = _expenses_this_month(user)
    assert len(rows) == 1 and rows[0].description == "Spotify" and rows[0].import_source == "csv"


def test_a_bill_that_came_from_the_bank_is_already_the_real_charge(auth_client, user):
    # Tracked from an imported charge: a second, separate charge is a new
    # purchase, not this one again.
    due = _day_this_month()
    db.session.add(Transaction(
        description="Spotify", amount=Decimal("11.99"), type="expense", user_id=user.id,
        is_recurring=True, category="subscriptions", currency="USD", import_source="csv",
        timestamp=datetime(due.year, due.month, due.day, 12, tzinfo=timezone.utc),
    ))
    db.session.commit()

    _add(auth_client, "SPOTIFY P3A2B1 STOCKHOLM", "11.99", due)

    assert len(_expenses_this_month(user)) == 2


# --- one-time cleanup of duplicates from before matching existed -------------

def _row(user, description, amount, day: date, **extra):
    tx = Transaction(description=description, amount=Decimal(amount), type="expense", user_id=user.id,
                     currency="USD", timestamp=datetime(day.year, day.month, day.day, 12, tzinfo=timezone.utc),
                     **extra)
    db.session.add(tx)
    db.session.commit()
    return tx


def _past_month_day(months_back: int) -> date:
    today = date.today()
    idx = today.year * 12 + today.month - 1 - months_back
    return date(idx // 12, idx % 12 + 1, 15)


def test_cleanup_is_a_dry_run_unless_applied(app, user):
    due = _past_month_day(2)
    _row(user, "Netflix", "15.49", due, is_recurring=True, recurring_month=due.replace(day=1),
         category="subscriptions")
    _row(user, "NETFLIX.COM", "15.49", due + timedelta(days=1), import_source="csv")

    result = app.test_cli_runner().invoke(args=["merge-bill-duplicates"])

    assert "Dry run: 1 pair(s)" in result.output
    assert "NETFLIX.COM" not in result.output  # no one's transactions without --details
    assert Transaction.query.filter_by(user_id=user.id).count() == 2


def test_cleanup_merges_past_pairs_and_leaves_the_rest(app, user):
    old = _past_month_day(2)
    older = _past_month_day(3)
    # A generated Netflix copy and its statement line, two months ago.
    _row(user, "Netflix", "15.49", old, is_recurring=True, recurring_month=old.replace(day=1), category="subscriptions")
    _row(user, "NETFLIX.COM", "15.99", old + timedelta(days=2), import_source="csv")
    # Rent typed in as a repeating bill, and the Zelle payment for it.
    _row(user, "Rent", "1650.25", older, is_recurring=True, import_source="manual", category="housing")
    _row(user, "Zelle to J Smith", "1650.25", older - timedelta(days=1), import_source="csv")
    # A round amount to someone else is left alone (Verizon vs a $60 Zelle).
    _row(user, "Verizon Bill", "60.00", old, is_recurring=True, recurring_month=old.replace(day=1))
    _row(user, "Zelle payment to ATTERNA LLC", "60.00", old + timedelta(days=2), import_source="csv")
    # Two bills with the same amount and a payment that could be either.
    _row(user, "Gym", "30.00", older, is_recurring=True, recurring_month=older.replace(day=1))
    _row(user, "Phone", "30.00", older, is_recurring=True, recurring_month=older.replace(day=1))
    _row(user, "Online payment", "30.00", older, import_source="csv")
    # An ordinary purchase.
    _row(user, "Whole Foods", "84.12", old, import_source="csv")

    result = app.test_cli_runner().invoke(args=["merge-bill-duplicates", "--apply", "--details"])

    assert "Merged 2 pair(s)" in result.output, result.output
    names = sorted(t.description for t in Transaction.query.filter_by(user_id=user.id).all())
    assert names == ["Gym", "Netflix", "Online payment", "Phone", "Rent", "Verizon Bill", "Whole Foods",
                     "Zelle payment to ATTERNA LLC"]
    netflix = Transaction.query.filter_by(user_id=user.id, description="Netflix").one()
    assert netflix.amount == Decimal("15.99") and netflix.bill_matched and netflix.import_source == "csv"
    assert netflix.timestamp.date() == old + timedelta(days=2)
    rent = Transaction.query.filter_by(user_id=user.id, description="Rent").one()
    assert rent.bill_matched and rent.timestamp.date() == older - timedelta(days=1)

    # Running it again finds nothing more.
    again = app.test_cli_runner().invoke(args=["merge-bill-duplicates", "--apply"])
    assert "Merged 0 pair(s)" in again.output


def test_generation_does_not_hand_one_payment_to_either_of_two_equal_bills(auth_client, user):
    due = _day_this_month()
    _track(user, "Gym", "30.00", due)
    _track(user, "Phone", "30.00", due)
    _row(user, "Online payment", "30.00", due - timedelta(days=1), import_source="csv")

    _generate(auth_client)

    rows = _expenses_this_month(user)
    assert sorted(r.description for r in rows) == ["Gym", "Online payment", "Phone"]
    assert not any(r.bill_matched for r in rows)
