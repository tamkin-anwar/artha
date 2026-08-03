from datetime import datetime, timezone

from ..extensions import db


class PushSubscription(db.Model):
    """
    One browser/device's Web Push subscription. A user can have several
    (phone + laptop, etc.) — endpoint is the natural unique key the
    browser assigns per subscription, not per user.

    last_notified_date dedupes the daily renewal-reminder job: it only
    sends once per subscription per day even if the job runs more than
    once (a manual re-run, a retried cron trigger, ...).
    """

    __tablename__ = "push_subscription"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    endpoint = db.Column(db.String(500), nullable=False, unique=True)
    p256dh = db.Column(db.String(255), nullable=False)
    auth = db.Column(db.String(255), nullable=False)
    last_notified_date = db.Column(db.Date, nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    def __repr__(self) -> str:
        return f"<PushSubscription user={self.user_id} endpoint={self.endpoint[:40]}...>"
