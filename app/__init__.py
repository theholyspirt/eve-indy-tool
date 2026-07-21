from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_session import Session
from dotenv import load_dotenv
from sqlalchemy import text
from datetime import timedelta
import os

load_dotenv()

db = SQLAlchemy()


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

    db.init_app(app)

    Session(app)

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
