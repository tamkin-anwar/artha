from datetime import datetime, timezone

from ..extensions import db


class TransferAccount(db.Model):
    """
    One of the person's own accounts, as their bank names it on a statement
    line ("SAV 7837", "CHK 1234", "A/C 5521"). Learned when they leave a
    line to or from it out of an import (services/statement_reader.py's
    account_reference): from then on, a statement line naming it starts
    unticked as a transfer between their own accounts, which isn't
    spending or income. Settings lists these, each with Forget.

    key is the reference normalized ("sav 7837"); label is how it reads.
    """

    __tablename__ = "transfer_account"
    __table_args__ = (
        db.UniqueConstraint("user_id", "key", name="uq_transfer_account_user_key"),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    key = db.Column(db.String(80), nullable=False)
    label = db.Column(db.String(80), nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    def __repr__(self) -> str:
        return f"<TransferAccount {self.label!r}>"
