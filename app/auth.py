import os
import secrets
import requests
from flask import Blueprint, redirect, request, session, url_for
from app.models import Character
from app import db
from datetime import datetime, timezone, timedelta

auth = Blueprint("auth", __name__)

EVE_SSO_AUTH_URL = "https://login.eveonline.com/v2/oauth/authorize"
EVE_SSO_TOKEN_URL = "https://login.eveonline.com/v2/oauth/token"

# Same story as routes.ESI_USER_AGENT (defined separately — routes imports
# this module, so importing back would be circular): CCP asks every client
# to identify itself, and treats anonymous defaults as abuse-suspect.
SSO_HEADERS = {"User-Agent": os.getenv("ESI_USER_AGENT", "eve-indy-toolbox/1.0 (personal industry tool)")}


class TokenRefreshError(Exception):
    """EVE SSO rejected the refresh token outright — revoked, expired, or the
    app credentials changed. Non-retryable: that character has to log in again.

    Deliberately distinct from transient failures (network errors, SSO 5xx,
    malformed JSON, DB errors), which keep their own exception types so callers
    can tell "this session is dead" apart from "ESI is having a moment".
    """


@auth.route("/login")
def login():
    # Random per-login value, checked again in /callback, so a forged callback
    # request (without ever going through EVE's login flow) is rejected.
    state = secrets.token_hex(16)
    session["oauth_state"] = state
    params = {
        "response_type": "code",
        "redirect_uri": os.getenv("EVE_CALLBACK_URL"),
        "client_id": os.getenv("EVE_CLIENT_ID"),
        # Requests every ESI scope the app is registered for, even ones not used
        # yet, so future features don't require re-authing existing characters.
        "scope": " ".join(
            [
                "publicData",
                "esi-calendar.respond_calendar_events.v1",
                "esi-calendar.read_calendar_events.v1",
                "esi-location.read_location.v1",
                "esi-location.read_ship_type.v1",
                "esi-mail.organize_mail.v1",
                "esi-mail.read_mail.v1",
                "esi-mail.send_mail.v1",
                "esi-skills.read_skills.v1",
                "esi-skills.read_skillqueue.v1",
                "esi-wallet.read_character_wallet.v1",
                "esi-wallet.read_corporation_wallet.v1",
                "esi-search.search_structures.v1",
                "esi-clones.read_clones.v1",
                "esi-characters.read_contacts.v1",
                "esi-universe.read_structures.v1",
                "esi-killmails.read_killmails.v1",
                "esi-corporations.read_corporation_membership.v1",
                "esi-assets.read_assets.v1",
                "esi-planets.manage_planets.v1",
                "esi-fleets.read_fleet.v1",
                "esi-fleets.write_fleet.v1",
                "esi-ui.open_window.v1",
                "esi-ui.write_waypoint.v1",
                "esi-characters.write_contacts.v1",
                "esi-fittings.read_fittings.v1",
                "esi-fittings.write_fittings.v1",
                "esi-markets.structure_markets.v1",
                "esi-corporations.read_structures.v1",
                "esi-characters.read_loyalty.v1",
                "esi-characters.read_chat_channels.v1",
                "esi-characters.read_medals.v1",
                "esi-characters.read_standings.v1",
                "esi-characters.read_agents_research.v1",
                "esi-industry.read_character_jobs.v1",
                "esi-markets.read_character_orders.v1",
                "esi-characters.read_blueprints.v1",
                "esi-characters.read_corporation_roles.v1",
                "esi-location.read_online.v1",
                "esi-contracts.read_character_contracts.v1",
                "esi-clones.read_implants.v1",
                "esi-characters.read_fatigue.v1",
                "esi-killmails.read_corporation_killmails.v1",
                "esi-corporations.track_members.v1",
                "esi-wallet.read_corporation_wallets.v1",
                "esi-characters.read_notifications.v1",
                "esi-corporations.read_divisions.v1",
                "esi-corporations.read_contacts.v1",
                "esi-assets.read_corporation_assets.v1",
                "esi-corporations.read_titles.v1",
                "esi-corporations.read_blueprints.v1",
                "esi-contracts.read_corporation_contracts.v1",
                "esi-corporations.read_standings.v1",
                "esi-corporations.read_starbases.v1",
                "esi-industry.read_corporation_jobs.v1",
                "esi-markets.read_corporation_orders.v1",
                "esi-corporations.read_container_logs.v1",
                "esi-industry.read_character_mining.v1",
                "esi-industry.read_corporation_mining.v1",
                "esi-planets.read_customs_offices.v1",
                "esi-corporations.read_facilities.v1",
                "esi-corporations.read_medals.v1",
                "esi-characters.read_titles.v1",
                "esi-alliances.read_contacts.v1",
                "esi-characters.read_fw_stats.v1",
                "esi-corporations.read_fw_stats.v1",
                "esi-corporations.read_projects.v1",
            ]
        ),
        "state": state,
    }
    req = requests.Request("GET", EVE_SSO_AUTH_URL, params=params)
    url = req.prepare().url
    return redirect(url)


