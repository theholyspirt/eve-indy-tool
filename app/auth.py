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

    # Re-login for an already-known character just refreshes their tokens in place.
    character = Character.query.filter_by(
        character_id=character_info["CharacterID"]
    ).first()

    if character:
        character.access_token = tokens["access_token"]
        character.refresh_token = tokens["refresh_token"]
        character.token_expiry = expiry
    else:
        character = Character(
            character_id=character_info["CharacterID"],
            character_name=character_info["CharacterName"],
            access_token=tokens["access_token"],
            refresh_token=tokens["refresh_token"],
            token_expiry=expiry,
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
    session.clear()
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
    # A revoked/expired refresh token (e.g. the user changed their EVE password)
    # comes back as a 4xx with no access_token — surface it as a clean error the
    # callers already handle, instead of a bare KeyError on token["access_token"].
    if not response.ok:
        raise RuntimeError(
            f"token refresh failed for {character.character_id}: HTTP {response.status_code}"
        )
    token = response.json()
    character.access_token = token["access_token"]
    # EVE SSO can rotate the refresh token; persist the new one when it does,
    # otherwise the next refresh would reuse a stale token and fail.
    if token.get("refresh_token"):
        character.refresh_token = token["refresh_token"]
    character.token_expiry = datetime.now(timezone.utc) + timedelta(seconds=token["expires_in"])
    db.session.commit()
    return character
