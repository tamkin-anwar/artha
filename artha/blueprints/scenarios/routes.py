import logging
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from flask import abort, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import func

from ...extensions import db
from ...models import Transaction
from ...models.scenario import VALID_PRIORITIES, VALID_STATUSES, Scenario
from ...utils import is_ajax_request
from . import scenarios_bp

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

class ValidationError(Exception):
    pass


def _parse_decimal(raw, field_name: str) -> Decimal:
    raw = (raw or "").strip() or "0"
    try:
        value = Decimal(raw)
    except InvalidOperation:
        raise ValidationError(f"{field_name} must be a valid number.")
    if value < 0:
        raise ValidationError(f"{field_name} must be non-negative.")
    return value


def _parse_scale(raw, field_name: str, default: int = 5) -> int:
    raw = (raw or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValidationError(f"{field_name} must be a whole number.")
    if not (1 <= value <= 10):
        raise ValidationError(f"{field_name} must be between 1 and 10.")
    return value


def _parse_date(raw, field_name: str):
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        raise ValidationError(f"{field_name} must be a valid date (YYYY-MM-DD).")


def _current_balance(user_id: int) -> Decimal:
    income = (
        db.session.query(func.sum(Transaction.amount))
        .filter_by(user_id=user_id, type="income")
        .scalar()
        or Decimal("0")
    )
    expense = (
        db.session.query(func.sum(Transaction.amount))
        .filter_by(user_id=user_id, type="expense")
        .scalar()
        or Decimal("0")
    )
    return income - expense


def _monthly_income(user_id: int, months: int = 3) -> Decimal:
    """Average monthly income over the trailing N months of transaction history."""
    since = datetime.utcnow() - timedelta(days=30 * months)
    total = (
        db.session.query(func.sum(Transaction.amount))
        .filter(
            Transaction.user_id == user_id,
            Transaction.type == "income",
            Transaction.timestamp >= since,
        )
        .scalar()
        or Decimal("0")
    )
    return Decimal(total) / months


def _monthly_expense(user_id: int, months: int = 3) -> Decimal:
    """Average monthly expense over the trailing N months — the expense-side
    twin of _monthly_income(), used for the same projected-month fallback."""
    since = datetime.utcnow() - timedelta(days=30 * months)
    total = (
        db.session.query(func.sum(Transaction.amount))
        .filter(
            Transaction.user_id == user_id,
            Transaction.type == "expense",
            Transaction.timestamp >= since,
        )
        .scalar()
        or Decimal("0")
    )
    return Decimal(total) / months


def _month_totals(user_id: int, target: date) -> dict:
    """
    Real income/expense/net for the calendar month containing `target`,
    straight from Transaction rows — same bucketing approach as
    finance_page()'s bucket_for() (artha/blueprints/finance/routes.py),
    just scoped to one month instead of a full year's worth of buckets.
    has_data is False when nothing has been recorded for that month yet
    (e.g. a scenario dated for a future month) — callers fall back to a
    projected estimate rather than showing a misleading $0/$0 real month.
    """
    start = date(target.year, target.month, 1)
    next_month = date(target.year + 1, 1, 1) if target.month == 12 else date(target.year, target.month + 1, 1)

    income = (
        db.session.query(func.sum(Transaction.amount))
        .filter(
            Transaction.user_id == user_id,
            Transaction.type == "income",
            Transaction.timestamp >= start,
            Transaction.timestamp < next_month,
        )
        .scalar()
        or Decimal("0")
    )
    expense = (
        db.session.query(func.sum(Transaction.amount))
        .filter(
            Transaction.user_id == user_id,
            Transaction.type == "expense",
            Transaction.timestamp >= start,
            Transaction.timestamp < next_month,
        )
        .scalar()
        or Decimal("0")
    )
    return {
        "month_start": start,
        "income": income,
        "expense": expense,
        "net": income - expense,
        "has_data": income > 0 or expense > 0,
    }


def _scenario_month_comparison(scenario: Scenario) -> dict:
    """
    The real (or, absent real data, projected) month this scenario should
    be compared against, plus what that month's net looks like with the
    scenario's cost/savings applied — the direct "how does this affect my
    finances" answer the numbers-only stat cards below don't give on their
    own. Target month = scenario.start_date's month if set, else today's
    month, so an undated scenario defaults to "this month" like the user
    actually asks it ("what if I did this THIS month").
    """
    target = scenario.start_date or date.today()
    totals = _month_totals(scenario.user_id, target)

    if not totals["has_data"]:
        avg_income = _monthly_income(scenario.user_id)
        avg_expense = _monthly_expense(scenario.user_id)
        totals = {
            "month_start": date(target.year, target.month, 1),
            "income": avg_income,
            "expense": avg_expense,
            "net": avg_income - avg_expense,
            "has_data": False,
        }

    # target's own month is exactly what's being compared against, so the
    # one-time cost (if any) always lands here — no separate check needed.
    scenario_net_effect = scenario.monthly_savings - scenario.monthly_cost - scenario.one_time_cost
    net_with_scenario = totals["net"] + scenario_net_effect

    # Bar widths for the before/after visual, scaled against whichever side
    # is larger so the two bars stay comparable at a glance.
    max_abs = max(abs(totals["net"]), abs(net_with_scenario), Decimal("1"))
    bar_before_pct = float(abs(totals["net"]) / max_abs * 100)
    bar_after_pct = float(abs(net_with_scenario) / max_abs * 100)

    return {
        **totals,
        "projected": not totals["has_data"],
        "net_with_scenario": net_with_scenario,
        "bar_before_pct": bar_before_pct,
        "bar_after_pct": bar_after_pct,
    }


def _verdict(scenario: Scenario) -> dict:
    """
    Rule-based verdict + risk level for the premium scenario UI. No AI call.

    Anchored to the scenario's real (or, absent real data, projected)
    target month via _scenario_month_comparison() instead of the old
    abstract "cost > 3x average income" heuristic — the numbers lead,
    financial_risk (the user's own 1-10 slider) is a tie-breaker/amplifier
    on top of them, not the dominant signal it used to be.
    """
    comparison = _scenario_month_comparison(scenario)
    net_before = comparison["net"]
    net_after = comparison["net_with_scenario"]
    flips_negative = net_before > 0 and net_after < 0

    if flips_negative or scenario.financial_risk >= 8:
        label = "bad_idea"
    elif net_after < 0:
        label = "bad_idea" if scenario.financial_risk >= 6 else "wait"
    elif scenario.financial_risk <= 5:
        label = "do_it"
    else:
        label = "wait"

    if scenario.financial_risk <= 3:
        risk_level = "low"
    elif scenario.financial_risk <= 6:
        risk_level = "medium"
    else:
        risk_level = "high"

    def _money_str(v: Decimal) -> str:
        return f"-${abs(v):,.2f}" if v < 0 else f"${v:,.2f}"

    month_label = comparison["month_start"].strftime("%B %Y")
    kind = "projected" if comparison["projected"] else "real"
    before_str = _money_str(net_before)
    after_str = _money_str(net_after)

    if label == "bad_idea":
        if flips_negative:
            insight = (
                f"{month_label}'s {kind} net is {before_str} — this scenario would drop it to "
                f"{after_str}, turning a positive month negative."
            )
        elif net_after < 0:
            insight = (
                f"{month_label}'s {kind} net would be {after_str} with this scenario applied — "
                f"already a stretch before factoring in the {scenario.financial_risk}/10 risk you gave it."
            )
        else:
            insight = (
                f"Financial risk is rated {scenario.financial_risk}/10 — high enough that "
                f"{month_label}'s otherwise-workable numbers ({before_str} → {after_str}) "
                "shouldn't be the only thing driving this."
            )
    elif label == "do_it":
        insight = (
            f"{month_label}'s {kind} net is {before_str}; with this scenario it's "
            f"{after_str} — still comfortably positive."
        )
    else:
        insight = (
            f"{month_label}'s {kind} net is {before_str}; with this scenario it's "
            f"{after_str} — workable, but worth weighing before committing."
        )

    return {"label": label, "risk_level": risk_level, "insight": insight, "comparison": comparison}


def _scenario_compare_payload(scenario: Scenario, verdict: dict) -> dict:
    """JSON-safe (no Decimal/date) snapshot of a scenario + its verdict for
    the client-side Compare modal — the modal reads this straight out of a
    <script type="application/json"> tag rather than hitting a new route."""
    comparison = verdict["comparison"]
    return {
        "id": scenario.id,
        "title": scenario.title,
        "category": scenario.category,
        "one_time_cost": float(scenario.one_time_cost),
        "monthly_cost": float(scenario.monthly_cost),
        "monthly_savings": float(scenario.monthly_savings),
        "net_monthly_impact": float(scenario.net_monthly_impact),
        "financial_risk": scenario.financial_risk,
        "verdict_label": verdict["label"],
        "risk_level": verdict["risk_level"],
        "insight": verdict["insight"],
        "month_label": comparison["month_start"].strftime("%B %Y"),
        "projected": comparison["projected"],
        "income": float(comparison["income"]),
        "expense": float(comparison["expense"]),
        "net_before": float(comparison["net"]),
        "net_after": float(comparison["net_with_scenario"]),
    }


def _get_owned_scenario(scenario_id: int) -> Scenario:
    scenario = db.session.get(Scenario, scenario_id)
    if scenario is None or scenario.user_id != current_user.id:
        abort(404)
    return scenario


def _apply_form(scenario: Scenario, form) -> None:
    """Validate + apply submitted form fields onto scenario (new or existing)."""
    title = (form.get("title") or "").strip()
    if not title:
        raise ValidationError("Title is required.")

    one_time_cost = _parse_decimal(form.get("one_time_cost"), "One-time cost")
    monthly_cost = _parse_decimal(form.get("monthly_cost"), "Monthly cost")
    monthly_savings = _parse_decimal(form.get("monthly_savings"), "Monthly savings")
    emotional_value = _parse_scale(form.get("emotional_value"), "Emotional value")
    financial_risk = _parse_scale(form.get("financial_risk"), "Financial risk")
    start_date = _parse_date(form.get("start_date"), "Start date")
    end_date = _parse_date(form.get("end_date"), "End date")

    if start_date and end_date and end_date < start_date:
        raise ValidationError("End date can't be before start date.")

    priority = form.get("priority") or "medium"
    if priority not in VALID_PRIORITIES:
        priority = "medium"

    status = form.get("status") or "active"
    if status not in VALID_STATUSES:
        status = "active"

    scenario.title = title
    scenario.category = (form.get("category") or "other").strip() or "other"
    scenario.description = (form.get("description") or "").strip() or None
    scenario.one_time_cost = one_time_cost
    scenario.monthly_cost = monthly_cost
    scenario.monthly_savings = monthly_savings
    scenario.start_date = start_date
    scenario.end_date = end_date
    scenario.priority = priority
    scenario.emotional_value = emotional_value
    scenario.financial_risk = financial_risk
    scenario.notes = (form.get("notes") or "").strip() or None
    scenario.status = status


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@scenarios_bp.route("/")
@login_required
def index():
    status_filter = (request.args.get("status") or "").strip()

    query = Scenario.query.filter_by(user_id=current_user.id)
    if status_filter in VALID_STATUSES:
        query = query.filter_by(status=status_filter)
    scenarios = query.order_by(Scenario.created_at.desc()).all()

    balance = _current_balance(current_user.id)
    monthly_income = _monthly_income(current_user.id)
    verdicts = {s.id: _verdict(s) for s in scenarios}
    compare_data = [_scenario_compare_payload(s, verdicts[s.id]) for s in scenarios]

    return render_template(
        "scenarios.html",
        scenarios=scenarios,
        balance=balance,
        monthly_income=monthly_income,
        verdicts=verdicts,
        compare_data=compare_data,
        status_filter=status_filter,
        valid_statuses=VALID_STATUSES,
    )


@scenarios_bp.route("/add", methods=["GET", "POST"])
@login_required
def add():
    if request.method == "GET":
        return render_template(
            "scenario_form.html",
            scenario=None,
            mode="add",
            valid_priorities=VALID_PRIORITIES,
            valid_statuses=VALID_STATUSES,
        )

    scenario = Scenario(user_id=current_user.id)
    try:
        _apply_form(scenario, request.form)
    except ValidationError as exc:
        flash(str(exc), "error")
        return redirect(url_for("scenarios.add"))

    try:
        db.session.add(scenario)
        db.session.commit()
        flash("Scenario created!", "success")
        return redirect(url_for("scenarios.detail", scenario_id=scenario.id))
    except Exception as e:
        db.session.rollback()
        log.error("Error creating scenario: %s", e, exc_info=True)
        flash("Error creating scenario.", "error")
        return redirect(url_for("scenarios.add"))


@scenarios_bp.route("/<int:scenario_id>")
@login_required
def detail(scenario_id):
    scenario = _get_owned_scenario(scenario_id)
    balance = _current_balance(current_user.id)
    monthly_income = _monthly_income(current_user.id)

    scenarios = (
        Scenario.query.filter_by(user_id=current_user.id)
        .order_by(Scenario.created_at.desc())
        .all()
    )
    verdicts = {s.id: _verdict(s) for s in scenarios}

    return render_template(
        "scenario_detail.html",
        scenario=scenario,
        scenarios=scenarios,
        verdicts=verdicts,
        balance=balance,
        monthly_income=monthly_income,
        status_filter="",
        valid_statuses=VALID_STATUSES,
        recommendation=scenario.recommendation(balance),
        insight=scenario.insight(balance),
    )


@scenarios_bp.route("/<int:scenario_id>/edit", methods=["GET", "POST"])
@login_required
def edit(scenario_id):
    scenario = _get_owned_scenario(scenario_id)

    if request.method == "GET":
        return render_template(
            "scenario_form.html",
            scenario=scenario,
            mode="edit",
            valid_priorities=VALID_PRIORITIES,
            valid_statuses=VALID_STATUSES,
        )

    try:
        _apply_form(scenario, request.form)
    except ValidationError as exc:
        db.session.rollback()
        flash(str(exc), "error")
        return redirect(url_for("scenarios.edit", scenario_id=scenario_id))

    try:
        db.session.commit()
        flash("Scenario updated!", "success")
        return redirect(url_for("scenarios.detail", scenario_id=scenario.id))
    except Exception as e:
        db.session.rollback()
        log.error("Error updating scenario: %s", e, exc_info=True)
        flash("Error updating scenario.", "error")
        return redirect(url_for("scenarios.edit", scenario_id=scenario_id))


@scenarios_bp.route("/<int:scenario_id>/delete", methods=["POST"])
@login_required
def delete(scenario_id):
    scenario = db.session.get(Scenario, scenario_id)
    if scenario is None or scenario.user_id != current_user.id:
        if is_ajax_request():
            return jsonify({"message": "Not found"}), 404
        flash("Scenario not found.", "error")
        return redirect(url_for("scenarios.index"))

    try:
        db.session.delete(scenario)
        db.session.commit()
        if is_ajax_request():
            return jsonify({"message": "Scenario deleted."})
        flash("Scenario deleted.", "success")
        return redirect(url_for("scenarios.index"))
    except Exception as e:
        db.session.rollback()
        log.error("Error deleting scenario: %s", e, exc_info=True)
        if is_ajax_request():
            return jsonify({"message": "Error deleting scenario."}), 500
        flash("Error deleting scenario.", "error")
        return redirect(url_for("scenarios.index"))


@scenarios_bp.route("/<int:scenario_id>/archive", methods=["POST"])
@login_required
def archive(scenario_id):
    scenario = db.session.get(Scenario, scenario_id)
    if scenario is None or scenario.user_id != current_user.id:
        if is_ajax_request():
            return jsonify({"message": "Not found"}), 404
        flash("Scenario not found.", "error")
        return redirect(url_for("scenarios.index"))

    scenario.status = "archived"
    try:
        db.session.commit()
        if is_ajax_request():
            return jsonify({"message": "Scenario archived."})
        flash("Scenario archived.", "success")
        return redirect(url_for("scenarios.index"))
    except Exception as e:
        db.session.rollback()
        log.error("Error archiving scenario: %s", e, exc_info=True)
        if is_ajax_request():
            return jsonify({"message": "Error archiving scenario."}), 500
        flash("Error archiving scenario.", "error")
        return redirect(url_for("scenarios.index"))


# ---------------------------------------------------------------------------
# Dashboard widget data — registered here (not in dashboard/routes.py) so the
# widget's data is available to templates/index.html without modifying the
# existing dashboard blueprint.
# ---------------------------------------------------------------------------

@scenarios_bp.app_context_processor
def inject_scenario_widget_data():
    if not current_user.is_authenticated or request.endpoint != "dashboard.index":
        return {}

    active = (
        Scenario.query.filter_by(user_id=current_user.id, status="active")
        .order_by(Scenario.created_at.desc())
        .all()
    )
    total_monthly_impact = sum((s.net_monthly_impact for s in active), Decimal("0"))
    top_three = active[:3]

    return {
        "scenario_widget_scenarios": top_three,
        "scenario_widget_total_count": len(active),
        "scenario_widget_total_monthly_impact": total_monthly_impact,
        # Same _verdict() the detail/list pages use — previously this
        # widget computed its own simplified verdict inline in the
        # template, so it could disagree with the detail page for the
        # exact same scenario.
        "scenario_widget_verdicts": {s.id: _verdict(s) for s in top_three},
    }
