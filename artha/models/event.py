from datetime import datetime, timezone

from ..extensions import db


class Event(db.Model):
    """
    A time-blocked calendar entry (e.g. a 10am-12pm meeting). Distinct from
    Transaction (financial, date-only in practice) and Note.due_date (also
    date-only) — this is the only model in the app with a real time range.
    """

    __tablename__ = "event"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    title = db.Column(db.String(150), nullable=False)
    start = db.Column(db.DateTime, nullable=False, index=True)
    end = db.Column(db.DateTime, nullable=False)
    # One of NOTE_COLORS (artha.blueprints.notes.routes) — same closed
    # palette as Notes, reused rather than inventing a second color system.
    color = db.Column(db.String(20), nullable=False, default="sky")
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    def __repr__(self) -> str:
        return f"<Event {self.id} {self.title!r} {self.start}-{self.end}>"
