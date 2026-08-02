from datetime import datetime, timezone

from ..extensions import db

VALID_CATEGORIES = {"bug", "idea", "other"}
VALID_STATUSES = {"new", "seen", "resolved"}


class Feedback(db.Model):
    """A bug report or suggestion submitted from the floating feedback
    button. Always tied to the logged-in user who sent it — there is no
    anonymous path, so status_count queries and the admin inbox never have
    to deal with unattributed spam."""

    __tablename__ = "feedback"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    category = db.Column(db.String(10), nullable=False, default="bug")
    message = db.Column(db.Text, nullable=False)
    # Captured automatically from the referring page, shown only to the
    # admin inbox — helps triage a bug report without asking the reporter
    # "which page was this on?".
    page_url = db.Column(db.String(255), nullable=True)
    status = db.Column(db.String(10), nullable=False, default="new", index=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    author = db.relationship("User", backref=db.backref("feedback_items", lazy="dynamic"))

    def __repr__(self) -> str:
        return f"<Feedback {self.id} {self.category} {self.status}>"
