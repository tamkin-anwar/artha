import logging
from datetime import datetime, timezone

from flask import abort, jsonify, render_template, request
from flask_login import current_user, login_required
from sqlalchemy import func

from ...extensions import db
from ...models import Transaction, User
from ...models.feedback import VALID_STATUSES, Feedback
from . import admin_bp

log = logging.getLogger(__name__)


def _time_ago(dt) -> str:
    """Compact relative-time string ('5m ago', '3h ago', '2d ago'), falling
    back to an absolute date past 30 days. Normalizes to naive UTC first
    since User.created_at can be either naive (backfilled via a raw SQL
    CURRENT_TIMESTAMP for pre-existing accounts) or timezone-aware (new
    rows, set via datetime.now(timezone.utc)) — subtracting one from the
    other directly would raise."""
    if dt is None:
        return "Never"
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    seconds = (datetime.utcnow() - dt).total_seconds()
    if seconds < 60:
        return "Just now"
    minutes = seconds / 60
    if minutes < 60:
        return f"{int(minutes)}m ago"
    hours = minutes / 60
    if hours < 24:
        return f"{int(hours)}h ago"
    days = hours / 24
    if days < 30:
        return f"{int(days)}d ago"
    return dt.strftime("%b %d, %Y")


@admin_bp.before_request
@login_required
def require_admin():
    if not current_user.is_admin:
        abort(403)


@admin_bp.route("/")
def overview():
    users = User.query.order_by(User.created_at.desc()).all()
    user_rows = [
        {
            "id": u.id,
            "name": (f"{u.first_name or ''} {u.last_name or ''}".strip() or u.username),
            "username": u.username,
            "email": u.email,
            "is_admin": u.is_admin,
            "joined_label": _time_ago(u.created_at),
            "last_active_label": _time_ago(u.last_login_at),
        }
        for u in users
    ]

    status_filter = request.args.get("status", "all")
    feedback_query = Feedback.query.order_by(Feedback.created_at.desc())
    if status_filter in VALID_STATUSES:
        feedback_query = feedback_query.filter_by(status=status_filter)
    feedback_items = [
        {
            "id": f.id,
            "category": f.category,
            "message": f.message,
            "page_url": f.page_url,
            "status": f.status,
            "author_name": (f.author.first_name or f.author.username) if f.author else "Unknown",
            "created_label": _time_ago(f.created_at),
        }
        for f in feedback_query.limit(200).all()
    ]

    open_count = Feedback.query.filter_by(status="new").count()
    total_transactions = db.session.query(func.count(Transaction.id)).scalar() or 0

    return render_template(
        "admin.html",
        user_rows=user_rows,
        feedback_items=feedback_items,
        status_filter=status_filter,
        open_count=open_count,
        total_feedback=Feedback.query.count(),
        total_users=len(users),
        total_transactions=total_transactions,
    )


# ---------------------------------------------------------------------------
# Sidebar badge — registered here (not gated by before_request, since it
# needs to run on every page, not just /admin/*) so non-admins never pay
# for the query and the "Admin" nav link only appears with a live count.
# ---------------------------------------------------------------------------

@admin_bp.app_context_processor
def inject_admin_badge():
    if not current_user.is_authenticated or not current_user.is_admin:
        return {}
    return {"admin_open_feedback_count": Feedback.query.filter_by(status="new").count()}


@admin_bp.route("/feedback/<int:item_id>/status", methods=["PATCH"])
def update_feedback_status(item_id):
    item = db.session.get(Feedback, item_id)
    if item is None:
        return jsonify({"message": "Not found"}), 404

    data = request.get_json(silent=True) or {}
    new_status = data.get("status")
    if new_status not in VALID_STATUSES:
        return jsonify({"message": "Invalid status"}), 400

    item.status = new_status
    try:
        db.session.commit()
        # The client uses this to keep the sidebar "Admin (N)" badge and
        # this page's own "Open feedback" tile in sync — both are computed
        # server-side at page load and otherwise have no way to know a
        # status change (here, or on another tab) touched the count.
        open_count = Feedback.query.filter_by(status="new").count()
        return jsonify({"status": item.status, "open_count": open_count})
    except Exception as e:
        db.session.rollback()
        log.error("Error updating feedback status: %s", e, exc_info=True)
        return jsonify({"message": "Database error"}), 500
