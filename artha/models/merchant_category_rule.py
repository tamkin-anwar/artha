from datetime import datetime, timezone

from ..extensions import db


class MerchantCategoryRule(db.Model):
    """
    A user's own "I categorized this merchant as X" memory, learned the
    moment they explicitly set or change a transaction's category (see
    _remember_merchant_category in finance/routes.py) — checked before
    the global _CATEGORY_KEYWORDS list in _guess_category(), so a user's
    own correction always wins over a generic keyword guess, the same
    "learns from your own activity" trait Copilot Money is known for.
    One row per (user, merchant_key); re-categorizing the same merchant
    overwrites rather than stacking a second rule.

    merchant_key is the transaction description normalized down to a
    stable "who was this" fragment (see finance/routes.py's
    _merchant_key()) rather than the raw description — bank-statement
    descriptions carry store numbers/transaction refs that make exact-
    string matching too narrow to ever hit twice.
    """

    __tablename__ = "merchant_category_rule"
    __table_args__ = (
        db.UniqueConstraint("user_id", "merchant_key", name="uq_merchant_category_rule_user_key"),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    merchant_key = db.Column(db.String(120), nullable=False)
    category = db.Column(db.String(32), nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    def __repr__(self) -> str:
        return f"<MerchantCategoryRule user={self.user_id} key={self.merchant_key!r} category={self.category}>"
