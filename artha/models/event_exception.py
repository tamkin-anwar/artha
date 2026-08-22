from datetime import datetime, timezone

from ..extensions import db


class EventException(db.Model):
    """
    Records that one specific occurrence of a recurring Event series was
    deliberately deleted. Without this, a deleted occurrence and one that
    simply hasn't been generated yet look identical to
    _generate_recurring_events() (dashboard/routes.py) — both are just "no
    Event row at this timestamp" — so it would regenerate the "deleted"
    occurrence the next time that date range is viewed. This table is the
    generator's memory of which slots to leave empty on purpose.
    """

    __tablename__ = "event_exception"

    id = db.Column(db.Integer, primary_key=True)
    anchor_id = db.Column(db.Integer, db.ForeignKey("event.id"), nullable=False, index=True)
    occurrence_start = db.Column(db.DateTime, nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        db.UniqueConstraint("anchor_id", "occurrence_start", name="uq_event_exception_anchor_start"),
    )

    def __repr__(self) -> str:
        return f"<EventException anchor={self.anchor_id} at={self.occurrence_start}>"
