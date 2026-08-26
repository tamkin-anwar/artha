"""
artha/services/email_service.py
---------------------------------
Password-reset email, sent via Resend (resend.com).

Architecture decisions:
  - Lazily imported here, not at module load: mirrors pdfplumber's lazy
    import in the CSV/PDF import path (artha/blueprints/finance/routes.py)
    — only this one code path needs the dependency, and it keeps the
    package importable in any environment that hasn't installed it yet.
  - No-op, not an exception, when RESEND_API_KEY is unset: local dev and
    CI never configure a real Resend account, and a forgotten-password
    flow with no mail service configured should behave like "nothing to
    send" (logged clearly) rather than take down the request that
    triggered it.
  - HTML + plaintext both sent: some inboxes render plaintext-only, and
    it costs nothing extra to include.
"""

from __future__ import annotations

import logging

from flask import current_app

log = logging.getLogger(__name__)


def send_password_reset_email(user, reset_url: str) -> bool:
    """Sends the password-reset link to `user`. Returns True if a send was
    attempted and Resend accepted it, False if skipped (no API key) or the
    send itself failed — callers should treat both False cases the same
    way (log-worthy, but never surfaced to the requester, see
    _forgot_password's no-enumeration flash)."""
    api_key = current_app.config.get("RESEND_API_KEY")
    from_address = current_app.config.get("RESET_EMAIL_FROM")

    if not api_key or not from_address:
        log.warning(
            "RESEND_API_KEY/RESET_EMAIL_FROM not configured — skipping password reset email to %s",
            user.email,
        )
        return False

    import resend

    resend.api_key = api_key

    first_name = user.first_name or user.username
    text_body = (
        f"Hi {first_name},\n\n"
        "Someone requested a password reset for your Artha account. "
        f"Click the link below to set a new password:\n\n{reset_url}\n\n"
        "This link expires in 1 hour. If you didn't request this, you can "
        "safely ignore this email. Your password won't change."
    )
    html_body = f"""
        <p>Hi {first_name},</p>
        <p>Someone requested a password reset for your Artha account.
        Click the button below to set a new password:</p>
        <p><a href="{reset_url}"
              style="display:inline-block; padding:12px 24px; background:#b8842a;
                     color:#000; text-decoration:none; border-radius:8px; font-weight:600;">
            Reset Password
        </a></p>
        <p style="color:#666; font-size:13px;">This link expires in 1 hour.
        If you didn't request this, you can safely ignore this email.
        Your password won't change.</p>
    """

    try:
        resend.Emails.send({
            "from": from_address,
            "to": user.email,
            "subject": "Reset your Artha password",
            "html": html_body,
            "text": text_body,
        })
        return True
    except Exception as e:
        log.error("Error sending password reset email to %s: %s", user.email, e, exc_info=True)
        return False
