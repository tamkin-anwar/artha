# Import all models here so Flask-Migrate / Alembic can discover them
# when it inspects the metadata at migration time.
from .user import User
from .note import Note
from .finance import Transaction
from .exchange_rate import ExchangeRate
from .event import Event
from .feedback import Feedback

__all__ = ["User", "Note", "Transaction", "ExchangeRate", "Event", "Feedback"]
