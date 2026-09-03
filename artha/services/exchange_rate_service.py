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
from decimal import Decimal

import requests

from ..extensions import db
from ..models import ExchangeRate

log = logging.getLogger(__name__)

EXCHANGE_RATE_URL = "https://open.er-api.com/v6/latest/USD"
SOURCE = "open-er-api"
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

    is_fresh = (
        row
        and row.source == SOURCE
        and (datetime.now(timezone.utc) - _aware_utc(row.fetched_at)) < FRESHNESS_WINDOW
    )
    if is_fresh:
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
            row.source = SOURCE
            row.fetched_at = datetime.now(timezone.utc)
        else:
            row = ExchangeRate(base=data["base_code"], rates_json=json.dumps(data["rates"]), source=SOURCE)
            db.session.add(row)

        db.session.commit()
        return _row_to_dict(row)

    except (requests.RequestException, KeyError, ValueError) as e:
        db.session.rollback()
        log.warning("Exchange rate fetch failed, serving stale cache if any: %s", e)
        return _row_to_dict(row) if row else None


def lock_usd_value(amount: Decimal, currency: str) -> tuple[Decimal | None, Decimal | None]:
    """Returns (usd_value, rate_locked): `amount` converted to USD using
    the rate table as of right now, meant to be called once at a
    Transaction's creation/import time and the result stored permanently
    (see Transaction.usd_value's own docstring for why — this is the
    "locked" half of "lock at creation, convert live at display").

    currency == "USD" short-circuits to (amount, 1) with no API call —
    the common case for most users, and the only one that must never be
    blocked by a third-party rate provider being unreachable.

    Returns (None, None) if rates are unavailable or `currency` isn't in
    the table. Callers should still save the transaction in that case
    rather than blocking on it — a NULL usd_value reads back as "treat
    as already USD" everywhere it's used (Transaction.value_in_usd),
    the same fallback a genuinely pre-this-feature row gets."""
    if currency == "USD":
        return amount, Decimal("1")

    rates = get_rates()
    if rates is None or currency not in rates["rates"]:
        return None, None

    rate = Decimal(str(rates["rates"][currency]))
    if rate == 0:
        return None, None

    usd_value = (amount / rate).quantize(Decimal("0.01"))
    return usd_value, rate


def convert_usd_to(amount_usd: Decimal, target_currency: str, rates: dict | None = None) -> Decimal:
    """A USD figure converted to `target_currency` using a *live* rate —
    the other half of the "lock at creation, convert live at display/
    comparison" split (see lock_usd_value above): a Transaction's own
    usd_value never moves once set, but a SUM across many transactions
    (a month's total spending, a budget comparison) has no historical
    moment of its own to lock, so it's always converted fresh, right
    before it's shown or compared against a currency-less stored number
    like Budget.monthly_cap.

    `rates` lets a caller already holding a fetched table (e.g. looping
    over several conversions in one request) skip a redundant call;
    omitted, this fetches its own. Falls back to returning `amount_usd`
    unconverted if the target currency or a live rate isn't available —
    the same "degrade to the USD figure rather than block" precedent
    lock_usd_value already sets."""
    if target_currency == "USD":
        return amount_usd
    if rates is None:
        rates = get_rates()
    if not rates or target_currency not in rates.get("rates", {}):
        return amount_usd
    return amount_usd * Decimal(str(rates["rates"][target_currency]))
