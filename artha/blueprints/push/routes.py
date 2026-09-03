import logging

from flask import jsonify, request
from flask_login import current_user, login_required

from ...extensions import csrf, db
from ...models.push_subscription import PushSubscription
from . import push_bp

log = logging.getLogger(__name__)


@push_bp.route("/subscribe", methods=["POST"])
@csrf.exempt
@login_required
def subscribe():
    """
    CSRF-exempt: this is also called from inside the service worker's own
    pushsubscriptionchange handler (Chrome/FCM can silently rotate a
    subscription's endpoint without the page ever being open), and a
    service worker has no DOM to read the CSRF meta tag from. Still
    requires a valid session cookie (@login_required), and the payload
    itself can only come from a real pushManager.subscribe() call, which
    only succeeds for a page/worker running on this exact origin with
    notification permission already granted for it — a third-party site
    can't forge one, so the usual CSRF threat (a hostile page silently
    submitting a form on the victim's behalf) doesn't apply here the way
    it would for a normal state-changing form post.
    """
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


@push_bp.route("/preferences", methods=["POST"])
@login_required
def set_preferences():
    """Upsert which kinds of "due today" reminders this user wants.
    Per-user (on User itself), not per-subscription — someone with a
    phone and a laptop subscribed wants one shared preference, not one
    per device. A key not sent leaves that flag untouched, so each
    checkbox can POST independently without clobbering the others."""
    data = request.get_json(silent=True) or {}
    if "notify_bills_due" in data:
        current_user.notify_bills_due = bool(data.get("notify_bills_due"))
    if "notify_notes_due" in data:
        current_user.notify_notes_due = bool(data.get("notify_notes_due"))
    if "notify_events_due" in data:
        current_user.notify_events_due = bool(data.get("notify_events_due"))
    if "reminder_hour" in data:
        hour = data.get("reminder_hour")
        if isinstance(hour, bool) or not isinstance(hour, int) or not (0 <= hour <= 23):
            return jsonify({"message": "Invalid reminder hour"}), 400
        current_user.reminder_hour = hour
    db.session.commit()
    return jsonify({"message": "Preferences updated"})
