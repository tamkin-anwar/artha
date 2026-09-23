from datetime import datetime, timezone
from decimal import Decimal

from ..extensions import db

# Fixed, code-defined set of account types -- same "plain dict, not a DB
# enum" shape as finance/routes.py's _CATEGORY_KEYWORDS. `liability` decides
# which side of net worth (assets - liabilities) a balance lands on; stored
# balances are always entered as a positive amount owed, never negative, so
# a credit card balance and a savings balance both read the same way and
# net worth is just sum(assets) - sum(liabilities), not sign-juggling.
ACCOUNT_TYPES = {
    "checking":        {"label": "Checking",        "liability": False},
    "savings":         {"label": "Savings",         "liability": False},
    "investment":      {"label": "Investment",      "liability": False},
    "real_estate":     {"label": "Real Estate",     "liability": False},
    "vehicle":         {"label": "Vehicle",         "liability": False},
    "other_asset":     {"label": "Other Asset",     "liability": False},
    "credit_card":     {"label": "Credit Card",     "liability": True},
    "loan":            {"label": "Loan",            "liability": True},
    "other_liability": {"label": "Other Liability", "liability": True},
}


class Account(db.Model):
    """
    A manually-tracked financial account (a bank account, a credit card, a
    loan, an estimated real-estate/vehicle value, ...) — the building block
    for net worth (assets minus liabilities). Artha has no live bank sync
    (manual entry + one-shot statement import only, a deliberate structural
    choice — see CLAUDE.md's competitive-ambition section), so this follows
    the same "Worth It"/manual-net-worth-tracker shape every no-bank-sync
    app in this space uses: a balance the user types in and updates
    themselves, not one pulled live from an institution.

    Deliberately NOT linked to Transaction (no account_id FK there) — this
    is a from-scratch, standalone feature. Attributing individual
    transactions to a specific account is a much bigger, separate lift
    (touching every add/edit/import form) and isn't needed for net worth
    to work: net worth only needs a balance per account, not a ledger.
    """

    __tablename__ = "account"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)

    name = db.Column(db.String(120), nullable=False)
    account_type = db.Column(db.String(20), nullable=False, default="checking")

    # Unlike Budget.currency/Scenario.currency (which silently take
    # whatever preferred_currency is at save time -- fine for a currency-
    # less "cap" or "cost" concept), an Account represents a real holding
    # that has its own intrinsic currency independent of the app's display
    # setting -- a GBP account stays a GBP account even if the user's
    # preferred_currency is USD. So this is an explicit field the user
    # picks on the form, not implicitly recaptured on every edit.
    currency = db.Column(db.String(3), nullable=False, default="USD")

    # Always entered as a positive number -- what's owed for a liability,
    # what's held for an asset. See ACCOUNT_TYPES' `liability` flag for
    # which side of net worth this lands on. Converted live via
    # exchange_rate_service.convert_amount() wherever it's aggregated with
    # other accounts, same pattern as Budget.monthly_cap/Scenario's cost
    # fields -- a balance is an ongoing, repeatedly-edited setting with no
    # single "creation moment" to lock a USD rate to the way a Transaction
    # does.
    current_balance = db.Column(db.Numeric(14, 2), nullable=False, default=Decimal("0"))

    # When current_balance was last actually saved (create or edit) -- not
    # a generic updated_at, since editing just the name shouldn't reset
    # staleness the way re-confirming the balance should. Drives the
    # "Updated X ago" / stale-balance indicator on the Accounts page, the
    # cheap, research-backed alternative to a full reminder system: a
    # visible nudge (grey past 30 days, amber past 90) is enough to keep
    # manual balances honest without new push-reminder infrastructure.
    balance_updated_at = db.Column(
        db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    @property
    def is_liability(self) -> bool:
        return ACCOUNT_TYPES.get(self.account_type, {}).get("liability", False)

    @property
    def type_label(self) -> str:
        return ACCOUNT_TYPES.get(self.account_type, {}).get("label", self.account_type)

    def __repr__(self) -> str:
        return f"<Account {self.id} {self.name!r} type={self.account_type} balance={self.current_balance}>"


class NetWorthSnapshot(db.Model):
    """
    One row per user per calendar day, upserted (never inserted twice for
    the same day) whenever an account is added, edited, or deleted --
    builds the net worth trend line over time without a cron job. Stored
    as a USD pivot (same convention as Transaction aggregates), converted
    to the user's own preferred_currency live for display -- the exact
    shape finance_totals() already uses for income/expense sums.
    """

    __tablename__ = "net_worth_snapshot"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    snapshot_date = db.Column(db.Date, nullable=False)
    total_assets_usd = db.Column(db.Numeric(14, 2), nullable=False, default=Decimal("0"))
    total_liabilities_usd = db.Column(db.Numeric(14, 2), nullable=False, default=Decimal("0"))

    __table_args__ = (
        db.UniqueConstraint("user_id", "snapshot_date", name="uq_net_worth_snapshot_user_date"),
    )

    @property
    def net_worth_usd(self) -> Decimal:
        return self.total_assets_usd - self.total_liabilities_usd

    def __repr__(self) -> str:
        return f"<NetWorthSnapshot {self.user_id} {self.snapshot_date} net={self.net_worth_usd}>"
