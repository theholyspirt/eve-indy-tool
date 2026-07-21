from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime, timezone, timedelta

# Job IDs already pinged, so a job is never announced twice. Per-process: a
# restart forgets these, but the lookback window below keeps re-pings to jobs
# that finished within the last few minutes.
_notified_jobs = set()


def check_finished_jobs(app):
    with app.app_context():
        from app.models import Character
        from app.routes import _fetch_character_jobs, refresh_if_expired
        from app.sde import get_type_name
        from app.notify import notify

        now = datetime.now(timezone.utc)
        window_start = now - timedelta(minutes=6)
        for character in Character.query.all():
            try:
                character = refresh_if_expired(character)
                raw_jobs = _fetch_character_jobs(character)
            except Exception:
                continue
            if not isinstance(raw_jobs, list):
                continue
            for job in raw_jobs:
                if job["job_id"] in _notified_jobs:
                    continue
                end = datetime.fromisoformat(job["end_date"].replace("Z", "+00:00"))
                if window_start <= end <= now:
                    notify(
                        "Industry job finished",
                        f"{get_type_name(job['product_type_id'])} x{job['runs']} "
                        f"({character.character_name})",
                    )
                    _notified_jobs.add(job["job_id"])


def sync_all_transactions(app):
    # Runs outside a request, so it needs its own app context to use the DB/session.
    with app.app_context():
        from app.models import Character
        from app.auth import refresh_access_token
        from app.routes import sync_character_transactions, sync_character_journal

        characters = Character.query.all()
        for character in characters:
            try:
                if character.token_expiry.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
                    character = refresh_access_token(character)
                new_count = sync_character_transactions(character)
                new_fees = sync_character_journal(character)
                print(f"[scheduler] Synced {new_count} new transactions, "
                      f"{new_fees} new fee entries for {character.character_name}")
            except Exception as e:
                # One character's failure (expired refresh token, ESI outage) shouldn't
                # stop the rest of the loop from syncing.
                print(f"[scheduler] Error syncing {character.character_name}: {e}")


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
    # Tighter interval than the sync so a finished job is announced within
    # ~5 minutes; the ESI jobs response is cached, so this stays cheap.
    scheduler.add_job(
        func=check_finished_jobs,
        args=[app],
        trigger="interval",
        minutes=5,
        id="job_alerts",
        replace_existing=True,
    )
    scheduler.start()
    return scheduler
