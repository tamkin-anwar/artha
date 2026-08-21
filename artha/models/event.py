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

    # Recurrence: "daily" / "weekly" / "monthly", or None for a one-off
    # event. Only meaningful on an anchor row (recurrence_parent_id is
    # None) — it's what _generate_recurring_events() (dashboard/routes.py)
    # scans for on every /calendar load to lazily materialize each
    # occurrence as its own real Event row (same pattern as
    # generate_recurring() for Transaction, just keyed off the viewed date
    # window instead of "this month" since daily/weekly need finer-grained
    # generation than transactions ever do). A generated occurrence points
    # back at its anchor via recurrence_parent_id and is otherwise a fully
    # normal, independently editable/deletable event — no special-casing
    # needed anywhere else (drag, resize, delete, the day panel).
    recurrence = db.Column(db.String(20), nullable=True)
    recurrence_parent_id = db.Column(db.Integer, db.ForeignKey("event.id"), nullable=True, index=True)

    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    def __repr__(self) -> str:
        return f"<Event {self.id} {self.title!r} {self.start}-{self.end}>"
