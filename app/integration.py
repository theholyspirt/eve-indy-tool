"""
===============================================================================
 CORP WEBSITE INTEGRATION POINTS  --  THIS IS THE ONLY FILE YOU NEED TO EDIT
===============================================================================

This industry tool is designed to bolt onto an existing corp website that
already owns user accounts, login, and ranks. It deliberately does NOT bring
its own user system.

Everything the host site has to provide is in this file. Nothing outside it
needs changing. Each function marked "IMPLEMENT ME" is a placeholder returning
a safe default; fill them in and the whole app starts working per-user.

-------------------------------------------------------------------------------
 WHAT'S ALREADY DONE vs. WHAT YOU STILL NEED TO DO
-------------------------------------------------------------------------------
   resolve_current_identity() below is already wired for tblds.space's real
   auth model: it reads the "siteAuth" cookie the site's own main.js sets,
   verifies it's a genuine JWT from that site (not just decodes it -- an
   unverified token is the same as no auth at all), and pulls your account id
   + rank out of it. You should NOT need to touch the code.

   All that's left is data only the tblds.space backend has:

   1. Set SITE_AUTH_SECRET in .env to whatever secret tblds.space's backend
      passes to jsonwebtoken.sign(...) when it creates the "siteAuth" cookie
      (look in its backend/src/routes/auth.js). If it signs with something
      other than HS256, set SITE_AUTH_ALG too.
   2. Double check the two claim-name constants just below
      (SITE_AUTH_EXTERNAL_ID_CLAIM / SITE_AUTH_ROLES_CLAIM) match what's
      actually in that JWT's payload. They're a best guess from reading
      tblds.space's public JS -- if login doesn't work, this is the first
      place to look.
   3. Set INTEGRATION_MODE=corp in .env.

   login_url() / logout_url() below already default to "/" (tblds.space's
   homepage has its own login button) -- only touch these if that's wrong.

-------------------------------------------------------------------------------
 HOW TO SWITCH IT ON
-------------------------------------------------------------------------------
   Set INTEGRATION_MODE=corp in the environment (.env).

   INTEGRATION_MODE=local  (default) -- standalone dev mode. Everyone resolves
                                        to a single local admin. Fine on a
                                        laptop, NEVER on a real deployment.
   INTEGRATION_MODE=corp             -- uses resolve_current_identity() below.

-------------------------------------------------------------------------------
 FAIL-SAFE BEHAVIOUR
-------------------------------------------------------------------------------
   In corp mode, returning None from resolve_current_identity() means "not
   signed in". Every main-blueprint page then shows a "Sign in" page linking
   to login_url() (see _require_corp_identity in app/routes.py) instead of
   running the page at all -- no query for anyone's data ever executes.

   So an unimplemented or broken integration fails CLOSED (a sign-in prompt),
   never OPEN (someone else's data). If every page sends you to "Sign in" no
   matter what, this function is returning None -- that is the safety net
   doing its job, not a bug in the tool.
===============================================================================
"""
import os

import jwt
from flask import request

# Which of YOUR rank names count as admin. Comma-separated, case-insensitive.
# Admins see corp-wide data; everyone else sees only their own characters.
# Anything not in this list is treated as a regular member -- deny by default,
# so adding a new rank on your site can never silently grant corp-wide access.
ADMIN_ROLES = {
    r.strip().lower()
    for r in os.getenv("ADMIN_ROLES", "admin,director,hr,ceo").split(",")
    if r.strip()
}

# Name of tblds.space's session cookie (set by its own main.js, readable
# here because it's not HttpOnly -- the site's own JS reads it too).
SITE_AUTH_COOKIE = "siteAuth"

# Claim in that JWT holding the stable per-ACCOUNT id (not per-character --
# an account can have multiple linked EVE characters). Best guess from
# reading tblds.space's public JS; confirm against backend/src/routes/auth.js
# and change here if it's named something else.
SITE_AUTH_EXTERNAL_ID_CLAIM = "sub"

