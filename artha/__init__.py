import logging
import os

import click
from flask import Flask, render_template, redirect, url_for, request, flash, jsonify, send_from_directory
from flask_wtf.csrf import generate_csrf, CSRFError
from werkzeug.middleware.proxy_fix import ProxyFix

from .config import config, ROOT_DIR
from .extensions import db, login_manager, migrate, csrf, limiter

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


def create_app(config_name: str = "default") -> Flask:
    """
    Application factory.

    Usage:
        app = create_app()              # development
        app = create_app("production")  # production
    """
    app = Flask(
        __name__,
        # Keep templates/ and static/ at the repo root — no need to move them.
        template_folder=os.path.join(ROOT_DIR, "templates"),
        static_folder=os.path.join(ROOT_DIR, "static"),
    )

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------
    app.config.from_object(config[config_name])

    # Guard: fail loudly if SECRET_KEY is missing in production
    if config_name == "production" and app.config["SECRET_KEY"] == "dev-only-change-me":
        raise RuntimeError("SECRET_KEY environment variable is required in production.")

    # Softer guard for VAPID: push notifications degrade gracefully (the
    # subscribe button just won't work) rather than being essential to the
    # app loading at all, so this warns instead of refusing to start.
    if config_name == "production" and app.config["VAPID_PRIVATE_KEY"] == "308EL19hh8MOodozDcYdBquW4xOo0dIPGM1y28bvwMc":
        log.warning(
            "VAPID_PRIVATE_KEY/VAPID_PUBLIC_KEY are not set. Every deployed "
            "environment would share the same dev keypair. Push notifications "
            "will work but generate real VAPID keys for production."
        )

    # ------------------------------------------------------------------
    # Middleware
    # ------------------------------------------------------------------
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)

    # ------------------------------------------------------------------
    # Extensions
    # ------------------------------------------------------------------
    db.init_app(app)
    login_manager.init_app(app)
    migrate.init_app(app, db)
    csrf.init_app(app)
    limiter.init_app(app)

    login_manager.login_view = "auth.login"
    login_manager.session_protection = "strong"

    # ------------------------------------------------------------------
    # Models — must be imported so Flask-Migrate sees them
    # ------------------------------------------------------------------
    from .models import User, Note, Transaction, Feedback, Budget, PushSubscription  # noqa: F401
    from .models.scenario import Scenario  # noqa: F401

    @login_manager.user_loader
    def load_user(user_id: str):
        return db.session.get(User, int(user_id))

    # ------------------------------------------------------------------
    # Blueprints
    # ------------------------------------------------------------------
    from .blueprints.auth import auth_bp
    from .blueprints.dashboard import dashboard_bp
    from .blueprints.notes import notes_bp
    from .blueprints.finance import finance_bp
    from .blueprints.ai import ai_bp
    from .blueprints.scenarios import scenarios_bp
    from .blueprints.feedback import feedback_bp
    from .blueprints.admin import admin_bp
    from .blueprints.push import push_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(notes_bp)
    app.register_blueprint(finance_bp)
    app.register_blueprint(ai_bp)
    app.register_blueprint(scenarios_bp)
    app.register_blueprint(feedback_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(push_bp)

    # ------------------------------------------------------------------
    # Service worker — served from the site root (not /static/...) so its
    # default scope is "/", covering real app pages instead of just
    # /static/*. A script served under /static/ can only be granted a
    # wider scope via a Service-Worker-Allowed response header; serving it
    # from root sidesteps that entirely, which is the standard approach.
    # ------------------------------------------------------------------
    @app.route("/service-worker.js")
    def service_worker():
        response = send_from_directory(app.static_folder, "service-worker.js")
        response.headers["Cache-Control"] = "no-cache"
        return response

    # ------------------------------------------------------------------
    # Context processors
    # ------------------------------------------------------------------
    @app.context_processor
    def inject_csrf_token():
        # Do not name this key "csrf_token" — Flask-WTF provides that name
        # already; overriding it breaks WTForms template helpers.
        return {"csrf_token_value": generate_csrf()}

    # ------------------------------------------------------------------
    # Security headers (applied to every response)
    # ------------------------------------------------------------------
    @app.after_request
    def add_security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        return response

    # ------------------------------------------------------------------
    # Error handlers
    # ------------------------------------------------------------------
    from .utils import is_ajax_request

    @app.errorhandler(CSRFError)
    def handle_csrf_error(e):
        log.info("CSRF error: %s", getattr(e, "description", ""))
        if is_ajax_request():
            return jsonify({"message": "CSRF token missing or invalid."}), 400
        flash("Security check failed. Please refresh and try again.", "error")
        return redirect(request.referrer or url_for("dashboard.index"))

    @app.errorhandler(429)
    def handle_rate_limit(e):
        log.info("Rate limit hit: %s", request.path)
        if is_ajax_request():
            return jsonify({"message": "Too many attempts. Please wait a moment and try again."}), 429
        flash("Too many attempts. Please wait a moment and try again.", "error")
        return redirect(request.referrer or url_for("auth.login"))

    @login_manager.unauthorized_handler
    def unauthorized():
        if is_ajax_request():
            return jsonify({"message": "Authentication required."}), 401
        return redirect(url_for("auth.login"))

    @app.errorhandler(404)
    def page_not_found(e):
        log.warning("404 at %s", request.path)
        return render_template("404.html"), 404

    @app.errorhandler(500)
    def internal_error(e):
        log.error("500 at %s: %s", request.path, e, exc_info=True)
        db.session.rollback()
        return render_template("500.html"), 500

    # ------------------------------------------------------------------
    # CLI commands
    # ------------------------------------------------------------------
    @app.cli.command("make-admin")
    @click.argument("username")
    def make_admin(username):
        """Grant a user admin access: flask make-admin <username>."""
        from .models import User

        user = User.query.filter_by(username=username).first()
        if user is None:
            click.echo(f"No user found with username {username!r}.")
            return
        user.is_admin = True
        db.session.commit()
        click.echo(f"{username} is now an admin.")

    from .cli import register_cli

    register_cli(app)

    return app
