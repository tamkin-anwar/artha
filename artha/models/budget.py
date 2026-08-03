from datetime import datetime, timezone
from decimal import Decimal

from ..extensions import db


class Budget(db.Model):
    """
    A single overall monthly spending cap per user — not per-category,
    since Transaction has no category field and adding one is a separate,
    much bigger initiative (touching every add/edit transaction form).
    One row per user (unique user_id): setting a new cap overwrites the
    old one rather than versioning by month, matching "my monthly budget
    is $X" as an ongoing setting, not a per-month one-off.
    """

    __tablename__ = "budget"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, unique=True)
    monthly_cap = db.Column(db.Numeric(12, 2), nullable=False, default=Decimal("0"))
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    def __repr__(self) -> str:
        return f"<Budget user={self.user_id} cap={self.monthly_cap}>"
