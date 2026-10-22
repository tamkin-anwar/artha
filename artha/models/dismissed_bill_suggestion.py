from datetime import datetime, timezone

from ..extensions import db


class DismissedBillSuggestion(db.Model):
    """
    A merchant someone told Artha is not a repeating bill ("Not a bill"),
    so services/recurring_detect.py stops suggesting it. Keyed the same
    way as MerchantCategoryRule (finance/routes.py's _merchant_key), so a
    store whose descriptions vary a little is still the same merchant.
    """

    __tablename__ = "dismissed_bill_suggestion"
    __table_args__ = (
        db.UniqueConstraint("user_id", "merchant_key", name="uq_dismissed_bill_suggestion_user_key"),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    merchant_key = db.Column(db.String(120), nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
