from flask import Blueprint

accounts_bp = Blueprint("accounts", __name__, url_prefix="/accounts")

from . import routes  # noqa: E402, F401
