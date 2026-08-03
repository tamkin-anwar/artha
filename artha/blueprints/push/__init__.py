from flask import Blueprint

push_bp = Blueprint("push", __name__, url_prefix="/push")

from . import routes  # noqa: E402, F401
