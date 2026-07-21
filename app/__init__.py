from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_session import Session
from flask_wtf import CSRFProtect
from dotenv import load_dotenv
from sqlalchemy import text, inspect
from datetime import timedelta
import os

load_dotenv()

db = SQLAlchemy()
csrf = CSRFProtect()


def create_app():
    app = Flask(__name__)

    # Fail loudly instead of silently signing sessions with None, which would
    # make login/CSRF protection meaningless.
    secret_key = os.getenv("SECRET_KEY")
    if not secret_key:
        raise RuntimeError("SECRET_KEY is not set in the environment (.env)")
    app.config["SECRET_KEY"] = secret_key
    # Falls back to local SQLite; set DATABASE_URL in production to point at Postgres.
    app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL", "sqlite:///eve_indy.db")
    app.config["SESSION_TYPE"] = "sqlalchemy"
    app.config["SESSION_SQLALCHEMY"] = db
    # Stay signed in across browser restarts — the EVE tokens live in the DB
    # anyway, so forcing a fresh SSO round-trip every browser session adds no
    # security, just friction. (Incognito windows still forget cookies.)
    app.config["SESSION_PERMANENT"] = True
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)

    # Session cookie hardening.
    #   HTTPONLY  — JS can't read the cookie (XSS can't steal sessions)
    #   SAMESITE  — browsers won't attach it to cross-site requests, first line
    #               of CSRF defence alongside the token check below
    #   SECURE    — cookie only over HTTPS. Opt-in via env because localhost
    #               dev is plain HTTP; MUST be true on any real deployment.
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = (
        os.getenv("SESSION_COOKIE_SECURE", "false").lower() == "true"
    )

    db.init_app(app)

    Session(app)
    # CSRF tokens on every state-changing request (POST/PUT/DELETE). Templates
    # get {{ csrf_token() }}; requests without a valid token are rejected, so a
    # hostile page can't submit forms on a member's behalf.
    csrf.init_app(app)

    def _migrate_transaction_unique():
        # One-time SQLite rebuild: the transaction table originally had a GLOBAL
        # unique on transaction_id, but both sides of a market trade between two
        # of your own characters share one transaction_id — uniqueness must be
        # per (character_id, transaction_id). SQLite can't alter constraints,
        # so detect the old schema and rebuild the table preserving all rows.
        if not db.engine.url.drivername.startswith("sqlite"):
            return
        with db.engine.connect() as conn:
            exists = conn.execute(text(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='transaction'"
            )).fetchone()
            if not exists:
                return
            old_schema = False
            for row in conn.execute(text("PRAGMA index_list('transaction')")).fetchall():
                # row: (seq, name, unique, origin, partial)
                if row[2]:
                    cols = [c[2] for c in conn.execute(
                        text(f"PRAGMA index_info('{row[1]}')")).fetchall()]
                    if cols == ["transaction_id"]:
                        old_schema = True
            if not old_schema:
                return
            conn.execute(text("DROP INDEX IF EXISTS idx_txn_character"))
            conn.execute(text("DROP INDEX IF EXISTS idx_txn_type"))
            conn.execute(text('ALTER TABLE "transaction" RENAME TO "transaction_old"'))
            conn.commit()
        from .models import Transaction
        Transaction.__table__.create(db.engine)
        with db.engine.connect() as conn:
            conn.execute(text('INSERT INTO "transaction" SELECT * FROM "transaction_old"'))
            conn.execute(text('DROP TABLE "transaction_old"'))
            conn.commit()

    def _migrate_add_user_ownership():
        # Multi-user upgrade. Two jobs, both idempotent:
        #   1. Add Character.user_id to databases that predate it. db.create_all()
        #      creates missing TABLES but never adds COLUMNS to existing ones, so
        #      this ALTER has to be explicit.
        #   2. Make sure every character has an owner, so the upgrade strands no
        #      data and the existing single-user setup keeps working untouched.
        inspector = inspect(db.engine)
        if "character" in inspector.get_table_names():
            columns = {c["name"] for c in inspector.get_columns("character")}
            if "user_id" not in columns:
                with db.engine.connect() as conn:
                    conn.execute(text("ALTER TABLE character ADD COLUMN user_id INTEGER"))
                    conn.commit()

        from .models import User, Character, BOOTSTRAP_EXTERNAL_ID, ROLE_ADMIN

        # The bootstrap admin adopts pre-existing rows, and is also who
        # get_current_user() resolves to until the corp-site lookup is wired up.
        owner = User.query.filter_by(external_id=BOOTSTRAP_EXTERNAL_ID).first()
        if not owner:
            owner = User(external_id=BOOTSTRAP_EXTERNAL_ID, role=ROLE_ADMIN)
            db.session.add(owner)
            db.session.commit()

        adopted = Character.query.filter_by(user_id=None).update(
            {"user_id": owner.id}, synchronize_session=False
        )
        # Commit unconditionally. The UPDATE has already executed inside a
        # transaction even when it matched zero rows, so skipping the commit
        # leaves that transaction open holding a write lock — and the next
        # statement (the CREATE INDEX below) fails with "database is locked".
        db.session.commit()
        if adopted:
            print(f"[migration] adopted {adopted} pre-existing character(s) "
                  f"into the bootstrap admin")

    def _migrate_stock_limit_per_user():
        # stock_limit originally had a GLOBAL unique on type_id. With more than
        # one member that means two people can never set a threshold on the same
        # item — the first person's limit silently applies to everyone's
        # inventory page. Uniqueness has to be per (user_id, type_id).
        # SQLite cannot alter constraints, so detect the old shape and rebuild,
        # preserving existing rows under the bootstrap admin.
        inspector = inspect(db.engine)
        if "stock_limit" not in inspector.get_table_names():
            return  # fresh install — create_all() already built the new shape
        if "user_id" in {c["name"] for c in inspector.get_columns("stock_limit")}:
            return  # already migrated

        from .models import StockLimit, User, BOOTSTRAP_EXTERNAL_ID

        owner = User.query.filter_by(external_id=BOOTSTRAP_EXTERNAL_ID).first()
        owner_id = owner.id if owner else None

        with db.engine.connect() as conn:
            rows = conn.execute(
                text("SELECT type_id, min_qty FROM stock_limit")
            ).fetchall()
            conn.execute(text("DROP TABLE stock_limit"))
            conn.commit()
        StockLimit.__table__.create(db.engine)
        if rows:
            for type_id, min_qty in rows:
                db.session.add(StockLimit(
                    user_id=owner_id, type_id=type_id, min_qty=min_qty
                ))
            db.session.commit()
            print(f"[migration] moved {len(rows)} stock limit(s) to the bootstrap admin")

    def _migrate_encrypt_tokens():
        # Upgrade pre-encryption rows in place. Reads raw values (bypassing the
        # ORM so the TypeDecorator doesn't double-process), encrypts any that
        # lack the enc$v1$ prefix. No-op without a key, and idempotent —
        # already-encrypted rows are skipped.
        from .crypto import encrypt_token, is_encrypted
        import os as _os
        if not _os.getenv("TOKEN_ENCRYPTION_KEY", "").strip():
            return
        inspector = inspect(db.engine)
        if "character" not in inspector.get_table_names():
            return
        with db.engine.connect() as conn:
            rows = conn.execute(
                text("SELECT id, access_token, refresh_token FROM character")
            ).fetchall()
            upgraded = 0
            for row_id, access, refresh in rows:
                if is_encrypted(access) and is_encrypted(refresh):
                    continue
                conn.execute(
                    text("UPDATE character SET access_token = :a, "
                         "refresh_token = :r WHERE id = :i"),
                    {
                        "a": access if is_encrypted(access) else encrypt_token(access),
                        "r": refresh if is_encrypted(refresh) else encrypt_token(refresh),
                        "i": row_id,
                    },
                )
                upgraded += 1
            conn.commit()
        if upgraded:
            print(f"[migration] encrypted tokens for {upgraded} character(s)")

    from .routes import main

    app.register_blueprint(main)
    from .auth import auth

    app.register_blueprint(auth)
    with app.app_context():
        # Imported for its side effect: importing the module registers the
        # Character/Transaction/JournalEntry/StockLimit models on db.metadata so
        # db.create_all() below actually creates their tables. Not called directly.
        from . import models  # noqa: F401

        _migrate_transaction_unique()
        db.create_all()
        # Must run AFTER create_all(), which is what creates the new `user`
        # table this migration writes the bootstrap admin into.
        _migrate_add_user_ownership()
        # ...and this one after that, since it hands existing rows to the
        # bootstrap admin that the previous migration creates.
        _migrate_stock_limit_per_user()
        _migrate_encrypt_tokens()
        # Speeds up the dashboard's per-character, per-type_id aggregate queries.
        with db.engine.connect() as conn:
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_txn_character ON 'transaction' (character_id)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_txn_type ON 'transaction' (type_id)"))
            conn.commit()

    # RUN_SCHEDULER must be set to "false" on every worker except one when running
    # multiple gunicorn workers in production — otherwise each worker starts its own
    # scheduler and the 30-minute sync job fires once per worker, multiplying ESI
    # calls and duplicating writes. Defaults to "true" so local single-process dev
    # (python -m app.run) behaves as before with no extra config.
    run_scheduler = os.getenv("RUN_SCHEDULER", "true").lower() == "true"
    # Flask's debug reloader spawns a watcher process and a child process; only the
    # child actually serves requests, so only it should start the scheduler.
    in_reloader_child = not app.debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true"
    if run_scheduler and in_reloader_child:
        from app.scheduler import start_scheduler
        start_scheduler(app)

    return app
