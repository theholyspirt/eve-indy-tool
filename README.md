# EVE Industry Toolbox

A Flask web app for tracking EVE Online industry — manufacturing jobs, blueprints, inventory, transactions, and profit.

Runs in two modes:

- **Standalone** (default) — single user on your own machine, with its own EVE SSO login. Zero extra setup.
- **Corp module** — bolts onto an existing corp website that owns user accounts and ranks. Hundreds of members, each seeing **only their own characters and finances**; admin ranks (directors / HR / CEO) see corp-wide data. See [Corp website integration](#corp-website-integration).

---

## Features

- **Dashboard** — Total spent (industry materials only), total earned, profit, top 25 buys/sells, active manufacturing jobs
- **Blueprints** — All owned blueprints grouped by type, showing BPO/BPC counts, total runs, ME, and TE
- **Inventory** — All character assets grouped by category with filters
- **Build Readiness** — Cross-references blueprints against inventory to show how many runs you can complete right now
- **Calculator** — Manufacturing cost/profit per blueprint using live market-hub prices, plus a shopping list for missing materials
- **Transaction tracking** — Wallet transactions saved to local database, auto-synced every 30 minutes

---

## Requirements

- Python 3.10+
- An EVE Online developer app registered at [developers.eveonline.com](https://developers.eveonline.com)
- The EVE SDE database (`eve.db`) placed in the root of the project

---

## Setup

### 1. Clone the repo

```bash
git clone <your-repo-url>
cd eve_indy
```

### 2. Create and activate a virtual environment

```bash
python -m venv venv

# Windows
venv\Scripts\activate

# Linux/Mac
source venv/bin/activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Get the EVE SDE

Download the SQLite version of the EVE Static Data Export from:
https://developers.eveonline.com/resource/resources

Place the file as `eve.db` in the root of the project (the top-level `eve_indy/` folder).

### 5. Create your .env file

Create a file called `.env` in the root of the project:

```
SECRET_KEY=any-long-random-string
EVE_CLIENT_ID=your-eve-app-client-id
EVE_CLIENT_SECRET=your-eve-app-client-secret
EVE_CALLBACK_URL=http://localhost:5000/callback
```

- `SECRET_KEY` — used to sign Flask sessions, make it long and random
- `EVE_CLIENT_ID` and `EVE_CLIENT_SECRET` — from your app on the EVE developer portal
- `EVE_CALLBACK_URL` — must match exactly what you set as the callback URL in your EVE developer app

### 6. Register your EVE developer app

Go to [developers.eveonline.com](https://developers.eveonline.com) and create an application:
- **Connection type:** Authentication & API Access
- **Callback URL:** `http://localhost:5000/callback`
- **Scopes:** Add all scopes you need (the app requests a broad set on login)

### 7. Run the app

```bash
python -m app.run
```

Visit `http://localhost:5000` and log in with your EVE character.

---

## Project structure

```
eve_indy/
├── .env                   # secrets — never commit this
├── .gitignore
├── README.md
├── CLAUDE.md              # developer/AI context file
├── requirements.txt
├── eve.db                 # EVE SDE — never commit this (too large)
├── venv/
└── app/
    ├── __init__.py        # app factory, starts background scheduler
    ├── run.py             # app entry point (run with: python -m app.run)
    ├── models.py          # SQLAlchemy models (Character, Transaction, JournalEntry, StockLimit)
    ├── auth.py            # EVE SSO OAuth2 login/logout/token refresh
    ├── routes.py          # page routes and ESI helpers
    ├── sde.py             # EVE SDE lookups with in-memory caching
    ├── scheduler.py       # background auto-sync job (APScheduler)
    └── templates/
        ├── base.html
        ├── index.html
        ├── blueprints.html
        ├── inventory.html
        ├── build.html
        ├── calculator.html
        └── transactions.html
```

---

## Corp website integration

The tool is designed to bolt onto a corp website that already owns users, login, and ranks. It never creates its own accounts — it **mirrors** the host site's identity.

**Everything the host site must provide lives in one file: [`app/integration.py`](app/integration.py).** Open it; each function marked `IMPLEMENT ME` is documented with a worked example. In short:

1. **`resolve_current_identity()`** *(required)* — read your session cookie, validate it against your session store, and return `{"external_id": "<your users.id>", "role": "<their rank>"}` — or `None` if signed out.
2. **`login_url()` / `logout_url()`** — where your login and logout live (or set `CORP_LOGIN_URL` / `CORP_LOGOUT_URL`).
3. Set `INTEGRATION_MODE=corp` and list which of **your** rank names get corp-wide visibility in `ADMIN_ROLES` (e.g. `admin,director,hr,ceo`). Everything else is a regular member. Matching is exact — unknown ranks never inherit admin.

**Fail-safe:** until `resolve_current_identity()` is implemented (or if it breaks), the app shows **nothing** — every query is scoped by the resolved user, and an unresolved user owns no data. Blank pages mean the integration isn't wired yet; they never mean a leak.

**EVE SSO stays**, but as a one-time "link your character" step per alt, not a login — wallet/asset data only comes from ESI with a token that character granted. Linked characters attach to the signed-in user; a character already linked by someone else cannot be claimed.

### Data isolation

- Members see only their own linked characters, transactions, and totals; admins see corp-wide. Enforced at the query level through one deny-by-default chokepoint (`owned_characters()` in `app/routes.py`) — never by hiding rows in templates.
- Probing another member's data (e.g. hand-editing `?character=`) returns a 403 page and writes an audit row; admins review attempts at `/admin/access-log`.
- Verified by the cross-user tests in `tests/test_isolation.py`, including mutation testing (deliberately removing a filter makes the suite fail).

### Production checklist

- `TOKEN_ENCRYPTION_KEY` set — EVE OAuth tokens encrypted at rest (a leaked DB file is useless without the key; keep the key out of DB backups)
- `SESSION_COOKIE_SECURE=true` behind HTTPS
- `ALLOWED_CORP_IDS` set — only corp members can link characters (fails closed if ESI can't verify)
- `DATABASE_URL` pointing at PostgreSQL — SQLite does not survive hundreds of concurrent users
- `FLASK_DEBUG` unset, app reachable **only** through the host site / reverse proxy
- `RUN_SCHEDULER=false` on all but one worker

All of these are documented in [`.env.example`](.env.example).

---

## Transaction sync

Transactions are pulled from the ESI wallet endpoint and stored locally (SQLite in standalone mode, PostgreSQL in the corp deployment — see [Production checklist](#production-checklist)).

- **Auto-sync** runs every 30 minutes in the background — you'll see log output in the terminal. Characters are fetched from ESI in parallel (token refresh stays sequential), so this scales to a full corp roster rather than one alt at a time
- **Manual sync** — POST to `/transactions/sync` (a button on the dashboard) to force an immediate sync of your own characters
- ESI only returns the last 2,500 transactions — sync regularly to avoid gaps in your history

---

## Notes

- The database file (`instance/eve_indy.db`) is created automatically on first run
- Tokens are refreshed automatically before each ESI call — you only need to log in once
- ESI responses are cached in memory (blueprints: 10 min, assets/jobs: 5 min) to keep the app fast
- SDE lookups are cached in memory for the lifetime of the process

---

## Legal

EVE Online and the EVE logo are the registered trademarks of CCP hf. All rights are reserved worldwide. All other trademarks are the property of their respective owners. EVE Online, the EVE logo, EVE and all associated logos and designs are the intellectual property of CCP hf. CCP hf. has granted permission to EVE Industry Toolbox to use EVE Online and all associated logos and designs for promotional and information purposes on its website but does not endorse, and is not in any way affiliated with, EVE Industry Toolbox. CCP is in no way responsible for the content on or functioning of this website, and you do not contract with CCP in any way when using this website.