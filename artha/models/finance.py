from decimal import Decimal

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

    # One of TRANSACTION_CATEGORIES (artha.blueprints.finance.routes) — a
    # small fixed set, deliberately not user-extensible (see that constant's
    # own docstring for why). Nullable so every transaction that already
    # existed before this column shipped stays valid without a backfill.
    category = db.Column(db.String(20), nullable=True)

    # "manual" | "csv" | None (legacy rows predating this column). "csv"
    # covers any uploaded-statement import regardless of the original file
    # type (CSV or PDF) — by the time a row reaches import_commit() it's
    # already normalized to the same shape, so the source document format
    # isn't worth a separate value. Not used for any behavior yet — just
    # provenance, so the UI can show "Imported" and a re-import of an
    # overlapping statement can be reasoned about.
    import_source = db.Column(db.String(10), nullable=True)

    # First-of-month date this row represents, set only on rows created by
    # generate_recurring() (NULL for everything else). A unique constraint
    # on (user_id, description, type, recurring_month) stops two
    # near-simultaneous /finance loads from double-generating the same
    # recurring bill for the same month — NULL isn't unique-constrained by
    # either Postgres or SQLite, so ordinary transactions are unaffected.
    recurring_month = db.Column(db.Date, nullable=True)

    # The currency this transaction actually happened in (one of
    # utils.CURRENCY_CODES). NULL on any row that predates this
    # column, which is read everywhere as "USD" — that was the only
    # currency that ever existed before display-currency switching did
    # anything real, so no backfill migration is needed.
    currency = db.Column(db.String(3), nullable=True)

    # This transaction's `amount`, converted to USD using the exchange
    # rate at the moment it was created/imported, then locked in
    # permanently — never recomputed later. This is what lets the display
    # layer convert a transaction into whatever currency the user is
    # currently browsing in without a months-old chart silently drifting
    # every time exchange rates move: the USD value of a past transaction
    # never changes, only the final pivot-to-display-currency hop uses a
    # live rate. NULL alongside `currency IS NULL` means "legacy row,
    # always was USD" — `usd_value` there is just `amount` itself.
    usd_value = db.Column(db.Numeric(14, 6), nullable=True)

    # The exact rate `usd_value` was computed with (units of `currency`
    # per 1 USD, exchange_rate_service's own rates[currency] at that
    # moment) — audit/debugging only, never read back into any
    # calculation. NULL whenever usd_value is NULL.
    usd_rate_locked = db.Column(db.Numeric(18, 8), nullable=True)

    __table_args__ = (
        db.UniqueConstraint(
            "user_id", "description", "type", "recurring_month",
            name="uq_transaction_recurring_month",
        ),
    )

    @property
    def native_currency(self) -> str:
        """This transaction's own currency, defaulting a legacy
        (pre-currency-column) row to USD — the only currency that ever
        existed before display-currency switching did anything real."""
        return self.currency or "USD"

    @property
    def value_in_usd(self) -> Decimal:
        """This transaction's value in USD, for aggregating across
        transactions that may be in different currencies. Falls back to
        `amount` itself when `usd_value` was never locked (a legacy row,
        or one saved while the exchange-rate API was unreachable) —
        correct specifically because both of those cases mean the
        transaction is already being treated as USD (see native_currency)."""
        return self.usd_value if self.usd_value is not None else self.amount

    def __repr__(self) -> str:
        return f"<Transaction {self.id} {self.type} {self.amount}>"
