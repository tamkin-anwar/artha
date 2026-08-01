"""
artha/services/exchange_rate_service.py
-----------------------------------------
Currency exchange rates for the Smart Calculator, sourced from
open.er-api.com (exchangerate-api.com's free, no-API-key, open endpoint —
refreshed once per day). Switched from Frankfurter, which is ECB-sourced
and doesn't carry BDT at all, one of the two currencies this was built
for.

Architecture decisions:
  - Cached in the database, not an in-memory dict or a file under instance/.
    Production runs multi-worker Gunicorn; a process-local cache would mean
    every worker independently re-fetches and disagrees, the exact bug
    already found and removed from finance_totals() (see
    artha/blueprints/finance/routes.py). The database is this app's one
    piece of state that's already correctly shared across workers.
  - Single row, always base=USD (this provider's rates are keyed off
    whatever base is in the URL path) — cross rates for any pair are
    computed from that one row, no need to store or fetch multiple bases.
  - 20-hour freshness window: safely under this provider's ~24h publish
    cycle without hammering it on every request. No cron/background job;
    this app has none, so refresh-on-access keeps it that simple.
  - Falls back to a stale cached row rather than failing hard if the
    provider is unreachable — still reasonably accurate for personal
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

EXCHANGE_RATE_URL = "https://open.er-api.com/v6/latest/USD"
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
        resp = requests.get(EXCHANGE_RATE_URL, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        if data.get("result") != "success":
            raise ValueError(f"unexpected response: {data.get('result')!r}")

        if row:
            row.base = data["base_code"]
            row.rates_json = json.dumps(data["rates"])
            row.fetched_at = datetime.now(timezone.utc)
        else:
            row = ExchangeRate(base=data["base_code"], rates_json=json.dumps(data["rates"]))
            db.session.add(row)

        db.session.commit()
        return _row_to_dict(row)

    except (requests.RequestException, KeyError, ValueError) as e:
        db.session.rollback()
        log.warning("Exchange rate fetch failed, serving stale cache if any: %s", e)
        return _row_to_dict(row) if row else None
