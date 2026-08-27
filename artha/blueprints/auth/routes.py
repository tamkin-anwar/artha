import logging
from datetime import datetime, timezone
from zoneinfo import available_timezones

from flask import current_app, render_template, redirect, url_for, request, flash, jsonify
from flask_login import login_user, logout_user, login_required, current_user
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from ...extensions import db, limiter
from ...models import User
from ...services.email_service import send_password_reset_email
from . import auth_bp

log = logging.getLogger(__name__)

# 1 hour — long enough that a reset email arriving a few minutes late still
# works, short enough that a stale, unused link isn't a lingering risk.
RESET_TOKEN_MAX_AGE = 3600


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
            user.last_login_at = datetime.now(timezone.utc)
            db.session.commit()
            login_user(user, remember=remember)
            return redirect(url_for("dashboard.index"))

        flash("Invalid credentials", "error")
        return redirect(url_for("auth.login"))

    return render_template("login.html")


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

    return render_template("edit_profile.html")


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


# Matches CURRENCY_PRESETS in static/js/currency.js — the closed set of
# codes the currency selector actually offers.
CURRENCY_CODES = {"USD", "GBP", "EUR", "BDT", "CAD", "AUD"}


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
