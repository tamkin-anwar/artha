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
    # Which provider fetched_at's data came from. A cached row's freshness
    # window alone can't detect a provider switch in the app's code — a
    # row fetched yesterday from the old provider still looks "fresh" by
    # the clock, so the service also checks this matches the provider it's
    # currently configured for before trusting the cache.
    source = db.Column(db.String(32), nullable=False, default="open-er-api")
    fetched_at = db.Column(
        db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    def __repr__(self) -> str:
        return f"<ExchangeRate base={self.base} fetched_at={self.fetched_at}>"
