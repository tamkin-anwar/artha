from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from artha.models import Event, Note, Transaction
from artha.models.budget import Budget
from artha.models.category_budget import CategoryBudget


def _fake_response(blocks, input_tokens=10, output_tokens=5):
    return SimpleNamespace(
        content=blocks,
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
    )


def _text_block(text):
    return SimpleNamespace(type="text", text=text)


def _tool_use_block(name, input_):
    return SimpleNamespace(type="tool_use", name=name, input=input_)


@patch("artha.services.ai_service._get_client")
def test_chat_plain_reply_has_no_pending_actions(mock_get_client, auth_client):
    mock_get_client.return_value.messages.create.return_value = _fake_response(
        [_text_block("You spent $42 on dining last month.")]
    )

    resp = auth_client.post(
        "/api/ai/chat",
        json={"message": "How much did I spend on dining?", "history": []},
    )

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["reply"] == "You spent $42 on dining last month."
    assert data["pending_actions"] == []


@patch("artha.services.ai_service._get_client")
def test_chat_returns_pending_action_for_tool_use_without_writing_anything(mock_get_client, auth_client):
    mock_get_client.return_value.messages.create.return_value = _fake_response([
        _text_block("Here's that coffee expense to confirm."),
        _tool_use_block("add_transaction", {
            "description": "Coffee",
            "amount": 12.0,
            "type": "expense",
            "category": "dining",
        }),
    ])

    resp = auth_client.post(
        "/api/ai/chat",
        json={"message": "log a $12 coffee expense", "history": []},
    )

    assert resp.status_code == 200
    data = resp.get_json()
    assert len(data["pending_actions"]) == 1
    action = data["pending_actions"][0]
    assert action["type"] == "add_transaction"
    assert action["params"]["description"] == "Coffee"
    assert action["params"]["amount"] == 12.0

    # The proposal alone must never touch the database.
    assert Transaction.query.count() == 0


@patch("artha.services.ai_service._get_client")
def test_confirming_a_proposed_action_creates_the_transaction(mock_get_client, auth_client, user):
    mock_get_client.return_value.messages.create.return_value = _fake_response([
        _tool_use_block("add_transaction", {
            "description": "Coffee",
            "amount": 12.0,
            "type": "expense",
            "category": "dining",
        }),
    ])

    chat_resp = auth_client.post(
        "/api/ai/chat",
        json={"message": "log a $12 coffee expense", "history": []},
    )
    params = chat_resp.get_json()["pending_actions"][0]["params"]

    confirm_resp = auth_client.post(
        "/add_transaction",
        data=params,
        headers={"X-Requested-With": "XMLHttpRequest"},
    )

    assert confirm_resp.status_code == 200
    tx = Transaction.query.filter_by(user_id=user.id).first()
    assert tx is not None
    assert tx.description == "Coffee"
    assert float(tx.amount) == 12.0
    assert tx.type == "expense"
    assert tx.category == "dining"
    assert tx.import_source == "manual"


@patch("artha.services.ai_service._get_client")
def test_chat_returns_pending_note_action_without_writing_anything(mock_get_client, auth_client):
    mock_get_client.return_value.messages.create.return_value = _fake_response([
        _tool_use_block("create_note", {
            "title": "Grocery list",
            "content": "Eggs, milk, bread",
        }),
    ])

    resp = auth_client.post(
        "/api/ai/chat",
        json={"message": "jot down a grocery list: eggs, milk, bread", "history": []},
    )

    assert resp.status_code == 200
    action = resp.get_json()["pending_actions"][0]
    assert action["type"] == "create_note"
    assert action["params"]["title"] == "Grocery list"
    assert Note.query.count() == 0


@patch("artha.services.ai_service._get_client")
def test_confirming_a_proposed_note_creates_it(mock_get_client, auth_client, user):
    mock_get_client.return_value.messages.create.return_value = _fake_response([
        _tool_use_block("create_note", {
            "title": "Grocery list",
            "content": "Eggs, milk, bread",
            "color": "sage",
        }),
    ])

    chat_resp = auth_client.post(
        "/api/ai/chat",
        json={"message": "jot down a grocery list: eggs, milk, bread", "history": []},
    )
    params = chat_resp.get_json()["pending_actions"][0]["params"]

    # Mirrors what the frontend does on Confirm: create a blank note, then
    # save its fields onto it.
    create_resp = auth_client.post("/notes/new")
    note_id = create_resp.get_json()["id"]

    update_resp = auth_client.patch(
        f"/notes/{note_id}/update",
        json={"title": params["title"], "content": params["content"], "color": params["color"]},
    )

    assert update_resp.status_code == 200
    note = Note.query.filter_by(user_id=user.id).first()
    assert note is not None
    assert note.title == "Grocery list"
    assert note.content == "Eggs, milk, bread"
    assert note.color == "sage"


