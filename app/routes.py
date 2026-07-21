from flask import Blueprint, render_template, session, redirect, url_for, request, abort
from app.models import Character, Transaction, JournalEntry, StockLimit, AccessAttempt
from app import db
from collections import defaultdict
from app.sde import (
    get_type_name,
    get_type_names,
    get_item_categories,
    get_blueprint_materials,
    get_blueprint_products,
    get_industry_input_ids,
    get_industry_output_ids,
    search_type_ids,
)
from sqlalchemy import func
import requests
import time
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from app.auth import refresh_access_token, TokenRefreshError
from datetime import datetime, timezone, timedelta

# CCP requires a descriptive User-Agent on ESI calls; unidentified clients
# (python-requests default UA) making concurrent bursts get tarpitted —
# connections deliberately stalled ~30s. Set ESI_USER_AGENT in .env with a
# contact email per CCP guidelines; the default at least identifies the app.
ESI_USER_AGENT = os.getenv("ESI_USER_AGENT", "eve-indy-toolbox/1.0 (personal industry tool)")

# ONE process-lifetime worker pool + one Session per worker thread. ESI's
# gateway also tarpits bursts of NEW TLS connections (measured: batch 1 fast,
# batch 2 from fresh threads stalled 30s) — so connections must be long-lived
# and capped, never opened per call. All parallel ESI work goes through this
# pool. Never submit pool work from inside a pool task: with every worker
# waiting on a nested submission the pool deadlocks.
_esi_pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="esi")
_esi_tls = threading.local()


def _esi_session():
    if not hasattr(_esi_tls, "session"):
        _esi_tls.session = requests.Session()
    return _esi_tls.session

# Categories that are industry inputs by nature. Decryptors and Ancient Relics
# are OPTIONAL invention inputs, so they never appear in industryActivityMaterials
# and must be caught by category. The rest are belt-and-braces alongside the
# input-based check in get_industry_input_ids (which catches T1 items, salvage,
# datacores, etc. that live in otherwise non-industry categories).
INDUSTRY_CATEGORIES = {
    "Material",
    "Planetary Commodities",
    "Planetary Resources",
    "Reaction",
    "Commodity",
    "Decryptors",
    "Ancient Relics",
}

# Wallet journal ref_types that are industry costs. Order here is the display
# order in the dashboard taxes box. Contract courier fees are deliberately
# absent for now — contracts between your own alts are internal transfers,
# and telling those apart from real hauling costs needs contract tracking.
FEE_LABELS = {
    "transaction_tax": "Sales Tax",
    "brokers_fee": "Broker Fees",
    "manufacturing": "Job Install Costs",
    "industry_job_tax": "Facility Tax",
    "reaction": "Reaction Costs",
    "planetary_import_tax": "PI Import Tax",
    "planetary_export_tax": "PI Export Tax",
}
FEE_REF_TYPES = set(FEE_LABELS)

main = Blueprint("main", __name__)

# CCP-published average prices for every market type, refreshed every ~23h on
# their side. One unauthenticated call covers the whole game — much cheaper
# than per-item Jita order queries, at the cost of being an average rather
# than live Jita sell. Good enough for "what is my inventory roughly worth".
_market_prices = {"data": {}, "expires": 0}


def get_market_prices():
    now = time.time()
    if now < _market_prices["expires"]:
        return _market_prices["data"]
    r = esi_get(_esi_session(), "https://esi.evetech.net/latest/markets/prices/", timeout=30)
    data = r.json()
    if isinstance(data, list):
        _market_prices["data"] = {
            p["type_id"]: p["average_price"] for p in data if p.get("average_price")
        }
        _market_prices["expires"] = now + 1800
    return _market_prices["data"]


# Live order-book prices at major trade hubs, one ESI call per type (paginated),
# cached 15 min per type per hub. Different from get_market_prices: these are
# REAL current lowest-sell/highest-buy at the hub, not CCP averages.
# station: 0 means no location filter (use entire region); >0 is a specific station/structure
MARKET_HUBS = {
    "jita": {"region": 10000002, "station": 60003760, "name": "Jita"},
    "amarr": {"region": 10000042, "station": 60008494, "name": "Amarr"},
    "c-n4od": {"region": 10000069, "station": 0, "name": "C-N4OD (Fountain)"},
}
_market_cache = {}


def get_hub_prices(type_ids, hub="jita"):
    if hub not in MARKET_HUBS:
        hub = "jita"
    hub_info = MARKET_HUBS[hub]
    region = hub_info["region"]
    station = hub_info["station"]
    cache_key = (hub, "cache")

    now = time.time()
    out = {}
    missing = []
    hub_cache = _market_cache.setdefault(cache_key, {})

    for tid in set(type_ids):
        cached = hub_cache.get(tid)
        if cached and now < cached[1]:
            out[tid] = cached[0]
        else:
            missing.append(tid)

    if missing:
        def fetch_type(tid):
            s = _esi_session()
            orders = []
            page = 1
            while True:
                r = esi_get(
                    s,
                    f"https://esi.evetech.net/latest/markets/{region}/orders/",
                    params={"type_id": tid, "page": page, "order_type": "all"},
                    timeout=30,
                )
                data = r.json()
                if not isinstance(data, list):
                    break
                orders.extend(data)
                if page >= int(r.headers.get("X-Pages", 1)):
                    break
                page += 1
            if station > 0:
                in_station = [o for o in orders if o["location_id"] == station]
            else:
                in_station = orders
            sells = [o["price"] for o in in_station if not o["is_buy_order"]]
            buys = [o["price"] for o in in_station if o["is_buy_order"]]
            return tid, {
                "sell": min(sells) if sells else None,
                "buy": max(buys) if buys else None,
            }

        for tid, price in _esi_pool.map(fetch_type, missing):
            hub_cache[tid] = (price, now + 900)
            out[tid] = price
    return out


