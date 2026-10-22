from datetime import datetime, timezone

from ..extensions import db


class Goal(db.Model):
    """
    A savings goal: what someone is putting money aside for, how much it
    takes, and optionally by when (services/goals.py turns that into "set
    aside $X a month"). saved_amount is what they've told Artha they've
    set aside so far, kept up to date by hand ("Add money"), not linked to
    transactions or accounts: the money usually sits in a savings account
    Artha doesn't connect to, and pretending otherwise would be wrong.

    Amounts are in the goal's own currency, captured from the person's
    display currency when it's created or edited (same rule as
    CategoryBudget.currency).
    """

    __tablename__ = "goal"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    name = db.Column(db.String(80), nullable=False)
    target_amount = db.Column(db.Numeric(12, 2), nullable=False)
    saved_amount = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    currency = db.Column(db.String(3), nullable=False, default="USD")
    target_date = db.Column(db.Date, nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    def __repr__(self) -> str:
        return f"<Goal user={self.user_id} name={self.name!r} {self.saved_amount}/{self.target_amount}>"
