import logging
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from flask import abort, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from ...extensions import db
from ...models.account import ACCOUNT_TYPES, Account, NetWorthSnapshot
from ...services.exchange_rate_service import convert_amount, convert_usd_to, get_rates
from ...utils import CURRENCY_CODES, is_ajax_request, user_today
from . import accounts_bp

log = logging.getLogger(__name__)


class ValidationError(Exception):
    pass


def _get_owned_account(account_id: int) -> Account:
    account = db.session.get(Account, account_id)
    if account is None or account.user_id != current_user.id:
        abort(404)
    return account


def _parse_balance(raw) -> Decimal:
    raw = (raw or "").strip() or "0"
    try:
        value = Decimal(raw)
    except InvalidOperation:
        raise ValidationError("Balance must be a valid number.")
    if value < 0:
        raise ValidationError("Balance must be zero or positive. A liability's balance is what's owed, not a negative number.")
    return value


def _apply_form(account: Account, form) -> None:
    name = (form.get("name") or "").strip()
    if not name:
        raise ValidationError("Account name is required.")

    account_type = form.get("account_type") or "checking"
    if account_type not in ACCOUNT_TYPES:
        raise ValidationError("Unknown account type.")

    currency = (form.get("currency") or "USD").strip().upper()
    if currency not in CURRENCY_CODES:
        raise ValidationError("Unsupported currency.")

    balance = _parse_balance(form.get("current_balance"))

    account.name = name
    account.account_type = account_type
    account.currency = currency
    account.current_balance = balance
    account.balance_updated_at = datetime.now(timezone.utc)


def _account_balances_usd(user_id: int, rates: dict | None = None):
    """(accounts, total_assets_usd, total_liabilities_usd) for one user —
    each account's own native currency converted live to USD via
    convert_amount(), the same "no locked-in rate" treatment
    Budget.monthly_cap/Scenario's cost fields get, since a balance is an
    ongoing setting that gets re-typed, not a point-in-time transaction."""
    accounts = (
        Account.query.filter_by(user_id=user_id)
        .order_by(Account.account_type.asc(), Account.created_at.asc())
        .all()
    )
    total_assets_usd = Decimal("0")
    total_liabilities_usd = Decimal("0")
    for a in accounts:
        usd = convert_amount(a.current_balance, a.currency or "USD", "USD", rates)
        if a.is_liability:
            total_liabilities_usd += usd
        else:
            total_assets_usd += usd
    return accounts, total_assets_usd, total_liabilities_usd


def _upsert_net_worth_snapshot(user) -> None:
    """One row per user per local day — re-saved in place if an account
    changes again the same day, rather than accumulating duplicates.
    Called after every add/edit/delete so the trend line starts
    accumulating real history from day one."""
    rates = get_rates()
    _, total_assets_usd, total_liabilities_usd = _account_balances_usd(user.id, rates)
    today = user_today(user)
    snapshot = NetWorthSnapshot.query.filter_by(user_id=user.id, snapshot_date=today).first()
    if snapshot is None:
        snapshot = NetWorthSnapshot(user_id=user.id, snapshot_date=today)
        db.session.add(snapshot)
    snapshot.total_assets_usd = total_assets_usd
    snapshot.total_liabilities_usd = total_liabilities_usd
    db.session.commit()


def net_worth_summary(user) -> dict | None:
    """Shared with the dashboard widget (dashboard/routes.py imports this)
    so both places compute net worth exactly the same way. None when the
    user has no accounts yet — there's nothing to summarize, same
    "only show when there's real data" gating Safe to Spend uses."""
    accounts, total_assets_usd, total_liabilities_usd = _account_balances_usd(user.id)
    if not accounts:
        return None

    display_currency = user.preferred_currency or "USD"
    rates = get_rates() if display_currency != "USD" else None
    total_assets = convert_usd_to(total_assets_usd, display_currency, rates)
    total_liabilities = convert_usd_to(total_liabilities_usd, display_currency, rates)

    return {
        "account_count": len(accounts),
        "total_assets": float(total_assets),
        "total_liabilities": float(total_liabilities),
        "net_worth": float(total_assets - total_liabilities),
    }


def _staleness_tier(balance_updated_at: datetime) -> str:
    """Visual staleness indicator, the cheap research-backed alternative to
    a full push-reminder system for keeping manual balances honest."""
    updated = balance_updated_at
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    days = (datetime.now(timezone.utc) - updated).days
    if days >= 90:
        return "stale"
    if days >= 30:
        return "aging"
    return "fresh"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@accounts_bp.route("/")
