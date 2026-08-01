"""
artha/services/exchange_rate_service.py
-----------------------------------------
Currency exchange rates for the Smart Calculator, sourced from Frankfurter
(frankfurter.dev — free, no API key, ECB-sourced, refreshed once per
business day).

Architecture decisions:
  - Cached in the database, not an in-memory dict or a file under instance/.
    Production runs multi-worker Gunicorn; a process-local cache would mean
    every worker independently re-fetches and disagrees, the exact bug
    already found and removed from finance_totals() (see
    artha/blueprints/finance/routes.py). The database is this app's one
    piece of state that's already correctly shared across workers.
  - Single row, always base=EUR (Frankfurter's own native base) — cross
    rates for any pair are computed from that one row, no need to store
    or fetch multiple bases.
  - 20-hour freshness window: safely under Frankfurter's ~24h publish
    cycle without hammering it on every request. No cron/background job;
    this app has none, so refresh-on-access keeps it that simple.
  - Falls back to a stale cached row rather than failing hard if
    Frankfurter is unreachable — still reasonably accurate for personal
    budgeting even a day or two old. Only returns None (unavailable) if
    there's no cache at all yet and the fetch also fails.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

import requests

from ..extensions import db
from ..models import ExchangeRate

log = logging.getLogger(__name__)

FRANKFURTER_URL = "https://api.frankfurter.dev/v1/latest"
FRESHNESS_WINDOW = timedelta(hours=20)
REQUEST_TIMEOUT = 5


def _aware_utc(dt: datetime) -> datetime:
    # SQLite always hands back a naive datetime on read, even though it was
    # written as timezone-aware (the tzinfo doesn't survive the round trip);
    # Postgres may or may not, depending on the column's exact type. The
    # value itself is always UTC either way since that's all this service
    # ever writes, so a naive read just needs the tzinfo re-attached rather
    # than reinterpreted.
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _row_to_dict(row: ExchangeRate) -> dict:
    return {
        "base": row.base,
        "rates": json.loads(row.rates_json),
        "fetched_at": _aware_utc(row.fetched_at).isoformat(),
    }


def get_rates() -> dict | None:
    """Return {"base": ..., "rates": {...}, "fetched_at": ...}, or None if
    no cached data exists and a fresh fetch also failed."""
    row = ExchangeRate.query.first()

    if row and (datetime.now(timezone.utc) - _aware_utc(row.fetched_at)) < FRESHNESS_WINDOW:
        return _row_to_dict(row)

    try:
        resp = requests.get(FRANKFURTER_URL, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()

        if row:
            row.base = data["base"]
            row.rates_json = json.dumps(data["rates"])
            row.fetched_at = datetime.now(timezone.utc)
        else:
            row = ExchangeRate(base=data["base"], rates_json=json.dumps(data["rates"]))
            db.session.add(row)

        db.session.commit()
        return _row_to_dict(row)

    except (requests.RequestException, KeyError, ValueError) as e:
        db.session.rollback()
        log.warning("Exchange rate fetch failed, serving stale cache if any: %s", e)
        return _row_to_dict(row) if row else None
