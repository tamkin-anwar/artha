from datetime import datetime, timezone
from decimal import Decimal

from artha.extensions import db
from artha.models import Transaction
from tests.conftest import make_user

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


def test_add_transaction_with_no_category_auto_guesses_from_description(auth_client, user):
    # "Coffee" matches the dining keyword list (_CATEGORY_KEYWORDS) -- a
    # manual add with no explicit category is exactly the case the
    # keyword guesser exists for, same as an imported statement row.
    auth_client.post(
        "/add_transaction",
        data={"description": "Coffee", "amount": "4.50", "type": "expense"},
        follow_redirects=True,
    )
    tx = Transaction.query.filter_by(user_id=user.id).first()
    assert tx.category == "dining"


def test_add_transaction_with_unguessable_description_stays_uncategorized(auth_client, user):
    # No keyword match, and the AI fallback has no client configured in
    # tests (no ANTHROPIC_API_KEY) -- degrades to None exactly like an
    # import row the AI service couldn't reach, never a guess dressed up
    # as a real answer.
    auth_client.post(
        "/add_transaction",
        data={"description": "xyzzy plugh 42", "amount": "4.50", "type": "expense"},
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


def test_categorize_uncategorized_guesses_from_keywords(auth_client, user):
    coffee = _add_tx(user, description="Coffee", category=None)
    rent = _add_tx(user, description="Rent", category=None)
    already_set = _add_tx(user, description="Groceries run", category="groceries")

    resp = auth_client.post("/finance/categorize_uncategorized")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["categorized"] == 2

    db.session.refresh(coffee)
    db.session.refresh(rent)
    db.session.refresh(already_set)
    assert coffee.category == "dining"
    assert rent.category == "housing"
    # Never overwrites a category that was already set, whatever it is.
    assert already_set.category == "groceries"


def test_categorize_uncategorized_with_nothing_to_do(auth_client, user):
    _add_tx(user, description="Dinner", category="dining")

    resp = auth_client.post("/finance/categorize_uncategorized")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data == {"categorized": 0, "remaining": 0, "message": "Nothing to categorize."}


def test_categorize_uncategorized_leaves_unguessable_rows_alone(auth_client, user):
    # No keyword match, and no ANTHROPIC_API_KEY in tests -- the AI
    # fallback degrades to None exactly like it does for a single add.
    tx = _add_tx(user, description="xyzzy plugh 42", category=None)

    resp = auth_client.post("/finance/categorize_uncategorized")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["categorized"] == 0
    assert data["remaining"] == 1

    db.session.refresh(tx)
    assert tx.category is None


def test_categorize_uncategorized_only_touches_current_users_own_transactions(auth_client, user):
    other = make_user(username="bob")
    others_tx = Transaction(
        description="Coffee",
        amount=Decimal("4.50"),
        type="expense",
        user_id=other.id,
        timestamp=datetime.now(timezone.utc),
        category=None,
    )
    db.session.add(others_tx)
    db.session.commit()

    resp = auth_client.post("/finance/categorize_uncategorized")
    assert resp.status_code == 200
    assert resp.get_json()["categorized"] == 0

    db.session.refresh(others_tx)
    assert others_tx.category is None


def test_undo_delete_restores_category(auth_client, user):
    tx = _add_tx(user, category="housing")
    auth_client.post(f"/delete_transaction/{tx.id}", headers=AJAX_HEADERS)
    assert Transaction.query.count() == 0

    resp = auth_client.post("/undo_delete_transaction")
    assert resp.status_code == 200
    restored = Transaction.query.filter_by(user_id=user.id).first()
    assert restored is not None
    assert restored.category == "housing"
