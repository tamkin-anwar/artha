"""Savings goals (models/goal.py): saving them and describing them, shared
by the website's Plan page and the native app's API, so both say the same
thing about how a goal is going.

A goal's amounts stay in its own currency (like an account's balance); a
goal is one thing, not a total, so it's never converted.
"""

import math
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from ..extensions import db
from ..models import Goal

MAX_GOALS = 20
NAME_MAX = 80
_CENT = Decimal("0.01")


class GoalError(ValueError):
    """Something about a goal that can't be saved as given. The message
    is written for the person."""


def _amount(raw, field: str, allow_zero: bool) -> Decimal:
    text = str(raw if raw is not None else "").strip().replace(",", "")
    if not text:
        raise GoalError(f"Enter the {field}.")
    try:
        value = Decimal(text).quantize(_CENT)
    except (InvalidOperation, ValueError):
        raise GoalError(f"The {field} needs to be a number.")
    if value < 0 or (value == 0 and not allow_zero):
        raise GoalError(f"The {field} needs to be above zero." if not allow_zero else f"The {field} can't be negative.")
    if value >= Decimal("10000000000"):
        raise GoalError(f"The {field} is too large.")
    return value


def _target_date(raw) -> date | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        raise GoalError("The date needs to look like 2027-06-30.")


def save_goal(user, goal: Goal | None, fields, partial: bool = False) -> Goal:
    """Creates a goal (goal=None) or edits one, and commits. `fields` is
    anything with .get(): name, target_amount, saved_amount, target_date
    (blank clears it). With partial=True only the fields present are
    touched (the app's PATCH)."""
    creating = goal is None
    if creating:
        if Goal.query.filter_by(user_id=user.id).count() >= MAX_GOALS:
            raise GoalError(f"You can have up to {MAX_GOALS} goals.")
        goal = Goal(user_id=user.id, saved_amount=Decimal("0"))

    def present(key):
        return not partial or creating or key in fields

    if present("name"):
        name = str(fields.get("name") or "").strip()
        if not name:
            raise GoalError("Give the goal a name.")
        goal.name = name[:NAME_MAX]
    if present("target_amount"):
        goal.target_amount = _amount(fields.get("target_amount"), "goal amount", allow_zero=False)
    if "saved_amount" in fields:
        goal.saved_amount = _amount(fields.get("saved_amount") or "0", "amount saved", allow_zero=True)
    if present("target_date"):
        goal.target_date = _target_date(fields.get("target_date"))
    # Captured when the amounts are typed (creating, or re-entering the
    # goal amount), never on a rename alone, which would relabel the
    # same numbers as a different currency.
    if creating or "target_amount" in fields:
        goal.currency = user.preferred_currency or "USD"

    if creating:
        db.session.add(goal)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    return goal


def add_to_goal(goal: Goal, raw_amount) -> Goal:
    """Adds money to (or, with a negative amount, takes it back out of) a
    goal, never below zero, and commits."""
    text = str(raw_amount if raw_amount is not None else "").strip().replace(",", "")
    try:
        change = Decimal(text).quantize(_CENT)
    except (InvalidOperation, ValueError):
        raise GoalError("Enter an amount.")
    if change == 0:
        raise GoalError("Enter an amount other than zero.")
    goal.saved_amount = max(Decimal(goal.saved_amount or 0) + change, Decimal("0"))
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    return goal


def delete_goal(goal: Goal) -> None:
    try:
        db.session.delete(goal)
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise


def months_until(today: date, target: date) -> int:
    """Whole months left to save in, counting this one: due later this
    month is 1, due next month is 2. Zero once the date has passed."""
    if target < today:
        return 0
    return (target.year - today.year) * 12 + (target.month - today.month) + 1


def describe_goal(goal: Goal, today: date) -> dict:
    """What Plan shows for a goal. Money as two-decimal strings, in the
    goal's own currency. `per_month` is what to set aside each month from
    now to reach it by its date (null without a date, or once reached);
    `status` is "done", "overdue" (date passed, not reached) or "saving"."""
    target = Decimal(goal.target_amount)
    saved = Decimal(goal.saved_amount or 0)
    left = max(target - saved, Decimal("0"))
    done = saved >= target
    months_left = months_until(today, goal.target_date) if goal.target_date else None
    per_month = None
    if not done and months_left:
        per_month = (left / months_left).quantize(_CENT)
    if done:
        status = "done"
    elif goal.target_date and goal.target_date < today:
        status = "overdue"
    else:
        status = "saving"
    return {
        "id": goal.id,
        "name": goal.name,
        "currency": goal.currency,
        "target": str(target.quantize(_CENT)),
        "saved": str(saved.quantize(_CENT)),
        "left": str(left.quantize(_CENT)),
        "percent": min(100, math.floor(saved / target * 100)) if target > 0 else 0,
        "target_date": goal.target_date.isoformat() if goal.target_date else None,
        "months_left": months_left,
        "per_month": str(per_month) if per_month is not None else None,
        "status": status,
    }


def goals_for(user, today: date) -> list[dict]:
    """Every goal, still-saving ones first (soonest date first), then
    reached ones."""
    goals = Goal.query.filter_by(user_id=user.id).order_by(Goal.created_at.asc(), Goal.id.asc()).all()
    described = [describe_goal(g, today) for g in goals]
    described.sort(key=lambda g: (g["status"] == "done", g["target_date"] or "9999-12-31"))
    return described
