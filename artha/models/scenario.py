from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from ..extensions import db

VALID_STATUSES = ("draft", "active", "completed", "archived")
VALID_PRIORITIES = ("low", "medium", "high")


class Scenario(db.Model):
    """
    A "what if" financial decision the user is weighing (e.g. "Move to a
    new apartment", "Switch to a 4-day work week"). Costs/savings are
    modeled explicitly so the impact can be computed instead of guessed.
    """

    __tablename__ = "scenario"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)

    title = db.Column(db.String(150), nullable=False)
    category = db.Column(db.String(50), nullable=False, default="other")
    description = db.Column(db.Text, nullable=True)

    one_time_cost = db.Column(db.Numeric(12, 2), nullable=False, default=Decimal("0"))
    monthly_cost = db.Column(db.Numeric(12, 2), nullable=False, default=Decimal("0"))
    monthly_savings = db.Column(db.Numeric(12, 2), nullable=False, default=Decimal("0"))

    start_date = db.Column(db.Date, nullable=True)
    end_date = db.Column(db.Date, nullable=True)

    priority = db.Column(db.String(10), nullable=False, default="medium")
    emotional_value = db.Column(db.Integer, nullable=False, default=5)  # 1-10, how much it matters to the user
    financial_risk = db.Column(db.Integer, nullable=False, default=5)   # 1-10, how risky it is financially

    notes = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(10), nullable=False, default="active", index=True)

    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    # ------------------------------------------------------------------
    # Pure arithmetic — no DB access, safe to use anywhere the row is loaded
    # ------------------------------------------------------------------

    @property
    def net_monthly_impact(self) -> Decimal:
        """Positive means the scenario nets more savings than cost per month."""
        return self.monthly_savings - self.monthly_cost

    @property
    def net_yearly_impact(self) -> Decimal:
        """Full first-year impact: 12 months of net cash flow minus the upfront cost."""
        return (self.net_monthly_impact * 12) - self.one_time_cost

    @property
    def payback_months(self) -> Decimal | None:
        """
        Months of net savings needed to cover the one-time cost.
        None if there's a one-time cost but no positive monthly impact to ever recover it.
        """
        if self.one_time_cost <= 0:
            return Decimal("0")
        if self.net_monthly_impact <= 0:
            return None
        return (self.one_time_cost / self.net_monthly_impact).quantize(Decimal("0.1"))

    def __repr__(self) -> str:
        return f"<Scenario {self.id} {self.title!r} status={self.status}>"
