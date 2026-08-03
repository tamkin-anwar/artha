import logging

from flask import jsonify, request
from flask_login import current_user, login_required

from ...extensions import db
from ...models.push_subscription import PushSubscription
from . import push_bp

log = logging.getLogger(__name__)


@push_bp.route("/subscribe", methods=["POST"])
@login_required
def subscribe():
    data = request.get_json(silent=True) or {}
    endpoint = (data.get("endpoint") or "").strip()
    keys = data.get("keys") or {}
    p256dh = (keys.get("p256dh") or "").strip()
    auth = (keys.get("auth") or "").strip()

    if not endpoint or not p256dh or not auth:
        return jsonify({"message": "Invalid subscription"}), 400

    row = PushSubscription.query.filter_by(endpoint=endpoint).first()
    if row is None:
        row = PushSubscription(user_id=current_user.id, endpoint=endpoint, p256dh=p256dh, auth=auth)
        db.session.add(row)
    else:
        # Same browser can re-subscribe (e.g. after clearing permission) —
        # reassign to whoever's logged in now and refresh the keys rather
        # than erroring on the unique constraint.
        row.user_id = current_user.id
        row.p256dh = p256dh
        row.auth = auth

    try:
        db.session.commit()
        return jsonify({"message": "Subscribed"}), 201
    except Exception as e:
        db.session.rollback()
        log.error("Error saving push subscription: %s", e, exc_info=True)
        return jsonify({"message": "Database error"}), 500


@push_bp.route("/unsubscribe", methods=["POST"])
@login_required
def unsubscribe():
    data = request.get_json(silent=True) or {}
    endpoint = (data.get("endpoint") or "").strip()

    row = PushSubscription.query.filter_by(endpoint=endpoint, user_id=current_user.id).first()
    if row is not None:
        db.session.delete(row)
        db.session.commit()

    return jsonify({"message": "Unsubscribed"})