@login_required
def index():
    rates = get_rates()
    accounts, total_assets_usd, total_liabilities_usd = _account_balances_usd(current_user.id, rates)

    display_currency = current_user.preferred_currency or "USD"
    total_assets = float(convert_usd_to(total_assets_usd, display_currency, rates))
    total_liabilities = float(convert_usd_to(total_liabilities_usd, display_currency, rates))
    net_worth = total_assets - total_liabilities

    accounts_view = []
    for a in accounts:
        accounts_view.append({
            "account": a,
            "staleness": _staleness_tier(a.balance_updated_at),
        })
    asset_rows = [row for row in accounts_view if not row["account"].is_liability]
    liability_rows = [row for row in accounts_view if row["account"].is_liability]

    # Trend chart needs at least two points to draw a line that means
    # anything — a single dot (every brand-new user, day one) isn't a
    # trend, it's just today's number restated, so the template shows a
    # "come back after a few updates" note instead of an empty-looking chart.
    snapshots = (
        NetWorthSnapshot.query.filter_by(user_id=current_user.id)
        .order_by(NetWorthSnapshot.snapshot_date.asc())
        .all()
    )
    chart_points = [
        {
            "date": s.snapshot_date.strftime("%Y-%m-%d"),
            "net_worth": float(convert_usd_to(s.net_worth_usd, display_currency, rates)),
        }
        for s in snapshots
    ]

    return render_template(
        "accounts.html",
        asset_rows=asset_rows,
        liability_rows=liability_rows,
        total_assets=total_assets,
        total_liabilities=total_liabilities,
        net_worth=net_worth,
        chart_points=chart_points,
        account_types=ACCOUNT_TYPES,
        currency_codes=sorted(CURRENCY_CODES),
    )


@accounts_bp.route("/add", methods=["GET", "POST"])
@login_required
def add():
    if request.method == "GET":
        return render_template(
            "account_form.html",
            account=None,
            mode="add",
            account_types=ACCOUNT_TYPES,
            currency_codes=sorted(CURRENCY_CODES),
        )

    account = Account(user_id=current_user.id, currency=current_user.preferred_currency or "USD")
    try:
        _apply_form(account, request.form)
    except ValidationError as exc:
        flash(str(exc), "error")
        return redirect(url_for("accounts.add"))

    try:
        db.session.add(account)
        db.session.commit()
        _upsert_net_worth_snapshot(current_user)
        flash("Account added!", "success")
        return redirect(url_for("accounts.index"))
    except Exception as e:
        db.session.rollback()
        log.error("Error creating account: %s", e, exc_info=True)
        flash("Error creating account.", "error")
        return redirect(url_for("accounts.add"))


@accounts_bp.route("/<int:account_id>/edit", methods=["GET", "POST"])
@login_required
def edit(account_id):
    account = _get_owned_account(account_id)

    if request.method == "GET":
        return render_template(
            "account_form.html",
            account=account,
            mode="edit",
            account_types=ACCOUNT_TYPES,
            currency_codes=sorted(CURRENCY_CODES),
        )

    try:
        _apply_form(account, request.form)
    except ValidationError as exc:
        flash(str(exc), "error")
        return redirect(url_for("accounts.edit", account_id=account_id))

    try:
        db.session.commit()
        _upsert_net_worth_snapshot(current_user)
        flash("Account updated!", "success")
        return redirect(url_for("accounts.index"))
    except Exception as e:
        db.session.rollback()
        log.error("Error updating account: %s", e, exc_info=True)
        flash("Error updating account.", "error")
        return redirect(url_for("accounts.edit", account_id=account_id))


@accounts_bp.route("/<int:account_id>/delete", methods=["POST"])
@login_required
def delete(account_id):
    account = db.session.get(Account, account_id)
    if account is None or account.user_id != current_user.id:
        if is_ajax_request():
            return jsonify({"message": "Not found"}), 404
        flash("Account not found.", "error")
        return redirect(url_for("accounts.index"))

    try:
        db.session.delete(account)
        db.session.commit()
        _upsert_net_worth_snapshot(current_user)
        if is_ajax_request():
            return jsonify({"message": "Account deleted."})
        flash("Account deleted.", "success")
        return redirect(url_for("accounts.index"))
    except Exception as e:
        db.session.rollback()
        log.error("Error deleting account: %s", e, exc_info=True)
        if is_ajax_request():
            return jsonify({"message": "Error deleting account."}), 500
        flash("Error deleting account.", "error")
        return redirect(url_for("accounts.index"))
