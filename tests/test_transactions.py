from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from artha.extensions import db
from artha.models import Transaction, User

from .conftest import make_user

AJAX_HEADERS = {"X-Requested-With": "XMLHttpRequest"}


def test_add_transaction(auth_client, user):
    resp = auth_client.post(
        "/add_transaction",
        data={"description": "Coffee", "amount": "4.50", "type": "expense"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    tx = Transaction.query.filter_by(user_id=user.id).first()
    assert tx is not None
    assert tx.description == "Coffee"
    assert tx.amount == Decimal("4.50")
    assert tx.type == "expense"


def test_add_transaction_requires_description(auth_client):
    resp = auth_client.post(
        "/add_transaction",
        data={"description": "", "amount": "10", "type": "expense"},
        headers=AJAX_HEADERS,
    )
    assert resp.status_code == 400
    assert Transaction.query.count() == 0


def test_add_transaction_rejects_negative_amount(auth_client):
    resp = auth_client.post(
        "/add_transaction",
        data={"description": "Refund", "amount": "-5", "type": "expense"},
        headers=AJAX_HEADERS,
    )
    assert resp.status_code == 400
    assert Transaction.query.count() == 0


def test_add_transaction_rejects_invalid_type(auth_client):
    resp = auth_client.post(
        "/add_transaction",
        data={"description": "Mystery", "amount": "10", "type": "sideways"},
        headers=AJAX_HEADERS,
    )
    assert resp.status_code == 400
    assert Transaction.query.count() == 0


def test_add_transaction_requires_login(client):
    resp = client.post(
        "/add_transaction",
        data={"description": "Coffee", "amount": "4.50", "type": "expense"},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 401)
    assert Transaction.query.count() == 0


def test_update_transaction(auth_client, user):
    tx = Transaction(description="Old", amount=Decimal("10"), type="expense", user_id=user.id)
    db.session.add(tx)
    db.session.commit()

    resp = auth_client.post(
        f"/update_transaction/{tx.id}",
        json={"description": "New", "amount": "20.00", "type": "income"},
    )
    assert resp.status_code == 200
    refreshed = db.session.get(Transaction, tx.id)
    assert refreshed.description == "New"
    assert refreshed.amount == Decimal("20.00")
    assert refreshed.type == "income"


def test_update_transaction_blocks_other_users(auth_client, user):
    other = make_user(username="mallory", password="password123")
    tx = Transaction(description="Private", amount=Decimal("10"), type="expense", user_id=other.id)
    db.session.add(tx)
    db.session.commit()

    resp = auth_client.post(
        f"/update_transaction/{tx.id}",
        json={"description": "Hacked", "amount": "999", "type": "income"},
    )
    assert resp.status_code == 403
    refreshed = db.session.get(Transaction, tx.id)
    assert refreshed.description == "Private"


def test_delete_transaction(auth_client, user):
    tx = Transaction(description="Gone soon", amount=Decimal("10"), type="expense", user_id=user.id)
    db.session.add(tx)
    db.session.commit()
    tx_id = tx.id

    resp = auth_client.post(f"/delete_transaction/{tx_id}", headers=AJAX_HEADERS)
    assert resp.status_code == 200
    assert db.session.get(Transaction, tx_id) is None


def test_delete_transaction_blocks_other_users(auth_client, user):
    other = make_user(username="mallory2", password="password123")
    tx = Transaction(description="Private", amount=Decimal("10"), type="expense", user_id=other.id)
    db.session.add(tx)
    db.session.commit()

    resp = auth_client.post(f"/delete_transaction/{tx.id}", headers=AJAX_HEADERS)
    assert resp.status_code == 403
    assert db.session.get(Transaction, tx.id) is not None


def test_generate_recurring_creates_one_copy_per_template(auth_client, user):
    last_month = date.today().replace(day=1) - timedelta(days=1)
    tx = Transaction(
        description="Netflix",
        amount=Decimal("15.99"),
        type="expense",
        user_id=user.id,
        is_recurring=True,
        timestamp=datetime(last_month.year, last_month.month, min(last_month.day, 28), 12, 0, tzinfo=timezone.utc),
    )
    db.session.add(tx)
    db.session.commit()

    resp = auth_client.post("/finance/generate-recurring")
    assert resp.status_code == 200
    assert resp.get_json()["generated"] == 1

    this_month_start = date.today().replace(day=1)
    rows_this_month = Transaction.query.filter(
        Transaction.user_id == user.id,
        Transaction.description == "Netflix",
        Transaction.timestamp >= datetime(this_month_start.year, this_month_start.month, 1),
    ).all()
    assert len(rows_this_month) == 1


def test_generate_recurring_is_idempotent(auth_client, user):
    """Calling it twice in the same month must not create a duplicate —
    this is the exact regression class flagged as most likely to silently
    corrupt someone's real data if it ever broke again."""
    last_month = date.today().replace(day=1) - timedelta(days=1)
    tx = Transaction(
        description="Rent",
        amount=Decimal("1500"),
        type="expense",
        user_id=user.id,
        is_recurring=True,
        timestamp=datetime(last_month.year, last_month.month, min(last_month.day, 28), 12, 0, tzinfo=timezone.utc),
    )
    db.session.add(tx)
    db.session.commit()

    first = auth_client.post("/finance/generate-recurring").get_json()
    second = auth_client.post("/finance/generate-recurring").get_json()

    assert first["generated"] == 1
    assert second["generated"] == 0
    assert second["skipped"] == 1

    total_rent_rows = Transaction.query.filter_by(user_id=user.id, description="Rent").count()
    assert total_rent_rows == 2  # last month's template + this month's one generated copy


def test_deleting_recurring_transaction_stops_it_regenerating(auth_client, user):
    """Regression guard: deleting a recurring bill used to leave older
    rows still flagged is_recurring=True, so the very next /finance load
    silently brought it back."""
    last_month = date.today().replace(day=1) - timedelta(days=1)
    old_tx = Transaction(
        description="Gym",
        amount=Decimal("40"),
        type="expense",
        user_id=user.id,
        is_recurring=True,
        timestamp=datetime(last_month.year, last_month.month, min(last_month.day, 28), 12, 0, tzinfo=timezone.utc),
    )
    db.session.add(old_tx)
    db.session.commit()

    generated = auth_client.post("/finance/generate-recurring").get_json()
    assert generated["generated"] == 1

    this_month_row = Transaction.query.filter_by(
        user_id=user.id, description="Gym", is_recurring=True
    ).order_by(Transaction.timestamp.desc()).first()

    auth_client.post(f"/delete_transaction/{this_month_row.id}", headers=AJAX_HEADERS)

    still_recurring = Transaction.query.filter_by(
        user_id=user.id, description="Gym", is_recurring=True
    ).count()
    assert still_recurring == 0

    regenerated = auth_client.post("/finance/generate-recurring").get_json()
    assert regenerated["generated"] == 0
