from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from flask_migrate import Migrate
from flask_wtf.csrf import CSRFProtect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

db = SQLAlchemy()
login_manager = LoginManager()
migrate = Migrate()
csrf = CSRFProtect()
# No global default_limits — applied per-route only (login for now), so
# every other endpoint is unaffected. In-memory storage: fine for this
# app's scale, but note it's per-process, so if the web service ever runs
# multiple gunicorn workers the *effective* limit is (limit x worker
# count) since each worker tracks its own counts. A shared store (Redis)
# would fix that, not worth the added infra for a handful of users.
limiter = Limiter(key_func=get_remote_address)
