from datetime import datetime, timezone
from decimal import Decimal

from artha.extensions import db
from artha.models import Transaction

AJAX_HEADERS = {"X-Requested-With": "XMLHttpRequest"}


def _add_tx(user, description="Coffee", amount="4.50", ttype="expense", category=None):
    tx = Transaction(
        description=description,
        amount=Decimal(amount),
        type=ttype,
        user_id=user.id,
        timestamp=datetime.now(timezone.utc),
        category=category,
    )
    db.session.add(tx)
    db.session.commit()
    return tx


def test_add_transaction_with_category(auth_client, user):
    resp = auth_client.post(
        "/add_transaction",
        data={"description": "Whole Foods", "amount": "60.00", "type": "expense", "category": "groceries"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    tx = Transaction.query.filter_by(user_id=user.id).first()
    assert tx.category == "groceries"


def test_add_transaction_ignores_unknown_category(auth_client, user):
    resp = auth_client.post(
        "/add_transaction",
        data={"description": "Mystery", "amount": "10.00", "type": "expense", "category": "not-a-real-category"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    tx = Transaction.query.filter_by(user_id=user.id).first()
    assert tx.category is None


def test_add_transaction_with_no_category_stays_uncategorized(auth_client, user):
    auth_client.post(
        "/add_transaction",
        data={"description": "Coffee", "amount": "4.50", "type": "expense"},
        follow_redirects=True,
    )
    tx = Transaction.query.filter_by(user_id=user.id).first()
    assert tx.category is None


def test_update_transaction_sets_category(auth_client, user):
    tx = _add_tx(user)
    resp = auth_client.post(
        f"/update_transaction/{tx.id}",
        json={"description": tx.description, "amount": str(tx.amount), "type": tx.type, "category": "dining"},
    )
    assert resp.status_code == 200
    db.session.refresh(tx)
    assert tx.category == "dining"


def test_update_transaction_omitting_category_does_not_clear_it(auth_client, user):
    tx = _add_tx(user, category="dining")
    resp = auth_client.post(
        f"/update_transaction/{tx.id}",
        json={"description": "Renamed", "amount": str(tx.amount), "type": tx.type},
    )
    assert resp.status_code == 200
    db.session.refresh(tx)
    assert tx.category == "dining"
    assert tx.description == "Renamed"


def test_update_transaction_empty_category_clears_it(auth_client, user):
    tx = _add_tx(user, category="dining")
    resp = auth_client.post(
        f"/update_transaction/{tx.id}",
        json={"description": tx.description, "amount": str(tx.amount), "type": tx.type, "category": ""},
    )
    assert resp.status_code == 200
    db.session.refresh(tx)
    assert tx.category is None


def test_undo_delete_restores_category(auth_client, user):
    tx = _add_tx(user, category="housing")
    auth_client.post(f"/delete_transaction/{tx.id}", headers=AJAX_HEADERS)
    assert Transaction.query.count() == 0

    resp = auth_client.post("/undo_delete_transaction")
    assert resp.status_code == 200
    restored = Transaction.query.filter_by(user_id=user.id).first()
    assert restored is not None
    assert restored.category == "housing"
