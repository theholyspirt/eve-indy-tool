from apscheduler.schedulers.background import BackgroundScheduler


def sync_all_transactions(app):
    # Runs outside a request, so it needs its own app context to use the DB/session.
    with app.app_context():
        from app.models import Character
        from app.routes import (
            refresh_if_expired, _snapshot, _esi_pool,
            _fetch_raw_transactions, _fetch_raw_journal,
            _write_transactions, _write_journal,
        )

        # Sequential: token refresh is a DB write, so it stays on the main
        # thread/app-context session — matches the pattern _gather_esi uses
        # for page loads. A dead refresh token skips that one character
        # instead of aborting the whole sync cycle.
        snaps = []
        for character in Character.query.all():
            try:
                snaps.append(_snapshot(refresh_if_expired(character)))
            except Exception as e:
                print(f"[scheduler] Skipping {character.character_name}: {e}")
        if not snaps:
            return

        # Parallel: the network-bound half, and the part that determines
        # whether this finishes inside its 30-minute window. Sequential ESI
        # calls do not scale to a corp of hundreds — each character costs at
        # least two round trips (transactions + paginated journal), so 200
        # characters sequentially can take longer than the interval between
        # runs. The fetch is pure (no DB access) precisely so it can run here.
        def fetch_one(snap):
            try:
                txns = _fetch_raw_transactions(snap)
                txn_err = None
            except Exception as e:
                txns, txn_err = [], str(e)
            try:
                journal = _fetch_raw_journal(snap)
                journal_err = None
            except Exception as e:
                journal, journal_err = [], str(e)
            return snap, txns, journal, txn_err, journal_err

        results = list(_esi_pool.map(fetch_one, snaps))

        # Sequential again: writes need the request-thread's DB session, so
        # they happen back on the main thread after every fetch has returned.
        for snap, txns, journal, txn_err, journal_err in results:
            if txn_err:
                print(f"[scheduler] Error fetching transactions for {snap.character_name}: {txn_err}")
            if journal_err:
                print(f"[scheduler] Error fetching journal for {snap.character_name}: {journal_err}")
            try:
                new_count = _write_transactions(snap.character_id, txns)
                new_fees = _write_journal(snap.character_id, journal)
                print(f"[scheduler] Synced {new_count} new transactions, "
                      f"{new_fees} new fee entries for {snap.character_name}")
            except Exception as e:
                # One character's write failure shouldn't lose the rest of the batch.
                print(f"[scheduler] Error writing sync results for {snap.character_name}: {e}")


def start_scheduler(app):
    # ESI only returns the last 2,500 transactions per character, so this needs to
    # run often enough that no character can exceed that between syncs.
    # NOTE: only call this once per deployment (see RUN_SCHEDULER in app/__init__.py) —
    # running it in more than one process duplicates every sync.
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        func=sync_all_transactions,
        args=[app],
        trigger="interval",
        minutes=30,
        id="sync_transactions",
        replace_existing=True,
    )
    scheduler.start()
    return scheduler
