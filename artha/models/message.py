from datetime import datetime, timezone

from ..extensions import db


class Message(db.Model):
    """
    One turn in a Conversation. Content is plain text (the same
    {"role", "content"} shape the AI Assistant already sends Anthropic,
    now durable instead of living only in the browser's memory) — never
    the raw Anthropic API response, so nothing here is coupled to whatever
    shape a future model/SDK version happens to return.

    Deliberately does not store pending_actions (the tool_use proposals a
    reply might carry): resurfacing a stale Confirm/Cancel card safely
    after a reload would need to track whether it was already acted on,
    for a benefit that only matters in the narrow window between a
    proposal and clicking Confirm. Not worth the added surface for v1 —
    see artha/blueprints/ai/routes.py.
    """

    __tablename__ = "message"

    id = db.Column(db.Integer, primary_key=True)
    conversation_id = db.Column(db.Integer, db.ForeignKey("conversation.id"), nullable=False, index=True)
    role = db.Column(db.String(10), nullable=False)  # "user" | "assistant"
    content = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    def __repr__(self) -> str:
        return f"<Message {self.role} conversation={self.conversation_id}>"
