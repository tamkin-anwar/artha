import logging

from flask import jsonify, request
from flask_login import current_user, login_required

from ...extensions import db
from ...models.feedback import VALID_CATEGORIES, Feedback
from . import feedback_bp

log = logging.getLogger(__name__)


@feedback_bp.route("/feedback", methods=["POST"])
@login_required
def submit():
    data = request.get_json(silent=True) or {}

    category = data.get("category") or "bug"
    if category not in VALID_CATEGORIES:
        category = "other"

    message = (data.get("message") or "").strip()
    if not message:
        return jsonify({"message": "Feedback can't be empty"}), 400
    if len(message) > 4000:
        return jsonify({"message": "Feedback is too long"}), 400

    # The page the user was on when they hit the feedback button — sent by
    # the client as window.location.pathname, not derived from the Referer
    # header, since this is an XHR POST that the client controls directly.
    page_url = (data.get("page_url") or "").strip()[:255] or None

    item = Feedback(
        user_id=current_user.id,
        category=category,
        message=message,
        page_url=page_url,
    )
    try:
        db.session.add(item)
        db.session.commit()
        return jsonify({"message": "Thanks, got it."}), 201
    except Exception as e:
        db.session.rollback()
        log.error("Error saving feedback: %s", e, exc_info=True)
        return jsonify({"message": "Something went wrong. Try again."}), 500