@patch("artha.services.ai_service._get_client")
def test_chat_returns_pending_event_action_without_writing_anything(mock_get_client, auth_client):
    mock_get_client.return_value.messages.create.return_value = _fake_response([
        _tool_use_block("create_event", {
            "title": "Dinner with Sarah",
            "start": "2026-08-28T19:00:00",
            "end": "2026-08-28T20:00:00",
        }),
    ])

    resp = auth_client.post(
        "/api/ai/chat",
        json={"message": "put dinner with Sarah on my calendar Friday at 7", "history": []},
    )

    assert resp.status_code == 200
    action = resp.get_json()["pending_actions"][0]
    assert action["type"] == "create_event"
    assert action["params"]["title"] == "Dinner with Sarah"
    assert Event.query.count() == 0


@patch("artha.services.ai_service._get_client")
def test_confirming_a_proposed_event_creates_it(mock_get_client, auth_client, user):
    mock_get_client.return_value.messages.create.return_value = _fake_response([
        _tool_use_block("create_event", {
            "title": "Dinner with Sarah",
            "start": "2026-08-28T19:00:00",
            "end": "2026-08-28T20:00:00",
            "recurrence": "weekly",
        }),
    ])

    chat_resp = auth_client.post(
        "/api/ai/chat",
        json={"message": "put dinner with Sarah on my calendar Friday at 7", "history": []},
    )
    params = chat_resp.get_json()["pending_actions"][0]["params"]

    confirm_resp = auth_client.post("/calendar/events", json=params)

    assert confirm_resp.status_code == 201
    event = Event.query.filter_by(user_id=user.id).first()
    assert event is not None
    assert event.title == "Dinner with Sarah"
    assert event.recurrence == "weekly"


@patch("artha.services.ai_service._get_client")
def test_chat_returns_pending_budget_action_without_writing_anything(mock_get_client, auth_client):
    mock_get_client.return_value.messages.create.return_value = _fake_response([
        _tool_use_block("set_budget", {"amount": 400.0, "category": "dining"}),
    ])

    resp = auth_client.post(
        "/api/ai/chat",
        json={"message": "set my dining budget to $400", "history": []},
    )

    assert resp.status_code == 200
    action = resp.get_json()["pending_actions"][0]
    assert action["type"] == "set_budget"
    assert action["params"]["amount"] == 400.0
    assert action["params"]["category"] == "dining"
    assert Budget.query.count() == 0
    assert CategoryBudget.query.count() == 0


@patch("artha.services.ai_service._get_client")
def test_confirming_a_proposed_category_budget_creates_it(mock_get_client, auth_client, user):
    mock_get_client.return_value.messages.create.return_value = _fake_response([
        _tool_use_block("set_budget", {"amount": 400.0, "category": "dining"}),
    ])

    chat_resp = auth_client.post(
        "/api/ai/chat",
        json={"message": "set my dining budget to $400", "history": []},
    )
    params = chat_resp.get_json()["pending_actions"][0]["params"]

    confirm_resp = auth_client.post(
        "/finance/category-budget",
        data={"monthly_cap": params["amount"], "category": params["category"]},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )

    assert confirm_resp.status_code == 200
    row = CategoryBudget.query.filter_by(user_id=user.id, category="dining").first()
    assert row is not None
    assert row.monthly_cap == Decimal("400.00")


@patch("artha.services.ai_service._get_client")
def test_confirming_a_proposed_overall_budget_creates_it(mock_get_client, auth_client, user):
    mock_get_client.return_value.messages.create.return_value = _fake_response([
        _tool_use_block("set_budget", {"amount": 2000.0}),
    ])

    chat_resp = auth_client.post(
        "/api/ai/chat",
        json={"message": "set my monthly budget to $2000", "history": []},
    )
    params = chat_resp.get_json()["pending_actions"][0]["params"]
    assert "category" not in params

    confirm_resp = auth_client.post(
        "/finance/budget",
        data={"monthly_cap": params["amount"]},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )

    assert confirm_resp.status_code == 200
    row = Budget.query.filter_by(user_id=user.id).first()
    assert row is not None
    assert row.monthly_cap == Decimal("2000.00")
