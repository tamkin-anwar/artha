import logging
import secrets
from datetime import datetime, timedelta, timezone
from zoneinfo import available_timezones

import pyotp
import qrcode
import qrcode.image.svg
from flask import current_app, render_template, redirect, url_for, request, flash, jsonify, session
from flask_login import login_user, logout_user, login_required, current_user
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from werkzeug.security import check_password_hash, generate_password_hash

from ...extensions import db, limiter
from ...models import User, RecoveryCode
from ...services.email_service import send_password_reset_email, send_account_deletion_email
from ...utils import CURRENCY_CODES
from . import auth_bp

log = logging.getLogger(__name__)

# 1 hour — long enough that a reset email arriving a few minutes late still
# works, short enough that a stale, unused link isn't a lingering risk.
RESET_TOKEN_MAX_AGE = 3600

# Matches Notes' own TRASH_RETENTION_DAYS precedent exactly (same 30-day
# shape GDPR Article 17 and most real products converge on) rather than
# inventing a different number for a conceptually identical "give people a
# window to change their mind" mechanism.
ACCOUNT_DELETION_GRACE_DAYS = 30


def _reset_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt="password-reset")


def _generate_reset_token(user: User) -> str:
    """Signs {user_id, a fragment of the CURRENT password hash}. The hash
    fragment is what makes a token single-use with no token table: once
    the password actually changes (this reset or a later one), the
    fragment embedded in any older link stops matching and it's rejected
    — see _verify_reset_token."""
    return _reset_serializer().dumps({"uid": user.id, "pwf": user.password_hash[-16:]})


