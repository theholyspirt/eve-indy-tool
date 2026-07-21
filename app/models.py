from app import db
from app.crypto import encrypt_token, decrypt_token

# The bootstrap owner used to adopt rows that predate multi-user support, and
# the identity get_current_user() falls back to until the corp-site lookup is
# wired up. Deliberately not a real corp-site id, so it can never collide.
BOOTSTRAP_EXTERNAL_ID = "__bootstrap_local_admin__"


class EncryptedToken(db.TypeDecorator):
    # Transparent encryption at rest for OAuth tokens: encrypts on the way into
    # the database, decrypts on the way out. Application code reads and writes
    # plain strings and never knows — which means no call site can forget.
    # See app/crypto.py for key management; without a key it passes through.
    impl = db.Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return encrypt_token(value)

    def process_result_value(self, value, dialect):
        return decrypt_token(value)

# Only these two ranks exist. Anything unrecognised is treated as MEMBER —
# deny by default, so a typo or a brand-new rank on the corp website can never
# accidentally hand someone corp-wide visibility.
ROLE_MEMBER = "member"
ROLE_ADMIN = "admin"


class User(db.Model):
    # Mirrors an account on the corp website. This app never creates its own
    # users or ranks — external_id is the corp site's primary key for a person
    # and role is copied from there. get_current_user() in routes.py is the one
    # place either value is resolved from.
    id = db.Column(db.Integer, primary_key=True)
    external_id = db.Column(db.String(64), unique=True, nullable=False, index=True)
    role = db.Column(db.String(20), nullable=False, default=ROLE_MEMBER)

    @property
    def is_admin(self):
        # Exact match only. Never truthiness or "startswith" — an unknown value
        # must fall through to member-level access, not admin.
        return self.role == ROLE_ADMIN


class Character(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    # Stays GLOBALLY unique on purpose: an EVE character can be linked by one
    # person only, so nobody can claim an alt another member has already added.
    character_id = db.Column(db.Integer, unique=True, nullable=False)
    character_name = db.Column(db.String(100), nullable=False)
    access_token = db.Column(EncryptedToken, nullable=False)
    refresh_token = db.Column(EncryptedToken, nullable=False)
    # Compared against datetime.now(timezone.utc) before each ESI call to decide
    # whether to refresh; stored naive (no tz) by SQLite, so callers must attach
    # UTC back on with .replace(tzinfo=timezone.utc) before comparing.
    token_expiry = db.Column(db.DateTime, nullable=False)
    # Who linked this character. Nullable ONLY because existing rows predate the
    # column — the migration in __init__.py backfills them. Application code
    # treats a NULL owner as "belongs to nobody" (invisible to everyone except
    # admins), never as "belongs to everyone".
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True, index=True)


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
    # below min_qty renders red. Scoped PER USER: type_id used to be globally
    # unique, which with multiple members would have meant one person's
    # threshold silently applying to everyone else's inventory page.
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True, index=True)
    type_id = db.Column(db.Integer, nullable=False)
    min_qty = db.Column(db.Integer, nullable=False)
    __table_args__ = (db.UniqueConstraint("user_id", "type_id"),)


class AccessAttempt(db.Model):
    # Audit trail of blocked access — someone trying to reach data that isn't
    # theirs. In a corp context this is the genuinely useful half of the
    # feature: directors/HR want to know who went looking.
    id = db.Column(db.Integer, primary_key=True)
    # Nullable: an attempt can come from a request with no resolvable user.
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True, index=True)
    at = db.Column(db.DateTime, nullable=False)
    path = db.Column(db.String(255), nullable=False)
    # What they reached for, e.g. "character_id=98000123 owned by user 7".
    detail = db.Column(db.String(255), nullable=False)


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