# Claim holding the account's rank names, as a list of strings (tblds.space's
# own HR-access check reads a "roles" array the same way). Confirm this one
# too -- change here if it's named something else.
SITE_AUTH_ROLES_CLAIM = "roles"


def integration_mode():
    return os.getenv("INTEGRATION_MODE", "local").strip().lower()


# =============================================================================
# 1. DONE -- just needs SITE_AUTH_SECRET set in .env (see module docstring)
# =============================================================================
def resolve_current_identity():
    """Work out which of YOUR users is making this HTTP request.

    Called on every request that touches data. Must be cheap -- cache or keep
    the DB connection pooled if you hit a database here.

    RETURN either:

        {"external_id": "<your user's primary key, as a string>",
         "role":        "<that user's rank on your site>"}

    ...or None if nobody is signed in.

    `external_id` is stored locally to tie linked EVE characters to your user.
    Use a STABLE key -- your users.id -- not a username someone can change.

    `role` is matched (case-insensitively) against ADMIN_ROLES above. Unknown
    values are treated as a regular member.

    -------------------------------------------------------------------------
    ALREADY IMPLEMENTED for tblds.space's real auth: it signs a JWT into a
    "siteAuth" cookie (not a session table), so this verifies that JWT
    (never trust one without checking its signature -- that's the same as no
    auth at all) and pulls external_id/role out of its claims. See the
    module docstring at the top of this file for what's still needed:
    SITE_AUTH_SECRET in .env, and confirming SITE_AUTH_EXTERNAL_ID_CLAIM /
    SITE_AUTH_ROLES_CLAIM above actually match tblds.space's JWT payload.

    If this app is ever pointed at a *different* host site that uses a
    plain session cookie + DB table instead of a JWT, that's a different
    shape -- look up "session token" auth pattern rather than reusing this.
    -------------------------------------------------------------------------
    """
    secret = os.getenv("SITE_AUTH_SECRET")
    if not secret:
        # Not configured yet -- fail closed (nobody's signed in) rather than
        # trusting an unverified cookie.
        return None

    token = request.cookies.get(SITE_AUTH_COOKIE)
    if not token:
        return None

    try:
        payload = jwt.decode(token, secret, algorithms=[os.getenv("SITE_AUTH_ALG", "HS256")])
    except jwt.PyJWTError:
        # Bad signature, expired, malformed -- all treated as "not signed in".
        return None

    external_id = payload.get(SITE_AUTH_EXTERNAL_ID_CLAIM)
    if not external_id:
        return None

    roles = payload.get(SITE_AUTH_ROLES_CLAIM) or []
    if isinstance(roles, str):
        roles = [roles]
    # Prefer whichever role actually grants admin, so multi-rank accounts
    # (e.g. ["member", "hr"]) still come through as admin.
    role = next((r for r in roles if str(r).strip().lower() in ADMIN_ROLES), None)
    if role is None:
        role = roles[0] if roles else "member"

    return {"external_id": str(external_id), "role": role}


# =============================================================================
# 2. IMPLEMENT ME  --  where to send signed-out visitors
# =============================================================================
def login_url():
    """URL of your site's login page.

    In corp mode, a visitor resolve_current_identity() can't identify sees a
    "Sign in" page (app/templates/signed_out.html, wired in app/routes.py's
    _require_corp_identity) linking here — instead of the empty-state pages
    they'd otherwise see on every route. Set CORP_LOGIN_URL, or return
    something else here directly.
    """
    return os.getenv("CORP_LOGIN_URL", "/")


# =============================================================================
# 3. IMPLEMENT ME  --  where your logout lives
# =============================================================================
def logout_url():
    """URL of your site's logout. This app does not own the session, so it
    must not try to end it itself."""
    return os.getenv("CORP_LOGOUT_URL", "/")


# =============================================================================
# Below here is wiring. You should not need to change it.
# =============================================================================
def role_is_admin(role):
    """Map one of YOUR rank names onto this app's two-tier model."""
    return bool(role) and str(role).strip().lower() in ADMIN_ROLES
