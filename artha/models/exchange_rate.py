from datetime import datetime, timezone

from ..extensions import db


class ExchangeRate(db.Model):
    """
    Single-row cache of the latest currency rates, shared across all
    Gunicorn workers via the database rather than an in-memory dict —
    see artha/blueprints/finance/routes.py's finance_totals() for why a
    process-local cache was already tried and removed for this exact
    multi-worker reason.
    """

    __tablename__ = "exchange_rate"

    id = db.Column(db.Integer, primary_key=True)
    base = db.Column(db.String(3), nullable=False, default="USD")
    rates_json = db.Column(db.Text, nullable=False)
    fetched_at = db.Column(
        db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    def __repr__(self) -> str:
        return f"<ExchangeRate base={self.base} fetched_at={self.fetched_at}>"
