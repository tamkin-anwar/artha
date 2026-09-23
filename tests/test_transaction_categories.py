from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from artha.blueprints.finance.routes import _guess_category, _merchant_key
from artha.extensions import db
from artha.models import MerchantCategoryRule, Transaction
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


def test_categorize_uncategorized_keeps_keyword_hits_even_if_ai_batch_fails(auth_client, user):
    # "Coffee" resolves for free via the keyword list; "Some Merchant"
    # has no keyword match and needs the AI fallback. Committing the
    # keyword pass before any AI call runs (see categorize_uncategorized)
    # means an AI failure must never cost the free win alongside it --
    # that's exactly the bug reported live: a whole backlog stuck at the
    # same count click after click because one failing batch discarded
    # everything, keyword hits included.
    coffee = _add_tx(user, description="Coffee", category=None)
    needs_ai = _add_tx(user, description="Some Merchant", category=None)

    with patch("artha.services.ai_service._get_client") as mock_get_client:
        mock_get_client.return_value.messages.create.side_effect = RuntimeError("boom")
        resp = auth_client.post("/finance/categorize_uncategorized")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["categorized"] == 1
    assert data["remaining"] == 1

    db.session.refresh(coffee)
    db.session.refresh(needs_ai)
    assert coffee.category == "dining"
    assert needs_ai.category is None


def test_categorize_uncategorized_uses_ai_for_what_keywords_miss(auth_client, user):
    tx = _add_tx(user, description="Some Merchant", category=None)

    def route_by_tool(**kwargs):
        assert kwargs["tool_choice"]["name"] == "assign_categories"
        return SimpleNamespace(
            content=[SimpleNamespace(
                type="tool_use", name="assign_categories",
                input={"categories": ["shopping"]},
            )],
            usage=SimpleNamespace(input_tokens=20, output_tokens=5),
        )

    with patch("artha.services.ai_service._get_client") as mock_get_client:
        mock_get_client.return_value.messages.create.side_effect = route_by_tool
        resp = auth_client.post("/finance/categorize_uncategorized")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["categorized"] == 1
    assert data["remaining"] == 0

    db.session.refresh(tx)
    assert tx.category == "shopping"


