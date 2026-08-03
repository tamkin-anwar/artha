import logging
from datetime import datetime, timezone

from flask import render_template, redirect, url_for, request, flash
from flask_login import login_user, logout_user, login_required, current_user

from ...extensions import db, limiter
from ...models import User
from . import auth_bp

log = logging.getLogger(__name__)


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


@auth_bp.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("auth.login"))


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
