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
    raw = str(raw if raw is not None else "").strip() or "0"
    try:
        value = Decimal(raw)
    except InvalidOperation:
        raise ValidationError("Balance must be a valid number.")
    # "NaN" and "Infinity" parse as Decimals but can't be compared or stored.
    if not value.is_finite():
        raise ValidationError("Balance must be a valid number.")
    if value < 0:
        raise ValidationError("Balance must be zero or positive. A liability's balance is what's owed, not a negative number.")
    return value


def _apply_form(account: Account, form, partial: bool = False) -> None:
    """Validates and applies an account's fields. The web form always
    sends every field. The native app sends only what changed
    (`partial=True`), so a field it leaves out keeps its value -- and the
    balance's "updated" time only moves when a balance was actually sent
    (which is how someone confirms a balance is still right), never on a
    rename alone. Validates everything before changing anything."""
    def sent(key: str) -> bool:
        return not partial or key in form

    name = str(form.get("name") or "").strip() if sent("name") else account.name
    if not name:
        raise ValidationError("Account name is required.")

    account_type = (form.get("account_type") or "checking") if sent("account_type") else account.account_type
    if account_type not in ACCOUNT_TYPES:
        raise ValidationError("Unknown account type.")

    currency = str(form.get("currency") or "USD").strip().upper() if sent("currency") else account.currency
    if currency not in CURRENCY_CODES:
        raise ValidationError("Unsupported currency.")

    balance = _parse_balance(form.get("current_balance")) if sent("current_balance") else None

    account.name = name[:120]
    account.account_type = account_type
    account.currency = currency
    if balance is not None:
        account.current_balance = balance
        account.balance_updated_at = datetime.now(timezone.utc)


def save_account(user, account: Account, fields, partial: bool = False) -> Account:
    """Validates and saves a new or changed account for `user`, then
    records today's net worth snapshot. Shared by the web forms below and
    the native app's API. Raises ValidationError with a message meant for
    the person; any other exception is a real failure (already rolled
    back)."""
    _apply_form(account, fields, partial=partial)
    try:
        if account.id is None:
            db.session.add(account)
        db.session.commit()
        _upsert_net_worth_snapshot(user)
    except Exception:
        db.session.rollback()
        raise
    return account


def delete_account_record(user, account: Account) -> None:
    """Deletes one account and records today's net worth snapshot."""
    try:
        db.session.delete(account)
        db.session.commit()
        _upsert_net_worth_snapshot(user)
    except Exception:
        db.session.rollback()
        raise


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


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@accounts_bp.route("/")
@login_required
def index():
    """Wealth: net worth now and over time, and every account behind it,
    from the same dict the native app's Wealth tab reads
    (services/screens.py's wealth_screen)."""
    from ...services.screens import wealth_screen

    wealth = wealth_screen(current_user)
    groups = {"cash": "Cash", "investments": "Investments", "property": "Property", "debts": "Debts"}
    grouped = []
    for key, label in groups.items():
        rows = [a for a in wealth["accounts"] if a["group"] == key]
        if not rows:
            continue
        for a in rows:
            days = a["days_since_update"] or 0
            a["staleness"] = "stale" if days >= 90 else ("aging" if days >= 30 else "fresh")
        total = sum(
            (Decimal(a["display_balance"] or a["balance"]) for a in rows),
            Decimal("0"),
        )
        grouped.append({"key": key, "label": label, "accounts": rows, "total": total})

    change = None
    history = wealth["history"]
    if len(history) >= 2:
        first, last = history[0], history[-1]
        change = {
            "amount": Decimal(last["net_worth"]) - Decimal(first["net_worth"]),
            "since": datetime.strptime(first["date"], "%Y-%m-%d").strftime("%b %-d, %Y"),
        }

    return render_template("accounts.html", wealth=wealth, groups=grouped, change=change)


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
        save_account(current_user, account, request.form)
        flash("Account added!", "success")
        return redirect(url_for("accounts.index"))
    except ValidationError as exc:
        flash(str(exc), "error")
        return redirect(url_for("accounts.add"))
    except Exception as e:
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
        save_account(current_user, account, request.form)
        flash("Account updated!", "success")
        return redirect(url_for("accounts.index"))
    except ValidationError as exc:
        flash(str(exc), "error")
        return redirect(url_for("accounts.edit", account_id=account_id))
    except Exception as e:
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
        delete_account_record(current_user, account)
        if is_ajax_request():
            return jsonify({"message": "Account deleted."})
        flash("Account deleted.", "success")
        return redirect(url_for("accounts.index"))
    except Exception as e:
        log.error("Error deleting account: %s", e, exc_info=True)
        if is_ajax_request():
            return jsonify({"message": "Error deleting account."}), 500
        flash("Error deleting account.", "error")
        return redirect(url_for("accounts.index"))