@auth.route("/callback")
def callback():
    code = request.args.get("code")
    state = request.args.get("state")

    # CSRF check: reject unless this matches the value /login put in the session.
    if not state or state != session.get("oauth_state"):
        return "Invalid OAuth state", 400

    # One-time use — a replayed callback with the same state should also fail.
    session.pop("oauth_state", None)

    response = requests.post(
        EVE_SSO_TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
        },
        auth=(os.getenv("EVE_CLIENT_ID"), os.getenv("EVE_CLIENT_SECRET")),
        headers=SSO_HEADERS,
    )
    if not response.ok:
        return "EVE SSO token request failed", 502
    tokens = response.json()

    verify = requests.get(
        "https://login.eveonline.com/oauth/verify",
        headers={"Authorization": f'Bearer {tokens["access_token"]}', **SSO_HEADERS},
    )
    if not verify.ok:
        return "EVE SSO verification request failed", 502
    character_info = verify.json()

    expiry = datetime.now(timezone.utc) + timedelta(seconds=tokens["expires_in"])

    # Deferred import: routes imports this module, so importing it at module
    # level would be circular.
    from app.routes import get_current_user

    # Whoever is signed in on the host site becomes the owner of this character.
    # Without an owner the character would belong to nobody and be invisible to
    # everyone, so refuse the link rather than orphan it.
    user = get_current_user()
    if user is None:
        return ("You must be signed in to link an EVE character.", 403)

    # CORP GATE: only characters in the corp/alliance may be linked. Without
    # this, anyone who can reach the URL could attach an outside character and
    # use the tool's ESI quota. ALLOWED_CORP_IDS unset = gate off (local dev).
    allowed = {c.strip() for c in os.getenv("ALLOWED_CORP_IDS", "").split(",") if c.strip()}
    if allowed:
        public = requests.get(
            f"https://esi.evetech.net/latest/characters/{character_info['CharacterID']}/",
            headers=SSO_HEADERS, timeout=30,
        )
        if not public.ok:
            # Fail CLOSED: if membership can't be verified, don't link. An ESI
            # blip means "try again in a minute", not "let anyone in".
            return ("Could not verify corp membership with ESI — try again shortly.", 502)
        corp_id = str(public.json().get("corporation_id", ""))
        if corp_id not in allowed:
            return ("That character is not in the corporation.", 403)

    # Re-login for an already-known character just refreshes their tokens in place.
    character = Character.query.filter_by(
        character_id=character_info["CharacterID"]
    ).first()

    if character:
        # An EVE character belongs to exactly one account. If it is already
        # linked by somebody else, refuse — otherwise anyone who can complete
        # SSO for a character could seize another member's alt, taking its
        # wallet and asset history with it.
        if character.user_id is not None and character.user_id != user.id:
            return ("That character is already linked to another account.", 403)
        character.access_token = tokens["access_token"]
        character.refresh_token = tokens["refresh_token"]
        character.token_expiry = expiry
        character.user_id = user.id
    else:
        character = Character(
            character_id=character_info["CharacterID"],
            character_name=character_info["CharacterName"],
            access_token=tokens["access_token"],
            refresh_token=tokens["refresh_token"],
            token_expiry=expiry,
            user_id=user.id,
        )
        db.session.add(character)

    db.session.commit()

    # "+ Add Character" runs this same flow — if someone is already signed in,
    # keep their identity instead of switching the session to the new alt.
    # The alt's tokens are saved above either way, which is all linking needs.
    if "character_id" not in session:
        session["character_id"] = character_info["CharacterID"]

    return redirect(url_for("main.index"))


@auth.route("/logout")
def logout():
    # CORP SITE INTEGRATION: in corp mode this app does not own the session, so
    # it clears only its own local state and hands off to the host site's
    # logout. Configure that URL via CORP_LOGOUT_URL (see app/integration.py).
    from app import integration

    session.clear()
    if integration.integration_mode() == "corp":
        return redirect(integration.logout_url())
    return redirect(url_for("main.index"))


def refresh_access_token(character):
    # ESI access tokens expire after ~20 minutes; callers check token_expiry
    # before each API call and refresh here to get a new one via refresh_token.
    response = requests.post(
        EVE_SSO_TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": character.refresh_token,
        },
        auth=(os.getenv("EVE_CLIENT_ID"), os.getenv("EVE_CLIENT_SECRET")),
        headers=SSO_HEADERS,
    )
    # Only OAuth2 "invalid_grant" (RFC 6749 §5.2) means the refresh token itself
    # is dead — revoked, expired, or invalidated by a password change. That is
    # terminal for this character: they have to log in again.
    if 400 <= response.status_code < 500:
        try:
            payload = response.json()
        except ValueError:
            payload = None
        # The body is not guaranteed to be a JSON *object*. A proxy or CDN in
        # front of SSO can return a bare string or array, which parses fine but
        # has no .get() — that would raise AttributeError, sailing straight past
        # the ValueError above. Check the shape before reading the error field.
        oauth_error = payload.get("error") if isinstance(payload, dict) else None
        if oauth_error == "invalid_grant":
            raise TokenRefreshError(
                f"EVE SSO rejected the refresh token for character "
                f"{character.character_id} (HTTP {response.status_code}, invalid_grant)"
            )
    # Every other status is the app's problem, not this character's: 401
    # invalid_client means OUR client credentials are wrong (logging the user out
    # would hide a config error and drop every character), 429 means we're rate
    # limited, 5xx means SSO is struggling. raise_for_status() surfaces them as a
    # normal requests.HTTPError, which keeps the response — and any Retry-After
    # header — attached for callers to inspect.
    response.raise_for_status()
    token = response.json()
    character.access_token = token["access_token"]
    # EVE SSO can rotate the refresh token; persist the new one when it does,
    # otherwise the next refresh would reuse a stale token and fail.
    if token.get("refresh_token"):
        character.refresh_token = token["refresh_token"]
    character.token_expiry = datetime.now(timezone.utc) + timedelta(seconds=token["expires_in"])
    db.session.commit()
    return character
