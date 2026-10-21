"""A sample account for Apple's App Review (`flask create-review-account`).

Apple reviews every TestFlight beta that goes to outside testers, and
every App Store release, by actually using the app. Artha only works
signed in, so the reviewer needs an account of their own: one with
realistic sample data, so Today, Activity, Plan and Wealth all have
something to show instead of empty states. Nothing in it is anyone's
real money.

Running it again wipes the account and builds it fresh with a new
password, with dates relative to today, so a reviewer always sees a live
month rather than one that ended months ago.
"""

import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from ..extensions import db
from ..models import Transaction, User
from ..models.account import Account, NetWorthSnapshot
from ..models.budget import Budget
from ..models.category_budget import CategoryBudget
from ..utils import user_today

REVIEW_USERNAME = "appreview"
REVIEW_EMAIL = "appreview@arthaapp.com"

# (days ago, description, amount, category). Spread across this month and
# last so Today's chart, Activity's history and Plan's usual spending all
# have real shape.
_EVERYDAY = [
    (0, "Blue Bottle Coffee", "6.75", "dining"),
    (1, "Whole Foods", "84.12", "groceries"),
    (2, "Uber", "18.40", "transport"),
    (3, "Chipotle", "13.85", "dining"),
    (5, "Target", "42.00", "shopping"),
    (6, "Trader Joe's", "61.30", "groceries"),
    (8, "AMC Theatres", "24.00", "entertainment"),
    (10, "Shell", "48.90", "transport"),
    (12, "Sweetgreen", "16.20", "dining"),
    (15, "Costco", "132.55", "groceries"),
    (18, "CVS Pharmacy", "21.40", "health"),
    (22, "Lyft", "14.75", "transport"),
    (26, "Whole Foods", "77.80", "groceries"),
    (31, "Nordstrom", "96.00", "shopping"),
    (35, "Trader Joe's", "58.10", "groceries"),
    (40, "Blue Bottle Coffee", "6.75", "dining"),
    (44, "Shell", "51.20", "transport"),
    (50, "Costco", "118.30", "groceries"),
    (55, "Chipotle", "12.95", "dining"),
]

_ACCOUNTS = [
    ("Everyday checking", "checking", "4210.55", 2),
    ("High-yield savings", "savings", "12500.00", 9),
    ("Brokerage", "investment", "28940.10", 40),
    ("Rewards card", "credit_card", "1284.33", 1),
    ("Car loan", "loan", "9600.00", 20),
]


def _at(day) -> datetime:
    return datetime(day.year, day.month, day.day, 12, tzinfo=timezone.utc)


def create_review_account() -> tuple[User, str]:
    """(user, password). Replaces any earlier review account entirely."""
    existing = User.query.filter_by(username=REVIEW_USERNAME).first()
    if existing is not None:
        # One row at a time so every cascade on User fires (see
        # cli.purge_expired_accounts for why a bulk delete would orphan rows).
        db.session.delete(existing)
        db.session.commit()

    password = secrets.token_urlsafe(12)
    user = User(username=REVIEW_USERNAME, email=REVIEW_EMAIL, first_name="App", last_name="Review",
                preferred_currency="USD")
    user.set_password(password)
    db.session.add(user)
    db.session.commit()

    today = user_today(user)
    first_of_month = today.replace(day=1)

    def add(day, description, amount, category, *, t_type="expense", recurring=False, interval=None):
        value = Decimal(amount)
        db.session.add(Transaction(
            user_id=user.id, description=description, amount=value, type=t_type, category=category,
            currency="USD", usd_value=value, timestamp=_at(day), import_source="manual",
            is_recurring=recurring, recurrence_interval=interval,
        ))

    for days_ago, description, amount, category in _EVERYDAY:
        add(today - timedelta(days=days_ago), description, amount, category)

    # Income and bills, this month and last.
    last_month = (first_of_month - timedelta(days=1)).replace(day=1)
    for month_start in (last_month, first_of_month):
        add(month_start, "Paycheck", "4200.00", "income", t_type="income")
        add(month_start, "Rent", "1650.00", "housing", recurring=month_start == first_of_month)
    add(today - timedelta(days=4), "Spotify", "11.99", "subscriptions", recurring=True)
    add(today - timedelta(days=3), "Gym class", "18.00", "health", recurring=True, interval="weekly")

    db.session.add(Budget(user_id=user.id, monthly_cap=Decimal("3400"), currency="USD"))
    db.session.add(CategoryBudget(user_id=user.id, category="dining", monthly_cap=Decimal("250"), currency="USD"))
    db.session.add(CategoryBudget(user_id=user.id, category="groceries", monthly_cap=Decimal("450"), currency="USD"))

    now = datetime.now(timezone.utc)
    assets = liabilities = Decimal("0")
    for name, account_type, balance, days_old in _ACCOUNTS:
        account = Account(user_id=user.id, name=name, account_type=account_type, currency="USD",
                          current_balance=Decimal(balance), balance_updated_at=now - timedelta(days=days_old))
        db.session.add(account)
        if account.is_liability:
            liabilities += account.current_balance
        else:
            assets += account.current_balance

    # Six months of net worth history, climbing toward today's figure.
    for months_back in range(6, 0, -1):
        day = today - timedelta(days=30 * months_back)
        db.session.add(NetWorthSnapshot(
            user_id=user.id, snapshot_date=day,
            total_assets_usd=assets - Decimal(1150 * months_back), total_liabilities_usd=liabilities,
        ))
    db.session.add(NetWorthSnapshot(user_id=user.id, snapshot_date=today,
                                    total_assets_usd=assets, total_liabilities_usd=liabilities))

    db.session.commit()
    return user, password
