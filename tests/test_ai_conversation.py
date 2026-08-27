from types import SimpleNamespace
from unittest.mock import patch

from artha.extensions import db
from artha.models import Conversation, Message


def _fake_response(text, input_tokens=10, output_tokens=5):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
    )


def test_get_conversation_is_empty_for_a_user_who_has_never_chatted(auth_client):
    resp = auth_client.get("/api/ai/conversation")
    assert resp.status_code == 200
    assert resp.get_json() == {"messages": []}


@patch("artha.services.ai_service._get_client")
def test_chat_persists_both_sides_of_the_turn(mock_get_client, auth_client, user):
    mock_get_client.return_value.messages.create.return_value = _fake_response("Hi there.")

    resp = auth_client.post("/api/ai/chat", json={"message": "Hello"})
    assert resp.status_code == 200

    conversation = Conversation.query.filter_by(user_id=user.id).one()
    messages = conversation.messages.order_by(Message.created_at.asc()).all()
    assert [(m.role, m.content) for m in messages] == [
        ("user", "Hello"),
        ("assistant", "Hi there."),
    ]

    # And the same turn comes back from the rehydration endpoint, in order.
    resp = auth_client.get("/api/ai/conversation")
    assert resp.get_json()["messages"] == [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi there."},
    ]


@patch("artha.services.ai_service._get_client")
def test_chat_sends_prior_turns_as_context_on_the_next_call(mock_get_client, auth_client):
    mock_get_client.return_value.messages.create.return_value = _fake_response("First reply.")
    auth_client.post("/api/ai/chat", json={"message": "First message"})

    mock_get_client.return_value.messages.create.return_value = _fake_response("Second reply.")
    auth_client.post("/api/ai/chat", json={"message": "Second message"})

    second_call_kwargs = mock_get_client.return_value.messages.create.call_args.kwargs
    assert second_call_kwargs["messages"] == [
        {"role": "user", "content": "First message"},
        {"role": "assistant", "content": "First reply."},
        {"role": "user", "content": "Second message"},
    ]


@patch("artha.services.ai_service._get_client")
def test_clearing_starts_a_fresh_conversation_without_deleting_the_old_one(mock_get_client, auth_client, user):
    mock_get_client.return_value.messages.create.return_value = _fake_response("Old reply.")
    auth_client.post("/api/ai/chat", json={"message": "Old message"})

    resp = auth_client.post("/api/ai/conversation/new")
    assert resp.status_code == 201

    # The new current conversation has nothing in it yet.
    resp = auth_client.get("/api/ai/conversation")
    assert resp.get_json() == {"messages": []}

    # But the old conversation and its messages are still in the DB.
    assert Conversation.query.filter_by(user_id=user.id).count() == 2
    old_messages = Message.query.join(Conversation).filter(Conversation.user_id == user.id).all()
    assert any(m.content == "Old message" for m in old_messages)


@patch("artha.services.ai_service.AIService.get_financial_insights")
def test_insights_are_saved_as_an_assistant_only_message(mock_insights, auth_client, user):
    mock_insights.return_value = {
        "insights": "You're doing fine.",
        "summary": {"total_income": 0, "total_expenses": 0, "net": 0, "transaction_count": 0},
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }

    resp = auth_client.post("/api/ai/insights")
    assert resp.status_code == 200

    conversation = Conversation.query.filter_by(user_id=user.id).one()
    messages = conversation.messages.all()
    assert len(messages) == 1
    assert messages[0].role == "assistant"
    assert messages[0].content == "You're doing fine."


def test_deleting_a_user_cascades_to_their_conversations_and_messages(app, user):
    conversation = Conversation(user_id=user.id)
    db.session.add(conversation)
    db.session.flush()
    db.session.add(Message(conversation_id=conversation.id, role="user", content="hi"))
    db.session.commit()

    conversation_id = conversation.id

    db.session.delete(user)
    db.session.commit()

    assert db.session.get(Conversation, conversation_id) is None
    assert Message.query.filter_by(conversation_id=conversation_id).count() == 0