def get_jita_prices(type_ids):
    return get_hub_prices(type_ids, "jita")


def compute_build_plan(mats, me, runs, inv, prices, product_id, qty_per_run):
    # Pure math, no I/O — deliberately, so it can be tested with fixed inputs.
    # Per material: EVE's exact need at this ME, what's on hand across all
    # alts, what's missing, and what the missing part costs at Jita sell.
    rows = []
    material_cost = 0.0
    shopping_cost = 0.0
    missing_prices = False
    for m in mats:
        if m["qty"] <= 0:
            continue
        need = max(runs, (runs * m["qty"] * (100 - me) + 99) // 100)
        have = inv.get(m["material_id"], 0)
        missing = max(0, need - have)
        sell = prices.get(m["material_id"], {}).get("sell")
        if sell is None:
            missing_prices = True
            sell = 0
        rows.append({
            "material_id": m["material_id"],
            "need": need,
            "have": have,
            "missing": missing,
            "price": sell,
            "cost": need * sell,
            "shopping_cost": missing * sell,
        })
        material_cost += need * sell
        shopping_cost += missing * sell

    output_qty = runs * qty_per_run
    product_sell = prices.get(product_id, {}).get("sell")
    if product_sell is None:
        missing_prices = True
        product_sell = 0
    revenue = output_qty * product_sell
    profit = revenue - material_cost
    return {
        "rows": rows,
        "material_cost": material_cost,
        "shopping_cost": shopping_cost,
        "output_qty": output_qty,
        "product_sell": product_sell,
        "revenue": revenue,
        "profit": profit,
        "profit_per_run": profit / runs,
        "margin": (profit / material_cost * 100) if material_cost else None,
        "missing_prices": missing_prices,
    }


# Simple in-memory ESI cache: {(character_id, key): (data, expires_at)}
# NOTE: per-process, like the SDE caches in sde.py — see the note there. Also
# means results are only ever as fresh as the last fetch on THIS worker; fine
# for one worker, inconsistent across several without a shared cache.
_esi_cache = {}


def esi_cached(character_id, key, fetch_fn, ttl=300):
    cache_key = (character_id, key)
    now = time.time()
    if cache_key in _esi_cache:
        data, expires = _esi_cache[cache_key]
        if now < expires:
            return data
    data = fetch_fn()
    _esi_cache[cache_key] = (data, now + ttl)
    return data


def esi_get(s, url, **kwargs):
    # Retries on network errors, ESI rate-limiting (420), and ESI-side 5xx errors —
    # all conditions where trying again after a short wait can succeed. Anything
    # else (4xx like bad auth) fails immediately since retrying won't help.
    # Single choke point for every ESI GET, so the User-Agent is set here once.
    kwargs["headers"] = {"User-Agent": ESI_USER_AGENT, **kwargs.get("headers", {})}
    last_error = None
    for attempt in range(3):
        try:
            response = s.get(url, **kwargs)
        except requests.exceptions.RequestException as e:
            last_error = e
        else:
            if response.status_code == 420 or response.status_code >= 500:
                last_error = f"HTTP {response.status_code}"
            else:
                return response
        if attempt < 2:
            time.sleep(2)
    raise Exception(f"ESI request failed after 3 attempts: {url} ({last_error})")


def fetch_esi_pages(character, endpoint):
    # ESI paginates at 1000 items/page; X-Pages on the first response tells us
    # how many more pages to fetch. Uses the calling thread's persistent
    # session — reusing its TLS connection across pages AND across calls.
    headers = {"Authorization": f"Bearer {character.access_token}"}
    results = []
    s = _esi_session()
    first = esi_get(
        s,
        f"https://esi.evetech.net/latest/characters/{character.character_id}/{endpoint}",
        headers=headers,
        params={"page": 1},
        timeout=30,
    )
    data = first.json()
    if not isinstance(data, list):
        return results
    results.extend(data)
    total_pages = int(first.headers.get("X-Pages", 1))
    for page in range(2, total_pages + 1):
        response = esi_get(
            s,
            f"https://esi.evetech.net/latest/characters/{character.character_id}/{endpoint}",
            headers=headers,
            params={"page": page},
            timeout=30,
        )
        data = response.json()
        if isinstance(data, list):
            results.extend(data)
    return results


def get_current_user():
    """Resolve this request to a User row, or None if not signed in.

    THE single point where corp-website identity enters this application. Every
    ownership decision downstream depends on it, so it stays one function with
    one job — no route may work out "who is this" for itself.

    The host site supplies identity via app/integration.py. Nothing here needs
    editing to integrate; see that file.

    local mode -> the bootstrap admin (standalone dev on a laptop)
    corp mode  -> integration.resolve_current_identity(), mirrored into a local
                  User row so linked EVE characters have an owner to hang off

    Returning None means "signed out", and every data query is scoped by the
    resolved user — so a missing or broken integration yields empty pages, not
    somebody else's data.
    """
    from app.models import User, BOOTSTRAP_EXTERNAL_ID, ROLE_ADMIN, ROLE_MEMBER
    from app import integration

    if integration.integration_mode() != "corp":
        return User.query.filter_by(external_id=BOOTSTRAP_EXTERNAL_ID).first()

    identity = integration.resolve_current_identity()
    if not identity or not identity.get("external_id"):
        return None

    external_id = str(identity["external_id"])
    role = ROLE_ADMIN if integration.role_is_admin(identity.get("role")) else ROLE_MEMBER

    # Mirror the host site's user locally. The corp site stays the source of
    # truth — the local row exists only to own linked characters, and the rank
    # is refreshed on every request so a demotion there takes effect here
    # immediately rather than at next login.
    user = User.query.filter_by(external_id=external_id).first()
    if user is None:
        user = User(external_id=external_id, role=role)
        db.session.add(user)
        db.session.commit()
    elif user.role != role:
        user.role = role
        db.session.commit()
    return user


def owned_characters(user=None):
    """Every Character the current user is allowed to see.

    THE only way a route may reach Character rows. Routes must never call
    Character.query themselves — that is exactly how a page ends up leaking the
    whole corp.

        member -> their own linked alts
        admin  -> everyone (directors / HR / CEO)

    DENY BY DEFAULT: with no signed-in user this returns an EMPTY list, never
    the full table. So a route that forgets to scope shows nothing — a visible
    bug — instead of everyone's data, which is a silent breach.
    """
    if user is None:
        user = get_current_user()
    if user is None:
        return []
    if user.is_admin:
        return Character.query.all()
    return Character.query.filter_by(user_id=user.id).all()


def owned_character_ids(user=None):
    # EVE character_ids (not User.id) — Transaction and JournalEntry key off
    # character_id, so this is what scopes their queries. An empty list makes
    # `.in_([])` match no rows, which is the correct deny-by-default outcome.
    return [c.character_id for c in owned_characters(user)]


def deny_as_spy(detail):
    """Block an attempt to reach data belonging to someone else.

    Records the attempt before blocking — in a corp the audit trail is the
    genuinely valuable half: leadership wants to know who went looking. Then
    raises 403, which renders the spy page.
    """
    user = get_current_user()
    try:
        db.session.add(AccessAttempt(
            user_id=user.id if user else None,
            at=datetime.now(timezone.utc),
            path=request.path,
            detail=detail[:255],
        ))
        db.session.commit()
    except Exception:
        # Failing to write the audit row must never prevent the block itself.
        db.session.rollback()
    abort(403)


@main.app_errorhandler(403)
def forbidden(_error):
    return render_template("spy.html"), 403


@main.app_context_processor
def inject_current_user():
    # Lets every template ask {{ current_user }} — e.g. base.html only shows
    # the admin nav link to admins. Backed by the same single identity seam.
    return {"current_user": get_current_user()}


@main.route("/admin/access-log")
def admin_access_log():
    # Leadership's view of blocked snooping attempts. Admin-only — a member
    # opening it by URL is itself logged and blocked.
    user = get_current_user()
    if user is None:
        return redirect(url_for("main.index"))
    if not user.is_admin:
        deny_as_spy("opened the admin access log without being an admin")

    attempts = (AccessAttempt.query
                .order_by(AccessAttempt.at.desc())
                .limit(200).all())
    from app.models import User as UserModel
    names = {u.id: u.external_id for u in UserModel.query.all()}
    rows = [{
        "at": a.at,
        "who": names.get(a.user_id, "(not signed in)"),
        "path": a.path,
        "detail": a.detail,
    } for a in attempts]
    return render_template("access-log.html", rows=rows)


def get_character():
    # Every route that needs a logged-in character goes through here, so token
    # refresh happens automatically before any ESI call needs a valid token.
    if "character_id" not in session:
        return None
    character = Character.query.filter_by(character_id=session["character_id"]).first()
    if not character:
        session.clear()
        return None
    # The session's character must actually belong to the signed-in user. A
    # stale session (or a tampered cookie) must not hand back somebody else's
    # character row — that row carries their ESI access token.
    _user = get_current_user()
    if not _user or character.user_id != _user.id:
        session.clear()
        return None
    if character.token_expiry.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
        # A rejected refresh token is permanent — this session can never work
        # again, so log them out rather than 500ing every page. Transient
        # failures (SSO/network down, bad JSON, DB errors) deliberately
        # propagate: silently logging someone out would hide the real fault
        # and look like the app randomly forgetting them.
        try:
            character = refresh_access_token(character)
        except TokenRefreshError:
            session.clear()
            return None
    return character


def sync_character_transactions(character):
    # ESI only returns the most recent 2,500 transactions with no further pagination,
    # so this can silently miss older ones if it isn't run often enough (see scheduler.py).
    headers = {"Authorization": f"Bearer {character.access_token}"}
    response = esi_get(
        _esi_session(),
        f"https://esi.evetech.net/latest/characters/{character.character_id}/wallet/transactions/",
        headers=headers,
        timeout=30,
    )
    transactions = response.json()

    if not isinstance(transactions, list):
        return 0

    # Dedupe against what's already stored rather than relying solely on the
    # DB unique constraint, so a partial batch failure doesn't roll back everything.
    existing_ids = {
        row[0] for row in db.session.query(Transaction.transaction_id)
        .filter_by(character_id=character.character_id).all()
    }

    new_count = 0
    for t in transactions:
        if t["transaction_id"] in existing_ids:
            continue
        db.session.add(Transaction(
            character_id=character.character_id,
            transaction_id=t["transaction_id"],
            date=datetime.fromisoformat(t["date"].replace("Z", "+00:00")),
            type_id=t["type_id"],
            quantity=t["quantity"],
            unit_price=t["unit_price"],
            is_buy=t["is_buy"],
        ))
        new_count += 1

    db.session.commit()
    return new_count


def sync_character_journal(character):
    # Wallet journal is paginated (unlike transactions) and only covers ~30 days.
    # Only fee/tax rows are stored — see FEE_REF_TYPES.
    entries = fetch_esi_pages(character, "wallet/journal/")

    existing_ids = {
        row[0] for row in db.session.query(JournalEntry.journal_id)
        .filter_by(character_id=character.character_id).all()
    }

    new_count = 0
    for e in entries:
        if e.get("ref_type") not in FEE_REF_TYPES:
            continue
        if e["id"] in existing_ids:
            continue
        amount = e.get("amount")
        if amount is None:
            continue
        db.session.add(JournalEntry(
            character_id=character.character_id,
            journal_id=e["id"],
            date=datetime.fromisoformat(e["date"].replace("Z", "+00:00")),
            ref_type=e["ref_type"],
            amount=amount,
        ))
        new_count += 1

    db.session.commit()
    return new_count


# Characters whose token refresh recently failed get a 10-minute backoff —
# without it, one alt with a dead refresh token adds a doomed EVE SSO
# round-trip to EVERY page load.
_refresh_failed_until = {}


def refresh_if_expired(character):
    if time.time() < _refresh_failed_until.get(character.character_id, 0):
        raise RuntimeError("token refresh recently failed; backing off")
    if character.token_expiry.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
        try:
            character = refresh_access_token(character)
        except Exception:
            _refresh_failed_until[character.character_id] = time.time() + 600
            raise
    return character


def _snapshot(character):
    # Plain-object copy of what ESI fetches need. Worker threads must not
    # touch SQLAlchemy model instances: any commit expires them, and a lazy
    # reload from a thread without an app context blows up.
    return SimpleNamespace(
        character_id=character.character_id,
        character_name=character.character_name,
        access_token=character.access_token,
    )


def _selected_characters():
    # Pages default to everything the user may see; ?character=<id> narrows to
    # one. Returns (chars for the dropdown, chars to fetch, raw selection
    # string for re-rendering the filter).
    all_chars = owned_characters()
    sel = request.args.get("character", "")
    if sel.isdigit():
        fetch_chars = [c for c in all_chars if c.character_id == int(sel)]
        # Asking for a character that isn't yours is a deliberate probe — the
        # id had to be typed into the URL. Block and log rather than quietly
        # rendering an empty page, which would leak whether the id exists.
        if not fetch_chars:
            deny_as_spy(f"requested character_id={sel} which they do not own")
    else:
        fetch_chars = all_chars
    return all_chars, fetch_chars, sel


def _gather_esi(fetch_chars, key, endpoint, ttl):
    # Merge one ESI endpoint across characters. A dead alt token (or ESI
    # hiccup for one character) skips that alt instead of 500ing the page.
    # Token refresh runs sequentially (it writes to the DB session); the
    # actual fetches run in parallel — ESI latency dominates page time, and
    # N alts one-after-another multiplies it for no reason.
    snaps = []
    for ch in fetch_chars:
        try:
            snaps.append(_snapshot(refresh_if_expired(ch)))
        except Exception:
            continue
    if not snaps:
        return []

    def fetch_one(snap):
        try:
            return esi_cached(
                snap.character_id, key,
                lambda: fetch_esi_pages(snap, endpoint),
                ttl=ttl,
            )
        except Exception:
            return []

    merged = []
    for result in _esi_pool.map(fetch_one, snaps):
        if isinstance(result, list):
            merged.extend(result)
    return merged


def _fetch_character_jobs(character):
    def fetch():
        # No include_completed param: active-only is already ESI's default,
        # and requests would serialize Python False as "False", which ESI's
        # validator can reject — silently emptying the jobs list.
        r = esi_get(
            _esi_session(),
            f"https://esi.evetech.net/latest/characters/{character.character_id}/industry/jobs/",
            headers={"Authorization": f"Bearer {character.access_token}"},
            timeout=30,
        )
        return r.json()
    return esi_cached(character.character_id, "jobs", fetch, ttl=300)


@main.route("/")
def index():
    character = get_character()
    jobs = []
    total_spent = 0
    total_earned = 0
    top_buys = []
    top_sells = []
    tax_breakdown = []
    total_taxes = 0
    spent_with_taxes = 0
    alt_rows = []

    # Date filtering: ?period=7d|30d|90d|all (default: all)
    period = request.args.get("period", "all")
    period_days = {"7d": 7, "30d": 30, "90d": 90, "all": None}.get(period, None)
    if period_days:
        cutoff_date = datetime.now(timezone.utc) - timedelta(days=period_days)
    else:
        cutoff_date = None
    period_label = {"7d": "Last 7 days", "30d": "Last 30 days", "90d": "Last 90 days", "all": "All time"}.get(period, "All time")

    if character:
        # The dashboard aggregates over every character THIS USER may see — a
        # member's own alts (buyer alt + indy main + PI alt roll into one
        # operation), or the whole corp for an admin.
        characters = owned_characters()
        scoped_char_ids = [ch.character_id for ch in characters]

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        jobs_by_char = {ch.character_id: 0 for ch in characters}
        # Sequential token refresh (DB writes), parallel job fetches (network) —
        # one alt with a dead refresh token shouldn't blank the dashboard, and
        # N alts shouldn't take N times the ESI latency.
        snaps = []
        for ch in characters:
            try:
                snaps.append(_snapshot(refresh_if_expired(ch)))
            except Exception:
                continue

        def fetch_jobs_for(snap):
            try:
                return snap, _fetch_character_jobs(snap)
            except Exception:
                return snap, None

        results = list(_esi_pool.map(fetch_jobs_for, snaps)) if snaps else []

        for snap, raw_jobs in results:
            if not isinstance(raw_jobs, list):
                continue
            jobs_by_char[snap.character_id] = len(raw_jobs)
            # ESI's own job "status" field can lag reality, so status is derived
            # by comparing end_date to now instead of trusting it directly.
            # String comparison works because both are same-format ISO8601 UTC.
            for job in raw_jobs:
                jobs.append({
                    "character": snap.character_name,
                    "product": get_type_name(job["product_type_id"]),
                    "runs": job["runs"],
                    "status": "Finished" if job["end_date"] < now else "Building",
                    "end_date": job["end_date"],
                })

        # Trades where two of YOUR characters were both sides (same
        # transaction_id in two wallets) are internal transfers — no ISK left
        # the operation, so both sides are excluded from every total. The
        # taxes/fees on them were still really paid and stay in the journal.
        internal_ids = [
            row[0] for row in db.session.query(Transaction.transaction_id)
            .filter(Transaction.character_id.in_(scoped_char_ids))
            .group_by(Transaction.transaction_id)
            .having(func.count(func.distinct(Transaction.character_id)) > 1)
            .all()
        ]

        # SQL aggregates grouped per character AND per item, so one pass feeds
        # both the combined totals and the per-alt breakdown.
        # .in_(scoped_char_ids) is the ownership boundary for all money figures.
        # An empty list matches no rows, so a user with no characters sees zeros
        # rather than the corp's totals.
        buy_q = db.session.query(
            Transaction.character_id,
            Transaction.type_id,
            func.sum(Transaction.unit_price * Transaction.quantity).label("total"),
        ).filter_by(is_buy=True).filter(Transaction.character_id.in_(scoped_char_ids))
        sell_q = db.session.query(
            Transaction.character_id,
            Transaction.type_id,
            func.sum(Transaction.unit_price * Transaction.quantity).label("total"),
        ).filter_by(is_buy=False).filter(Transaction.character_id.in_(scoped_char_ids))

        if cutoff_date:
            buy_q = buy_q.filter(Transaction.date >= cutoff_date)
            sell_q = sell_q.filter(Transaction.date >= cutoff_date)

        if internal_ids:
            buy_q = buy_q.filter(Transaction.transaction_id.notin_(internal_ids))
            sell_q = sell_q.filter(Transaction.transaction_id.notin_(internal_ids))

        buy_rows = buy_q.group_by(Transaction.character_id, Transaction.type_id).all()
        sell_rows = sell_q.group_by(Transaction.character_id, Transaction.type_id).all()

        # Total Spent is scoped to industry inputs only — excludes ships, modules,
        # etc. bought for reasons unrelated to manufacturing. An item counts if it
        # is an input to any industrial activity (catches T1 items for T2 builds,
        # salvage, datacores) OR its category is inherently industrial (catches
        # decryptors/relics, which are optional inputs the SDE table omits).
        all_buy_type_ids = {r.type_id for r in buy_rows}
        buy_item_info = get_item_categories(all_buy_type_ids)
        input_ids = get_industry_input_ids(all_buy_type_ids)

        def is_industry_input(type_id):
            return (type_id in input_ids
                    or buy_item_info.get(type_id, {}).get("category") in INDUSTRY_CATEGORIES)

        buy_totals = defaultdict(float)
        spent_by_char = defaultdict(float)
        for r in buy_rows:
            if not is_industry_input(r.type_id):
                continue
            buy_totals[r.type_id] += r.total
            spent_by_char[r.character_id] += r.total
        total_spent = sum(spent_by_char.values())

        # Total Earned mirrors the Spent scoping: a sale counts if the item is
        # something industry PRODUCES (manufacturing/reaction output — your
        # built ships), something industry CONSUMES (selling excess materials
        # recoups investment), or an inherently industrial category. Selling a
        # PLEX, an injector, or PvP loot is not industry income.
        all_sell_type_ids = {r.type_id for r in sell_rows}
        sell_item_info = get_item_categories(all_sell_type_ids)
        output_ids = get_industry_output_ids(all_sell_type_ids)
        sell_input_ids = get_industry_input_ids(all_sell_type_ids)

        def is_industry_sale(type_id):
            return (type_id in output_ids
                    or type_id in sell_input_ids
                    or sell_item_info.get(type_id, {}).get("category") in INDUSTRY_CATEGORIES)

        sell_totals = defaultdict(float)
        earned_by_char = defaultdict(float)
        for r in sell_rows:
            if not is_industry_sale(r.type_id):
                continue
            sell_totals[r.type_id] += r.total
            earned_by_char[r.character_id] += r.total
        total_earned = sum(earned_by_char.values())

        # Taxes/fees from the wallet journal, negated so costs display positive.
        tax_q = db.session.query(
            JournalEntry.character_id,
            JournalEntry.ref_type,
            func.sum(JournalEntry.amount).label("total"),
        ).filter(
            JournalEntry.ref_type.in_(FEE_REF_TYPES),
            JournalEntry.character_id.in_(scoped_char_ids),
        )
        if cutoff_date:
            tax_q = tax_q.filter(JournalEntry.date >= cutoff_date)
        tax_rows = tax_q.group_by(
            JournalEntry.character_id, JournalEntry.ref_type
        ).all()

        taxes_by_type = defaultdict(float)
        taxes_by_char = defaultdict(float)
        for r in tax_rows:
            taxes_by_type[r.ref_type] += -r.total
            taxes_by_char[r.character_id] += -r.total
        total_taxes = sum(taxes_by_type.values())
        spent_with_taxes = total_spent + total_taxes

        # Fixed label order from FEE_LABELS so the box layout never jumps around.
        tax_breakdown = [
            {"label": label, "amount": taxes_by_type.get(ref_type, 0)}
            for ref_type, label in FEE_LABELS.items()
        ]

        alt_rows = [
            {
                "name": ch.character_name,
                "spent": spent_by_char.get(ch.character_id, 0),
                "earned": earned_by_char.get(ch.character_id, 0),
                "taxes": taxes_by_char.get(ch.character_id, 0),
                "jobs": jobs_by_char.get(ch.character_id, 0),
            }
            for ch in characters
        ]

        type_ids = set(buy_totals.keys()) | set(sell_totals.keys())
        names = get_type_names(type_ids)

        top_buys = sorted(
            [{"name": names.get(k, "Unknown"), "total": v} for k, v in buy_totals.items()],
            key=lambda x: x["total"], reverse=True
        )[:25]
        top_sells = sorted(
            [{"name": names.get(k, "Unknown"), "total": v} for k, v in sell_totals.items()],
            key=lambda x: x["total"], reverse=True
        )[:25]

    # After-tax profit: what actually stayed in the wallet.
    profit = total_earned - total_spent - total_taxes

    return render_template("index.html", character=character, jobs=jobs,
        total_spent=total_spent, total_earned=total_earned, profit=profit,
        top_buys=top_buys, top_sells=top_sells,
        tax_breakdown=tax_breakdown, total_taxes=total_taxes,
        spent_with_taxes=spent_with_taxes, alt_rows=alt_rows,
        period=period, period_label=period_label)


@main.route("/blueprints")
def blueprints():
    character = get_character()
    if not character:
        return redirect(url_for("main.index"))

    all_chars, fetch_chars, sel = _selected_characters()
    # Merged across alts: BPO/BPC counts and total runs sum naturally, and
    # Best ME/TE becomes the best copy anyone in the operation owns.
    raw_blueprints = _gather_esi(fetch_chars, "blueprints", "blueprints/", ttl=600)

    type_ids = {bp["type_id"] for bp in raw_blueprints}
    type_names = get_type_names(type_ids)

    grouped = {}
    for bp in raw_blueprints:
        tid = bp["type_id"]
        if tid not in grouped:
            grouped[tid] = {
                "name": type_names.get(tid, "unknown"),
                "bpo": 0,
                "bpc": 0,
                "total_runs": 0,
                "me": bp["material_efficiency"],
                "te": bp["time_efficiency"],
            }
        # You can own several copies of the same print at different research
        # levels (ME10 BPO + ME2 invented BPCs). Show the best copy's stats
        # rather than whichever one ESI happened to list first.
        grouped[tid]["me"] = max(grouped[tid]["me"], bp["material_efficiency"])
        grouped[tid]["te"] = max(grouped[tid]["te"], bp["time_efficiency"])
        # ESI represents BPOs (unlimited use) with runs = -1, so they're counted
        # separately rather than added into total_runs, which is BPC-only.
        if bp["runs"] == -1:
            # A stack of N unresearched BPOs is ONE list entry with quantity=N;
            # single researched originals have quantity=-1 and count as one.
            grouped[tid]["bpo"] += bp["quantity"] if bp["quantity"] > 0 else 1
        else:
            grouped[tid]["bpc"] += 1
            grouped[tid]["total_runs"] += bp["runs"]

    return render_template(
        "blueprints.html", character=character, blueprints=list(grouped.values()),
        all_characters=all_chars, selected_character=sel,
    )


@main.route("/inventory")
def inventory():
    character = get_character()
    if not character:
        return redirect(url_for("main.index"))

    all_chars, fetch_chars, sel = _selected_characters()
    raw_assets = _gather_esi(fetch_chars, "assets", "assets/", ttl=300)

    inventory = {}
    for a in raw_assets:
        tid = a["type_id"]
        inventory[tid] = inventory.get(tid, 0) + a["quantity"]

    # Price failure (ESI hiccup) degrades to zero-values rather than a 500 —
    # quantities are still worth showing without valuations.
    try:
        prices = get_market_prices()
    except Exception:
        prices = {}

    # Thresholds are per user — otherwise one member's limit would paint red
    # flags across everyone else's inventory page.
    _user = get_current_user()
    limits = {
        sl.type_id: sl.min_qty
        for sl in StockLimit.query.filter_by(user_id=_user.id if _user else None).all()
    }

    item_info = get_item_categories(inventory.keys())
    grouped = {}
    for type_id, qty in inventory.items():
        info = item_info.get(type_id)
        # Items with no known SDE category (rare/unrecognized type_ids) are
        # dropped rather than shown under an "Unknown" bucket.
        if not info:
            continue
        category = info["category"]
        if category not in grouped:
            grouped[category] = []
        grouped[category].append({
            "type_id": type_id,
            "name": info["name"],
            "qty": qty,
            "value": qty * prices.get(type_id, 0),
            "limit": limits.get(type_id),
            # "Low" compares whatever this view shows (all alts or one) against
            # the limit — filtering to a single alt can flag items the rest of
            # the operation still has plenty of, which is intentional.
            "low": type_id in limits and qty < limits[type_id],
        })

    selected_categories = request.args.getlist("categories")
    if selected_categories:
        grouped = {k: v for k, v in grouped.items() if k in selected_categories}

    # Most valuable items first within each category; totals match what's shown.
    for items in grouped.values():
        items.sort(key=lambda i: i["value"], reverse=True)
    category_totals = {cat: sum(i["value"] for i in items) for cat, items in grouped.items()}
    total_value = sum(category_totals.values())

    return render_template(
        "inventory.html",
        character=character,
        grouped=grouped,
        selected_categories=selected_categories,
        category_totals=category_totals,
        total_value=total_value,
        all_characters=all_chars,
        selected_character=sel,
    )


def _max_runs_for_material(have, base_qty, me):
    # EVE's real material requirement for N runs of a print at ME level `me`:
    #     need(N) = max(N, ceil(N * base_qty * (100 - me) / 100))
    # Two things simple division gets wrong: the ceil applies to the BATCH total
    # (10 runs at 0.9x cost round up once, not per run), and each run always
    # consumes at least 1 of every material no matter how high ME is.
    # Integer arithmetic keeps it exact — (x + 99) // 100 is ceil(x / 100).
    if have <= 0 or base_qty <= 0:
        return 0
    mod = 100 - me
    # Optimistic upper bound, then walk down until need(n) fits in stock.
    # need(n) >= n means n can never exceed `have`; the +1 covers rounding slack.
    n = min(have, (have * 100) // (base_qty * mod) + 1)
    while n > 0 and max(n, (n * base_qty * mod + 99) // 100) > have:
        n -= 1
    return n


@main.route("/build")
def build():
    character = get_character()
    if not character:
        return redirect(url_for("main.index"))

    all_chars, fetch_chars, sel = _selected_characters()
    # Whole-operation readiness: any alt's prints vs everyone's combined
    # materials. (Materials may still need hauling to one place, of course.)
    raw_blueprints = _gather_esi(fetch_chars, "blueprints", "blueprints/", ttl=600)
    raw_assets = _gather_esi(fetch_chars, "assets", "assets/", ttl=300)

    inv = {}
    for a in raw_assets:
        inv[a["type_id"]] = inv.get(a["type_id"], 0) + a["quantity"]

    # Per print type: the best ME copy owned (that's the copy you'd build from),
    # and how many runs the owned prints can actually start — BPCs are limited
    # by their remaining runs; any BPO (runs == -1) means unlimited.
    best_me = {}
    print_runs = {}
    has_bpo = set()
    for bp in raw_blueprints:
        tid = bp["type_id"]
        best_me[tid] = max(best_me.get(tid, 0), bp["material_efficiency"])
        if bp["runs"] == -1:
            has_bpo.add(tid)
        else:
            print_runs[tid] = print_runs.get(tid, 0) + bp["runs"]

    bp_type_ids = set(best_me)
    type_names = get_type_names(bp_type_ids)
    materials = get_blueprint_materials(bp_type_ids)

    buildable = []
    for tid in bp_type_ids:
        mats = materials.get(tid)
        if not mats:
            continue
        me = best_me[tid]
        # Capped by whichever required material is scarcest on hand...
        material_runs = min(
            (_max_runs_for_material(inv.get(m["material_id"], 0), m["qty"], me)
             for m in mats if m["qty"] > 0),
            default=0,
        )
        # ...and by the runs remaining on the prints themselves.
        if tid in has_bpo:
            runs_available = None  # unlimited
            max_runs = material_runs
        else:
            runs_available = print_runs.get(tid, 0)
            max_runs = min(material_runs, runs_available)
        if max_runs > 0:
            buildable.append({
                "name": type_names.get(tid, "Unknown"),
                "me": me,
                "runs_available": runs_available,
                "max_runs": max_runs,
            })

    buildable.sort(key=lambda x: x["max_runs"], reverse=True)

    return render_template("build.html", character=character, buildable=buildable,
                           all_characters=all_chars, selected_character=sel)


@main.route("/inventory/limit", methods=["POST"])
def set_stock_limit():
    character = get_character()
    if not character:
        return redirect(url_for("main.index"))

    user = get_current_user()
    if not user:
        deny_as_spy("attempted to set a stock limit with no resolvable user")

    type_id = request.form.get("type_id", "")
    min_qty = request.form.get("min_qty", "").strip()
    if type_id.isdigit():
        type_id = int(type_id)
        # Scoped by user_id on the WRITE path too, not just the read. Without
        # this a POST could edit or delete another member's threshold by id.
        row = StockLimit.query.filter_by(user_id=user.id, type_id=type_id).first()
        # Empty or zero clears the limit; a positive number sets/updates it.
        if not min_qty.isdigit() or int(min_qty) <= 0:
            if row:
                db.session.delete(row)
        elif row:
            row.min_qty = int(min_qty)
        else:
            db.session.add(StockLimit(
                user_id=user.id, type_id=type_id, min_qty=int(min_qty)
            ))
        db.session.commit()

    # Bounce back to the inventory view (with its filters) the form came from;
    # only same-site referrers, so this can't redirect off-site.
    ref = request.referrer
    if ref and ref.startswith(request.host_url):
        return redirect(ref)
    return redirect(url_for("main.inventory"))


@main.route("/calculator")
def calculator():
    character = get_character()
    if not character:
        return redirect(url_for("main.index"))

    all_chars = owned_characters()
    raw_blueprints = _gather_esi(all_chars, "blueprints", "blueprints/", ttl=600)

    best_me = {}
    for bp in raw_blueprints:
        tid = bp["type_id"]
        best_me[tid] = max(best_me.get(tid, 0), bp["material_efficiency"])

    # Dropdown only lists prints that actually manufacture something —
    # research-only or reaction formulas can't be costed here (yet).
    products = get_blueprint_products(best_me.keys())
    bp_names = get_type_names(products.keys())
    bp_options = sorted(
        [{"type_id": tid, "name": bp_names.get(tid, "Unknown")} for tid in products],
        key=lambda b: b["name"],
    )

    plan = None
    plan_error = None
    sel_bp = request.args.get("bp", "")
    raw_runs = request.args.get("runs", "1")
    runs = int(raw_runs) if raw_runs.isdigit() and int(raw_runs) > 0 else 1
    runs = min(runs, 1_000_000)

    hub = request.args.get("hub", "jita")
    if hub not in MARKET_HUBS:
        hub = "jita"

    if sel_bp.isdigit() and int(sel_bp) in products:
        tid = int(sel_bp)
        mats = get_blueprint_materials({tid}).get(tid, [])
        product = products[tid]

        raw_assets = _gather_esi(all_chars, "assets", "assets/", ttl=300)
        inv = {}
        for a in raw_assets:
            inv[a["type_id"]] = inv.get(a["type_id"], 0) + a["quantity"]

        price_ids = {m["material_id"] for m in mats} | {product["product_id"]}
        try:
            prices = get_hub_prices(price_ids, hub)
        except Exception:
            prices = None
            plan_error = f"{MARKET_HUBS[hub]['name']} price fetch failed — ESI may be having a moment. Try again shortly."

        if prices is not None:
            plan = compute_build_plan(
                mats, best_me[tid], runs, inv, prices,
                product["product_id"], product["qty_per_run"],
            )
            mat_names = get_type_names({r["material_id"] for r in plan["rows"]})
            for r in plan["rows"]:
                r["name"] = mat_names.get(r["material_id"], "Unknown")
            plan["rows"].sort(key=lambda r: r["cost"], reverse=True)
            plan["blueprint_name"] = bp_names.get(tid, "Unknown")
            plan["product_name"] = get_type_name(product["product_id"])
            plan["qty_per_run"] = product["qty_per_run"]
            plan["me"] = best_me[tid]
            plan["runs"] = runs

    return render_template(
        "calculator.html",
        character=character,
        bp_options=bp_options,
        selected_bp=sel_bp,
        runs=runs,
        plan=plan,
        plan_error=plan_error,
        market_hubs=MARKET_HUBS,
        selected_hub=hub,
    )




@main.route("/transactions")
def transactions():
    character = get_character()
    if not character:
        return redirect(url_for("main.index"))

    characters = owned_characters()
    char_names = {c.character_id: c.character_name for c in characters}
    scoped_char_ids = [c.character_id for c in characters]

    # Ownership boundary: this page can only ever show the user's own trades.
    q = Transaction.query.filter(Transaction.character_id.in_(scoped_char_ids))

    sel_char = request.args.get("character", "")
    if sel_char.isdigit():
        # Filtering to a character you don't own is a probe, same as on the
        # other pages — the id had to be put in the URL by hand.
        if int(sel_char) not in scoped_char_ids:
            deny_as_spy(f"filtered transactions by character_id={sel_char} which they do not own")
        q = q.filter_by(character_id=int(sel_char))

    side = request.args.get("side", "")
    if side == "buy":
        q = q.filter_by(is_buy=True)
    elif side == "sell":
        q = q.filter_by(is_buy=False)

    item = request.args.get("item", "").strip()
    if item:
        # Name search resolves to type_ids via the SDE; no match = no rows.
        q = q.filter(Transaction.type_id.in_(search_type_ids(item)))

    # Bad date input is ignored rather than erroring — the filter just no-ops.
    for arg, op in (("from", "__ge__"), ("to", "__le__")):
        raw = request.args.get(arg, "")
        if raw:
            try:
                bound = datetime.fromisoformat(raw)
            except ValueError:
                continue
            q = q.filter(getattr(Transaction.date, op)(bound))

    rows = q.order_by(Transaction.date.desc()).limit(500).all()
    names = get_type_names({r.type_id for r in rows})

    txns = [{
        "date": r.date,
        "character": char_names.get(r.character_id, "?"),
        "item": names.get(r.type_id, "Unknown"),
        "qty": r.quantity,
        "unit_price": r.unit_price,
        "total": r.quantity * r.unit_price,
        "is_buy": r.is_buy,
    } for r in rows]

    return render_template(
        "transactions.html",
        character=character,
        txns=txns,
        characters=characters,
        filters={
            "character": sel_char,
            "side": side,
            "item": item,
            "from": request.args.get("from", ""),
            "to": request.args.get("to", ""),
        },
    )


@main.route("/notify/test", methods=["POST"])
def notify_test():
    # POST: sends a push — a GET with side effects can be triggered by any
    # page that embeds the URL (<img src=...>), and CSRF only guards POSTs.
    character = get_character()
    if not character:
        return redirect(url_for("main.index"))
    from app.notify import notify
    sent = notify("EVE Industry Toolbox", "Test notification — Ntfy is wired up.")
    if sent:
        return "Notification sent — check your devices."
    return ("NTFY_TOPIC is not set in .env (or the push failed). "
            "Add NTFY_TOPIC=<your-topic-name> and restart."), 200


@main.route("/transactions/sync", methods=["POST"])
def sync_transactions():
    # POST for the same reason as /notify/test: it writes to the DB and burns
    # ESI calls, so it must not be triggerable by embedding a URL in a page.
    character = get_character()
    if not character:
        return redirect(url_for("main.index"))
    # Manual sync covers the user's own alts, and pulls the wallet journal
    # (taxes/fees) alongside market transactions — same as the 30-minute
    # auto-sync. Scoped so nobody can burn ESI calls against other people's
    # characters (the scheduler still syncs everyone, on its own schedule).
    for ch in owned_characters():
        try:
            ch = refresh_if_expired(ch)
            sync_character_transactions(ch)
            sync_character_journal(ch)
        except Exception:
            continue
    return redirect(url_for("main.index"))
