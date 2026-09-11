"""Config flow for Yahoo Fantasy Football.

A league is identified by the numeric id in its Yahoo URL, e.g. ``802904`` in
``football.fantasysports.yahoo.com/f1/802904``. That is all this source needs —
no OAuth, no credentials, and, unlike the older HTML tier, **no requirement
that the league be public**.

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
    CONF_LEAGUE_ID,
    CONF_NAME,
    CONF_TEAM_ID,
    DEFAULT_NAME,
    DOMAIN,
)
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

    def _client(self, league_id: str) -> RedzoneClient:
        session = async_get_clientsession(self.hass)

        async def _fetch(url: str) -> str:
            async with session.get(url, headers={"User-Agent": USER_AGENT}) as resp:
                resp.raise_for_status()
                return await resp.text()

        return RedzoneClient(_fetch, league_id)

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            league_id = extract_league_id(str(user_input[CONF_LEAGUE_ID]))
            if not league_id:
                errors["base"] = "invalid_league_id"
            else:
                await self.async_set_unique_id(f"web:{league_id}")
                self._abort_if_unique_id_configured()
                try:
                    seed = await self._client(league_id).async_fetch_seed()
                except YahooWebError as err:
                    _LOGGER.debug("League %s failed validation: %s", league_id, err)
                    errors["base"] = "cannot_connect"
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
                        # Yahoo knows the league's real name, so offer it rather
                        # than making the user retype it. An explicit entry wins.
                        typed = str(user_input.get(CONF_NAME) or "").strip()
                        self._name = typed or found or DEFAULT_NAME
                        return await self.async_step_team()

        schema = vol.Schema(
            {
                vol.Required(CONF_LEAGUE_ID): str,
                vol.Optional(CONF_NAME): str,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_team(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Choose which team is yours, from the league's real team names."""
        if user_input is not None:
            data: dict[str, Any] = {
                CONF_LEAGUE_ID: self._league_id,
                CONF_NAME: self._name,
            }
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