def test_categorize_uncategorized_one_bad_ai_batch_does_not_lose_an_earlier_good_one(auth_client, user):
    # Force two AI batches (_BULK_CATEGORIZE_BATCH_SIZE=40): the first
    # call succeeds, the second raises. The first batch's results must
    # survive that -- each batch commits on its own, not one commit at
    # the very end that a later failure would roll back.
    first_batch = [_add_tx(user, description=f"Merchant A{i}", category=None) for i in range(40)]
    second_batch = [_add_tx(user, description=f"Merchant B{i}", category=None) for i in range(5)]

    call_count = {"n": 0}

    def route_by_tool(**kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return SimpleNamespace(
                content=[SimpleNamespace(
                    type="tool_use", name="assign_categories",
                    input={"categories": ["shopping"] * 40},
                )],
                usage=SimpleNamespace(input_tokens=100, output_tokens=40),
            )
        raise RuntimeError("second batch network blip")

    with patch("artha.services.ai_service._get_client") as mock_get_client:
        mock_get_client.return_value.messages.create.side_effect = route_by_tool
        resp = auth_client.post("/finance/categorize_uncategorized")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["categorized"] == 40
    assert data["remaining"] == 5

    for tx in first_batch:
        db.session.refresh(tx)
        assert tx.category == "shopping"
    for tx in second_batch:
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


# --- Per-merchant categorization memory ---


def test_merchant_key_normalizes_store_numbers_and_case():
    assert _merchant_key("STARBUCKS #4821 SEATTLE WA") == _merchant_key("Starbucks #9921 Seattle WA")


def test_merchant_key_empty_for_blank_description():
    assert _merchant_key("   ") == ""
    assert _merchant_key(None) == ""


def test_guess_category_prefers_remembered_rule_over_keyword():
    # "Starbucks" would normally keyword-match "dining" -- a remembered
    # rule for this exact merchant should win anyway.
    rules = {_merchant_key("Starbucks"): "shopping"}
    assert _guess_category("Starbucks", "expense", rules=rules) == "shopping"
    assert _guess_category("Starbucks", "expense") == "dining"


def test_add_transaction_with_explicit_category_remembers_merchant(auth_client, user):
    auth_client.post(
        "/add_transaction",
        data={"description": "Blue Bottle Coffee #12", "amount": "6.00", "type": "expense", "category": "shopping"},
        follow_redirects=True,
    )
    rule = MerchantCategoryRule.query.filter_by(user_id=user.id).first()
    assert rule is not None
    assert rule.category == "shopping"

    # A second, later transaction from the same merchant (different
    # store number, no explicit category) picks up the remembered
    # category instead of "dining" from the keyword list.
    auth_client.post(
        "/add_transaction",
        data={"description": "Blue Bottle Coffee #99", "amount": "5.50", "type": "expense"},
        follow_redirects=True,
    )
    txs = Transaction.query.filter_by(user_id=user.id).order_by(Transaction.id).all()
    assert txs[1].category == "shopping"


def test_add_transaction_without_explicit_category_does_not_remember(auth_client, user):
    # "Coffee" keyword-guesses to "dining" -- that's not a user
    # correction, so no rule should be created from it.
    auth_client.post(
        "/add_transaction",
        data={"description": "Coffee", "amount": "4.50", "type": "expense"},
        follow_redirects=True,
    )
    assert MerchantCategoryRule.query.filter_by(user_id=user.id).count() == 0


def test_update_transaction_category_change_remembers_merchant(auth_client, user):
    tx = _add_tx(user, description="Acme Corp Payroll Co", category=None)
    auth_client.post(
        f"/update_transaction/{tx.id}",
        json={"description": tx.description, "amount": str(tx.amount), "type": tx.type, "category": "subscriptions"},
    )
    rule = MerchantCategoryRule.query.filter_by(user_id=user.id).first()
    assert rule is not None
    assert rule.category == "subscriptions"

    # Correcting it again updates the same rule rather than stacking a
    # second one for the same merchant.
    auth_client.post(
        f"/update_transaction/{tx.id}",
        json={"description": tx.description, "amount": str(tx.amount), "type": tx.type, "category": "other"},
    )
    assert MerchantCategoryRule.query.filter_by(user_id=user.id).count() == 1
    db.session.refresh(rule)
    assert rule.category == "other"


def test_update_transaction_resaving_same_category_does_not_duplicate_rule(auth_client, user):
    tx = _add_tx(user, description="Acme Corp Payroll Co", category="subscriptions")
    # Autosave triggered by an unrelated field edit still sends the
    # row's current (unchanged) category -- must not create a rule from
    # a value that was never actually corrected in this request.
    auth_client.post(
        f"/update_transaction/{tx.id}",
        json={"description": "Acme Corp Payroll Co (renamed)", "amount": str(tx.amount), "type": tx.type, "category": "subscriptions"},
    )
    assert MerchantCategoryRule.query.filter_by(user_id=user.id).count() == 0


def test_categorize_uncategorized_uses_remembered_merchant_category(auth_client, user):
    db.session.add(MerchantCategoryRule(user_id=user.id, merchant_key=_merchant_key("Some Merchant"), category="entertainment"))
    db.session.commit()

    tx = _add_tx(user, description="Some Merchant", category=None)
    resp = auth_client.post("/finance/categorize_uncategorized")
    assert resp.status_code == 200
    assert resp.get_json()["categorized"] == 1

    db.session.refresh(tx)
    assert tx.category == "entertainment"


def test_merchant_category_rules_deleted_with_user(app, user):
    db.session.add(MerchantCategoryRule(user_id=user.id, merchant_key="acme", category="shopping"))
    db.session.commit()

    db.session.delete(user)
    db.session.commit()

    assert MerchantCategoryRule.query.count() == 0
