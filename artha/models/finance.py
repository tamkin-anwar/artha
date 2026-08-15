from ..extensions import db


class Transaction(db.Model):
    __tablename__ = "transaction"

    id = db.Column(db.Integer, primary_key=True)
    description = db.Column(db.String(255), nullable=False)

    # FIX: was db.Float — floats cannot represent money precisely.
    # Numeric(12, 2) stores exact decimal values up to $9,999,999,999.99.
    amount = db.Column(db.Numeric(12, 2), nullable=False)

    type = db.Column(db.String(10), nullable=False)  # "income" | "expense"
    timestamp = db.Column(db.DateTime, default=db.func.current_timestamp())
    position = db.Column(db.Integer, nullable=False, default=0, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    is_recurring = db.Column(db.Boolean, nullable=False, default=False)

    # First-of-month date this row represents, set only on rows created by
    # generate_recurring() (NULL for everything else). A unique constraint
    # on (user_id, description, type, recurring_month) stops two
    # near-simultaneous /finance loads from double-generating the same
    # recurring bill for the same month — NULL isn't unique-constrained by
    # either Postgres or SQLite, so ordinary transactions are unaffected.
    recurring_month = db.Column(db.Date, nullable=True)

    __table_args__ = (
        db.UniqueConstraint(
            "user_id", "description", "type", "recurring_month",
            name="uq_transaction_recurring_month",
        ),
    )

    def __repr__(self) -> str:
        return f"<Transaction {self.id} {self.type} {self.amount}>"