def _verify_reset_token(token: str) -> User | None:
    try:
        data = _reset_serializer().loads(token, max_age=RESET_TOKEN_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None

    user = db.session.get(User, data.get("uid"))
    if user is None or user.password_hash[-16:] != data.get("pwf"):
        return None
    return user


def _totp_qr_svg(uri: str) -> str:
    """Renders a TOTP provisioning URI as inline SVG markup — no separate
    /qrcode image route to expose or add no-cache headers to (the usual
    pattern for this, e.g. Miguel Grinberg's reference Flask/2FA writeup),
    since the markup is embedded directly in the setup page's own response
    instead of being a second, independently-fetchable resource. Uses
    qrcode's SVG image factory specifically to avoid also needing Pillow
    at runtime — this app's only other use of qrcode-adjacent imagery
    (pdfplumber's PDF parsing) already pulls Pillow in lazily and
    separately; this path doesn't need to."""
    img = qrcode.make(uri, image_factory=qrcode.image.svg.SvgPathImage)
    from io import BytesIO
    buf = BytesIO()
    img.save(buf)
    return buf.getvalue().decode("utf-8")


def _verify_totp_or_recovery_code(user: User, code: str) -> bool:
    """True if `code` is either a currently-valid TOTP value for `user`, or
    an unused recovery code (marked consumed on match). Tried in that
    order since a real login overwhelmingly presents a TOTP code, and
    checking it first avoids hashing every unused recovery code against a
    code that was never going to match one anyway."""
    code = (code or "").strip()
    if not code:
        return False

    if user.otp_secret and pyotp.TOTP(user.otp_secret).verify(code, valid_window=1):
        return True

    for rc in user.recovery_codes.filter_by(used_at=None).all():
        if check_password_hash(rc.code_hash, code):
            rc.used_at = datetime.now(timezone.utc)
            db.session.commit()
            return True
    return False


def _purge_expired_deleted_accounts() -> None:
    """Global sweep for accounts whose ACCOUNT_DELETION_GRACE_DAYS window
    has passed — not scoped to current_user the way Notes'
    _purge_expired_trash is scoped to whoever's browsing, since an account
    that's actually expired by definition isn't the one loading this page.
    Called opportunistically from login()'s GET path (the one page every
    user, deleted or not, eventually hits), mirrored by the
    purge-expired-accounts CLI command below for a deliberate daily
    Render Cron Job.

    Deliberately deletes User rows one at a time via db.session.delete(),
    never Query.delete() — a bulk delete executes as one SQL statement and
    bypasses every ORM-level cascade="all, delete-orphan" relationship
    entirely, which would silently orphan a user's transactions, notes,
    events, and everything else cascade-deletion is supposed to catch.
    Notes' own bulk delete is safe only because Note has no children to
    cascade; User now has many."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=ACCOUNT_DELETION_GRACE_DAYS)
    expired = User.query.filter(
        User.deleted_at.isnot(None), User.deleted_at < cutoff
    ).all()
    for user in expired:
        db.session.delete(user)
    if expired:
        db.session.commit()


@auth_bp.route("/privacy")
def privacy():
    """A plain-language explainer of how Artha handles a user's data,
    reachable without logging in (linked from Register, so someone can
    read it before handing over any of their own data) and from Edit
    Profile's Security section (for people who already have an account).
    No dynamic content — this is a static page kept in sync by hand
    whenever the actual data-handling behavior it describes changes."""
    return render_template("privacy.html")


@auth_bp.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        first_name = request.form.get("first_name", "").strip()
        last_name = request.form.get("last_name", "").strip()
        email = request.form.get("email", "").strip().lower()
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        if not username or not password or not email:
            flash("Email, username, and password are required.", "error")
            return redirect(url_for("auth.register"))

        # Same minimal check as edit_profile() — not a full RFC 5322
        # parser, just enough to catch "forgot the @"/"forgot the domain"
        # typos before they land in the database.
        local_part, _, domain_part = email.partition("@")
        if not local_part or "." not in domain_part or domain_part.startswith(".") or domain_part.endswith("."):
            flash("That doesn't look like a valid email address.", "error")
            return redirect(url_for("auth.register"))

        if len(password) < 8:
            flash("Password must be at least 8 characters.", "error")
            return redirect(url_for("auth.register"))

        if User.query.filter_by(username=username).first():
            flash("Username already exists.", "error")
            return redirect(url_for("auth.register"))

        if User.query.filter_by(email=email).first():
            flash("Email already exists.", "error")
            return redirect(url_for("auth.register"))

        new_user = User(
            username=username,
            email=email,
            first_name=first_name or None,
            last_name=last_name or None,
        )
        new_user.set_password(password)

        try:
            db.session.add(new_user)
            db.session.commit()
            flash("Registration successful! You can now log in.", "success")
            return redirect(url_for("auth.login"))
        except Exception as e:
            db.session.rollback()
            log.error("Error during registration: %s", e, exc_info=True)
            flash("Error during registration", "error")
            return redirect(url_for("auth.register"))

    return render_template("register.html")


@auth_bp.route("/login", methods=["GET", "POST"])
@limiter.limit("5 per minute", methods=["POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        remember = request.form.get("remember") == "on"

        user = User.query.filter_by(username=username).first()
        if user and user.check_password(password):
            # Logging back in during the grace window *is* the undo for a
            # pending account deletion — no separate token/link needed.
            # Checked before the 2FA branch below so a deletion-pending
            # account with 2FA enabled still has to clear both, not skip
            # one because of the other.
            deletion_canceled = user.deleted_at is not None
            if deletion_canceled:
                user.deleted_at = None

            if user.otp_enabled:
                db.session.commit()
                session["pending_2fa_user_id"] = user.id
                session["pending_2fa_remember"] = remember
                if deletion_canceled:
                    flash("Account deletion canceled. Finish signing in below.", "success")
                return redirect(url_for("auth.verify_2fa"))

            user.last_login_at = datetime.now(timezone.utc)
            db.session.commit()
            login_user(user, remember=remember)
            if deletion_canceled:
                flash("Account deletion canceled. Welcome back.", "success")
            return redirect(url_for("dashboard.index"))

        flash("Invalid credentials", "error")
        return redirect(url_for("auth.login"))

    # Cheap, opportunistic global sweep — see _purge_expired_deleted_accounts's
    # own docstring for why this page specifically, and why it can't just
    # mirror Notes' current-user-scoped lazy sweep.
    _purge_expired_deleted_accounts()
    return render_template("login.html")


@auth_bp.route("/login/verify-2fa", methods=["GET", "POST"])
@limiter.limit("5 per minute", methods=["POST"])
def verify_2fa():
    """The second login step for an account with 2FA enabled. Reached only
    via login()'s own redirect (never directly, and never skippable) —
    pending_2fa_user_id in the session is what proves the password step
    already passed."""
    user_id = session.get("pending_2fa_user_id")
    if user_id is None:
        return redirect(url_for("auth.login"))

    user = db.session.get(User, user_id)
    if user is None or not user.otp_enabled:
        session.pop("pending_2fa_user_id", None)
        session.pop("pending_2fa_remember", None)
        return redirect(url_for("auth.login"))

    if request.method == "POST":
        code = request.form.get("code", "")
        if _verify_totp_or_recovery_code(user, code):
            remember = session.pop("pending_2fa_remember", False)
            session.pop("pending_2fa_user_id", None)
            user.last_login_at = datetime.now(timezone.utc)
            db.session.commit()
            login_user(user, remember=remember)
            return redirect(url_for("dashboard.index"))

        flash("Invalid code.", "error")
        return redirect(url_for("auth.verify_2fa"))

    return render_template("verify_2fa.html")


@auth_bp.route("/forgot_password", methods=["GET", "POST"])
@limiter.limit("3 per hour", methods=["POST"])
def forgot_password():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        if email:
            user = User.query.filter_by(email=email).first()
            if user is not None:
                token = _generate_reset_token(user)
                reset_url = url_for("auth.reset_password", token=token, _external=True)
                send_password_reset_email(user, reset_url)

        # Identical message whether or not the email matched an account —
        # never let this form be used to check which emails are registered.
        flash("If that email has an account, a reset link is on its way.", "success")
        return redirect(url_for("auth.login"))

    return render_template("forgot_password.html")


@auth_bp.route("/reset_password/<token>", methods=["GET", "POST"])
def reset_password(token):
    user = _verify_reset_token(token)
    if user is None:
        flash("That reset link is invalid or has expired. Request a new one.", "error")
        return redirect(url_for("auth.forgot_password"))

    if request.method == "POST":
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")

        if not new_password or not confirm_password:
            flash("Both password fields are required.", "error")
            return redirect(url_for("auth.reset_password", token=token))

        if len(new_password) < 8:
            flash("New password must be at least 8 characters.", "error")
            return redirect(url_for("auth.reset_password", token=token))

        if new_password != confirm_password:
            flash("New passwords do not match.", "error")
            return redirect(url_for("auth.reset_password", token=token))

        try:
            user.set_password(new_password)
            db.session.commit()
            flash("Password reset. You can now log in with your new password.", "success")
            return redirect(url_for("auth.login"))
        except Exception as e:
            db.session.rollback()
            log.error("Error resetting password: %s", e, exc_info=True)
            flash("Error resetting password.", "error")
            return redirect(url_for("auth.reset_password", token=token))

    return render_template("reset_password.html", token=token)


@auth_bp.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("auth.login"))


@auth_bp.route("/profile", methods=["GET", "POST"])
@login_required
def edit_profile():
    """Lets a logged-in user set/update their own name and email — the one
    gap register() left: it requires an email up front for every new
    signup, but nothing before this let an existing account (including
    ones seeded or created before that requirement existed) add or fix
    one afterward.
    """
    if request.method == "POST":
        first_name = request.form.get("first_name", "").strip()
        last_name = request.form.get("last_name", "").strip()
        email = request.form.get("email", "").strip().lower()

        if not email:
            flash("Email is required.", "error")
            return redirect(url_for("auth.edit_profile"))

        # Deliberately minimal — not a full RFC 5322 parser, just enough to
        # catch "forgot the @" / "forgot the domain" typos before they land
        # in the database, same spirit as the password-length check below.
        local_part, _, domain_part = email.partition("@")
        if not local_part or "." not in domain_part or domain_part.startswith(".") or domain_part.endswith("."):
            flash("That doesn't look like a valid email address.", "error")
            return redirect(url_for("auth.edit_profile"))

        existing = User.query.filter_by(email=email).first()
        if existing and existing.id != current_user.id:
            flash("That email is already in use by another account.", "error")
            return redirect(url_for("auth.edit_profile"))

        try:
            current_user.first_name = first_name or None
            current_user.last_name = last_name or None
            current_user.email = email
            db.session.commit()
            flash("Profile updated.", "success")
            return redirect(url_for("dashboard.index"))
        except Exception as e:
            db.session.rollback()
            log.error("Error updating profile: %s", e, exc_info=True)
            flash("Error updating profile.", "error")
            return redirect(url_for("auth.edit_profile"))

    return render_template("edit_profile.html", account_deletion_grace_days=ACCOUNT_DELETION_GRACE_DAYS)


@auth_bp.route("/change_password", methods=["GET", "POST"])
@login_required
def change_password():
    if request.method == "POST":
        current_password = request.form.get("current_password", "")
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")

        if not current_password or not new_password or not confirm_password:
            flash("All password fields are required.", "error")
            return redirect(url_for("auth.change_password"))

        if not current_user.check_password(current_password):
            flash("Current password is incorrect.", "error")
            return redirect(url_for("auth.change_password"))

        if len(new_password) < 8:
            flash("New password must be at least 8 characters.", "error")
            return redirect(url_for("auth.change_password"))

        if new_password != confirm_password:
            flash("New passwords do not match.", "error")
            return redirect(url_for("auth.change_password"))

        if current_user.check_password(new_password):
            flash("New password must be different from the current password.", "error")
            return redirect(url_for("auth.change_password"))

        try:
            current_user.set_password(new_password)
            db.session.commit()
            flash("Password changed successfully.", "success")
            return redirect(url_for("dashboard.index"))
        except Exception as e:
            db.session.rollback()
            log.error("Error changing password: %s", e, exc_info=True)
            flash("Error changing password.", "error")
            return redirect(url_for("auth.change_password"))

    return render_template("change_password.html")


@auth_bp.route("/account/2fa/enable", methods=["GET", "POST"])
@login_required
def enable_2fa():
    if current_user.otp_enabled:
        flash("Two-factor authentication is already enabled.", "error")
        return redirect(url_for("auth.edit_profile"))

    if request.method == "POST":
        secret = session.get("pending_otp_secret")
        code = request.form.get("code", "")

        if not secret:
            flash("That setup session expired. Start again.", "error")
            return redirect(url_for("auth.enable_2fa"))

        if not pyotp.TOTP(secret).verify(code, valid_window=1):
            flash("That code didn't match. Check the time on your phone and try again.", "error")
            return redirect(url_for("auth.enable_2fa"))

        current_user.otp_secret = secret
        current_user.otp_enabled = True
        session.pop("pending_otp_secret", None)

        # Generated once, here, and never again — losing the phone this
        # was scanned with is exactly what these are for. Shown once on
        # the confirmation page below; only the hash is ever persisted.
        raw_codes = [secrets.token_hex(4) for _ in range(10)]
        for raw in raw_codes:
            db.session.add(RecoveryCode(user_id=current_user.id, code_hash=generate_password_hash(raw)))
        db.session.commit()

        return render_template("recovery_codes.html", codes=raw_codes)

    # A fresh secret per GET only if one isn't already pending — reloading
    # the setup page (or coming back to it) shouldn't invalidate a QR code
    # someone already scanned but hasn't confirmed yet.
    secret = session.get("pending_otp_secret")
    if not secret:
        secret = pyotp.random_base32()
        session["pending_otp_secret"] = secret

    uri = pyotp.totp.TOTP(secret).provisioning_uri(
        name=current_user.email or current_user.username, issuer_name="Artha"
    )
    return render_template("enable_2fa.html", secret=secret, qr_svg=_totp_qr_svg(uri))


@auth_bp.route("/account/2fa/disable", methods=["POST"])
@login_required
def disable_2fa():
    password = request.form.get("password", "")
    code = request.form.get("code", "")

    if not current_user.check_password(password):
        flash("Current password is incorrect.", "error")
        return redirect(url_for("auth.edit_profile"))

    if not _verify_totp_or_recovery_code(current_user, code):
        flash("Invalid code.", "error")
        return redirect(url_for("auth.edit_profile"))

    current_user.otp_enabled = False
    current_user.otp_secret = None
    RecoveryCode.query.filter_by(user_id=current_user.id).delete()
    db.session.commit()
    flash("Two-factor authentication disabled.", "success")
    return redirect(url_for("auth.edit_profile"))


@auth_bp.route("/account/delete", methods=["POST"])
@login_required
@limiter.limit("5 per minute", methods=["POST"])
def delete_account():
    password = request.form.get("password", "")
    confirm_username = request.form.get("confirm_username", "").strip()

    if not current_user.check_password(password):
        flash("Current password is incorrect.", "error")
        return redirect(url_for("auth.edit_profile"))

    if confirm_username != current_user.username:
        flash("Type your username exactly to confirm.", "error")
        return redirect(url_for("auth.edit_profile"))

    if current_user.is_admin and User.query.filter_by(is_admin=True).count() == 1:
        flash("You're the only admin. This account can't be deleted while that's true.", "error")
        return redirect(url_for("auth.edit_profile"))

    purge_date = datetime.now(timezone.utc) + timedelta(days=ACCOUNT_DELETION_GRACE_DAYS)
    purge_date_str = purge_date.strftime("%B %d, %Y")

    current_user.deleted_at = datetime.now(timezone.utc)
    db.session.commit()
    send_account_deletion_email(current_user, purge_date_str)

    logout_user()
    flash(
        f"Your account is scheduled for deletion on {purge_date_str}. "
        "Log back in before then to cancel.",
        "success",
    )
    return redirect(url_for("auth.login"))


@auth_bp.route("/set_currency", methods=["POST"])
@login_required
def set_currency():
    """Persists the account-wide currency preference (static/js/settings.js
    calls this on every change, best-effort). A brand-new device with no
    localStorage entry of its own yet reads this back to inherit the
    account's choice instead of defaulting to USD."""
    data = request.get_json(silent=True) or {}
    code = (data.get("code") or "").strip().upper()
    if code not in CURRENCY_CODES:
        return jsonify({"message": "Invalid currency"}), 400

    current_user.preferred_currency = code
    db.session.commit()
    return jsonify({"message": "Currency updated"}), 200


# Computed once at import — every IANA zone name Python's own tzdata knows,
# the same set Intl.DateTimeFormat().resolvedOptions().timeZone in the
# browser will always resolve to one of.
_VALID_TIMEZONES = available_timezones()


@auth_bp.route("/set_timezone", methods=["POST"])
@login_required
def set_timezone():
    """Records the browser's own IANA timezone against the account, so the
    server can compute "today" the same way the user's own clock does —
    see utils.user_now() for what silently goes wrong without this.
    static/js/settings.js posts here once per page load whenever the
    browser's detected zone doesn't match what's already stored, so this
    stays current if the user travels; a device just visiting doesn't
    change anything the user would notice, so no confirmation UI needed."""
    data = request.get_json(silent=True) or {}
    tz_name = (data.get("timezone") or "").strip()
    if tz_name not in _VALID_TIMEZONES:
        return jsonify({"message": "Invalid timezone"}), 400

    if current_user.timezone != tz_name:
        current_user.timezone = tz_name
        db.session.commit()
    return jsonify({"message": "Timezone updated"}), 200
