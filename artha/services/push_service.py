"""
artha/services/push_service.py
--------------------------------
Thin wrapper around pywebpush so callers never touch VAPID/subscription
plumbing directly.
"""

import json
import logging

from flask import current_app
from pywebpush import WebPushException, webpush

log = logging.getLogger(__name__)


def send_push(subscription, title: str, body: str, url: str = "/") -> str:
    """
    Sends one push notification to one subscription.

    Returns one of:
      "sent"  — delivered
      "gone"  — the push service returned 404/410: the browser has
                permanently invalidated this subscription (uninstalled,
                permission revoked, ...) — caller should delete the row
      "error" — anything else (network error, malformed keys, a
                transient 5xx, ...) — caller should leave the row alone
                and let the next scheduled run retry it

    Deliberately catches Exception, not just WebPushException: a
    malformed key (corrupt row, a future browser API changing the
    subscription shape) fails inside WebPusher's own base64 decoding
    before it ever makes an HTTP request, and one bad row should never
    crash the whole daily batch for every other user.
    """
    try:
        webpush(
            subscription_info={
                "endpoint": subscription.endpoint,
                "keys": {"p256dh": subscription.p256dh, "auth": subscription.auth},
            },
            data=json.dumps({"title": title, "body": body, "url": url}),
            vapid_private_key=current_app.config["VAPID_PRIVATE_KEY"],
            vapid_claims={"sub": f"mailto:{current_app.config['VAPID_CLAIMS_EMAIL']}"},
        )
        return "sent"
    except WebPushException as exc:
        status = exc.response.status_code if exc.response is not None else None
        if status in (404, 410):
            log.info("Push subscription gone (status %s): %s", status, subscription.endpoint[:60])
            return "gone"
        log.error("Push failed (status %s): %s", status, exc)
        return "error"
    except Exception as exc:
        log.error("Push failed unexpectedly for %s: %s", subscription.endpoint[:60], exc, exc_info=True)
        return "error"
