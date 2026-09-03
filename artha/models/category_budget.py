from datetime import datetime, timezone

from ..extensions import db


class CategoryBudget(db.Model):
    """
    A monthly spending cap for one category (e.g. "$400/mo on Dining").
    Separate from Budget (the single overall cap) rather than a nullable
    category column on it — keeps "the one overall cap" and "however many
    category caps" as two clear concepts instead of one table with a
    discriminator. A user can have either, both, or neither.
    """

    __tablename__ = "category_budget"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    # One of TRANSACTION_CATEGORIES (artha.blueprints.finance.routes),
    # never "income" — budgets are an expense concept.
    category = db.Column(db.String(20), nullable=False)
    monthly_cap = db.Column(db.Numeric(12, 2), nullable=False)
    # Same currency-capture as Budget.currency -- see that column's own
    # comment for why this exists and what breaks without it.
    currency = db.Column(db.String(3), nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (db.UniqueConstraint("user_id", "category", name="uq_category_budget_user_category"),)

    def __repr__(self) -> str:
        return f"<CategoryBudget user={self.user_id} category={self.category} cap={self.monthly_cap}>"
