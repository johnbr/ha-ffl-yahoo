"""Config flow for Yahoo Fantasy Football.

A league is identified by the numeric id in its Yahoo URL, e.g. ``802904`` in
``football.fantasysports.yahoo.com/f1/802904``. For a public league that is
all this source needs — no OAuth and no credentials.

A **private** league is served only to its members. Yahoo answers anyone else
exactly as it answers an id with no league, so setup cannot tell the two apart;
it says so, and takes an optional Yahoo cookie copied from a signed-in browser
(see the README). When that cookie stops working the coordinator raises
``ConfigEntryAuthFailed`` and :meth:`async_step_reauth` asks for a new one.

Two steps, because the second one can only be built from the first's answer:

1. ``user`` — take the league id and fetch it. Validating by actually fetching
   beats creating an entry that never produces data.
2. ``team`` — pick which team is yours, from the real names the fetch returned.
   Anonymous access reports ``isOwned: false`` for every team, so this cannot
   be detected and has to be asked. It is optional: skip it and you get the
   league scoreboard without the my-matchup sensor.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_COOKIE,
    CONF_LEAGUE_ID,
    CONF_NAME,
    CONF_TEAM_ID,
    DEFAULT_NAME,
    DOMAIN,
)
from .http_client import YahooAccessDenied, fetch_text, normalize_cookie
from .redzone_client import USER_AGENT, RedzoneClient, YahooWebError
from .yahoo_web import extract_league_id

_LOGGER = logging.getLogger(__name__)

NO_TEAM = "none"


class YahooFantasyFootballConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for a Yahoo Fantasy Football league."""

    VERSION = 1

    def __init__(self) -> None:
        self._league_id: str = ""
        self._name: str = DEFAULT_NAME
        self._teams: dict[str, str] = {}
        self._cookie: str = ""

    def _client(self, league_id: str, cookie: str = "") -> RedzoneClient:
        session = async_get_clientsession(self.hass)

        async def _fetch(url: str) -> str:
            body, _ = await fetch_text(session, url, headers={"User-Agent": USER_AGENT}, cookie=cookie)
            return body or ""

        return RedzoneClient(_fetch, league_id)

    async def _validate(self, league_id: str, cookie: str) -> tuple[str | None, str]:
        """Fetch the league's seed: ``(seed, "")``, or ``(None, error key)``."""
        try:
            return await self._client(league_id, cookie).async_fetch_seed(), ""
        except YahooAccessDenied as err:
            _LOGGER.debug("League %s access refused: %s", league_id, err)
            if not err.league_not_found:
                return None, "yahoo_access_denied"
            return None, "cookie_rejected" if cookie else "league_not_found"
        except YahooWebError as err:
            _LOGGER.debug("League %s failed validation: %s", league_id, err)
            return None, "cannot_connect"

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            league_id = extract_league_id(str(user_input[CONF_LEAGUE_ID]))
            try:
                cookie = normalize_cookie(str(user_input.get(CONF_COOKIE) or ""))
            except ValueError:
                cookie = None
            if not league_id:
                errors["base"] = "invalid_league_id"
            elif cookie is None:
                errors["base"] = "invalid_cookie"
            else:
                await self.async_set_unique_id(f"web:{league_id}")
                self._abort_if_unique_id_configured()
                seed, error = await self._validate(league_id, cookie)
                if seed is None:
                    errors["base"] = error
                else:
                    from .yahoo_redzone import league_name, team_choices

                    try:
                        self._teams = team_choices(seed, league_id)
                        found = league_name(seed, league_id)
                    except (ValueError, KeyError, TypeError) as err:
                        _LOGGER.debug("League %s payload unreadable: %s", league_id, err)
                        errors["base"] = "cannot_connect"
                    else:
                        self._league_id = league_id
                        self._cookie = cookie
                        # Yahoo knows the league's real name, so offer it rather
                        # than making the user retype it. An explicit entry wins.
                        typed = str(user_input.get(CONF_NAME) or "").strip()
                        self._name = typed or found or DEFAULT_NAME
                        return await self.async_step_team()

        schema = vol.Schema(
            {
                vol.Required(CONF_LEAGUE_ID): str,
                vol.Optional(CONF_NAME): str,
                vol.Optional(CONF_COOKIE): str,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> FlowResult:
        """Yahoo stopped accepting a private league's cookie."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Take a fresh cookie, and keep it only once Yahoo serves the league with it."""
        entry = self._get_reauth_entry()
        league_id = str(entry.data.get(CONF_LEAGUE_ID, ""))
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                cookie = normalize_cookie(str(user_input.get(CONF_COOKIE) or ""))
            except ValueError:
                errors["base"] = "invalid_cookie"
            else:
                _, error = await self._validate(league_id, cookie)
                if error:
                    errors["base"] = error
                else:
                    return self.async_update_reload_and_abort(entry, data_updates={CONF_COOKIE: cookie})

        schema = vol.Schema({vol.Required(CONF_COOKIE): str})
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=schema,
            errors=errors,
            description_placeholders={"league": entry.title},
        )

    async def async_step_team(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Choose which team is yours, from the league's real team names."""
        if user_input is not None:
            data: dict[str, Any] = {
                CONF_LEAGUE_ID: self._league_id,
                CONF_NAME: self._name,
            }
            if self._cookie:
                data[CONF_COOKIE] = self._cookie
            team_id = str(user_input.get(CONF_TEAM_ID) or NO_TEAM)
            if team_id != NO_TEAM:
                data[CONF_TEAM_ID] = team_id
            return self.async_create_entry(title=self._name, data=data)

        choices = {NO_TEAM: "— none, league scoreboard only —", **self._teams}
        schema = vol.Schema({vol.Optional(CONF_TEAM_ID, default=NO_TEAM): vol.In(choices)})
        return self.async_show_form(
            step_id="team",
            data_schema=schema,
            description_placeholders={"league": self._name},
        )
