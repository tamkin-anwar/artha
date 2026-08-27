from datetime import datetime, timezone

from ..extensions import db


class Conversation(db.Model):
    """
    One AI Assistant conversation thread. "The current conversation" for a
    user is just the most recently created row here — no separate
    is_active flag needed: Clear (artha/blueprints/ai/routes.py) starts a
    fresh one instead of flipping a status, which makes "what's current"
    always a plain "most recent" query rather than something that could
    drift out of sync with a flag. Older conversations are never deleted;
    there's just no UI to browse them yet (see the AI conversation memory
    plan for why that's a deliberate v1 cut, not a gap).
    """

    __tablename__ = "conversation"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    # Ordered by id, not created_at: a user/assistant pair saved from the
    # same request can land on the same timestamp at typical datetime
    # resolution, and created_at ties don't reliably preserve insertion
    # order. The auto-incrementing primary key can't tie.
    messages = db.relationship(
        "Message",
        backref="conversation",
        lazy="dynamic",
        cascade="all, delete-orphan",
        order_by="Message.id",
    )

    def __repr__(self) -> str:
        return f"<Conversation {self.id} user={self.user_id}>"
