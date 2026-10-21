from flask import Blueprint

# The native Artha app's API (the artha-apple repo). Versioned in the URL so
# an app already installed on someone's phone keeps working when a later
# version changes a response shape: /api/v2 can ship alongside this one.
api_bp = Blueprint("api", __name__, url_prefix="/api/v1")

from . import routes  # noqa: E402,F401
