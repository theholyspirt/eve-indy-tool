from app import db


class Character(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    character_id = db.Column(db.Integer, unique=True, nullable=False)
    character_name = db.Column(db.String(100), nullable=False)
    access_token = db.Column(db.Text, nullable=False)
    refresh_token = db.Column(db.Text, nullable=False)
    # Compared against datetime.now(timezone.utc) before each ESI call to decide
    # whether to refresh; stored naive (no tz) by SQLite, so callers must attach
    # UTC back on with .replace(tzinfo=timezone.utc) before comparing.
    token_expiry = db.Column(db.DateTime, nullable=False)


class Transaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    character_id = db.Column(db.Integer, nullable=False)
    # When two of your own characters are the two sides of the same market
    # trade, BOTH wallets report the same transaction_id — so uniqueness must
    # be per character, or the second character's sync batch fails wholesale.
    transaction_id = db.Column(db.BigInteger, nullable=False)
    date = db.Column(db.DateTime, nullable=False)
    type_id = db.Column(db.Integer, nullable=False)
    quantity = db.Column(db.Integer, nullable=False)
    unit_price = db.Column(db.Float, nullable=False)
    is_buy = db.Column(db.Boolean, nullable=False)
    __table_args__ = (db.UniqueConstraint("character_id", "transaction_id"),)


class StockLimit(db.Model):
    # Low-stock threshold per item type for the inventory page — quantity
    # below min_qty renders red. One limit per item across the whole
    # operation, since inventory aggregates all characters.
    id = db.Column(db.Integer, primary_key=True)
    type_id = db.Column(db.Integer, unique=True, nullable=False)
    min_qty = db.Column(db.Integer, nullable=False)


class JournalEntry(db.Model):
    # Wallet journal rows for taxes/fees only (see FEE_REF_TYPES in routes.py) —
    # market trades themselves live in Transaction, not here. ESI keeps only
    # ~30 days of journal history, so regular syncing is what accumulates a
    # complete record over time.
    id = db.Column(db.Integer, primary_key=True)
    character_id = db.Column(db.Integer, nullable=False)
    journal_id = db.Column(db.BigInteger, nullable=False)
    date = db.Column(db.DateTime, nullable=False)
    ref_type = db.Column(db.String(50), nullable=False)
    # Negative in ESI = ISK paid out; stored as-is, negated for display.
    amount = db.Column(db.Float, nullable=False)
    # Journal ids are unique per wallet, so the dedupe key includes the character.
    __table_args__ = (db.UniqueConstraint("character_id", "journal_id"),)
