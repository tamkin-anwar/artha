from flask import Blueprint

feedback_bp = Blueprint("feedback", __name__)

from . import routes  # noqa: E402, F401
