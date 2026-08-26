from types import SimpleNamespace
from unittest.mock import patch

from artha.models import Transaction


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
