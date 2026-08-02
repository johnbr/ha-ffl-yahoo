"""Config flow for Yahoo Fantasy Football.

A league is identified by the numeric id in its Yahoo URL, e.g. ``476807`` in
``football.fantasysports.yahoo.com/f1/476807``. That is all the public web
source needs — no OAuth, no credentials — provided the league's privacy setting
is **public**.

The flow validates that by actually fetching the league before creating the
entry. A private league cannot be served by this source at all, and finding that
out at setup with a clear message beats an entry that exists and never produces
data.

Yahoo API credentials, when they arrive, add a second source rather than
replacing this one; ``CONF_LEAGUE_KEY`` is reserved for it.
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
    CONF_SEASON,
    CONF_TEAM_ID,
    DEFAULT_NAME,
    DOMAIN,
)
from .web_client import USER_AGENT, LeagueIsPrivate, YahooWebClient, YahooWebError
from .yahoo_web import extract_league_id, extract_season

_LOGGER = logging.getLogger(__name__)


class YahooFantasyFootballConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for a Yahoo Fantasy Football league."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            raw = str(user_input[CONF_LEAGUE_ID])
            league_id = extract_league_id(raw)
            season = user_input.get(CONF_SEASON) or extract_season(raw)
            name = str(user_input.get(CONF_NAME) or DEFAULT_NAME).strip()
            team_id = str(user_input.get(CONF_TEAM_ID) or "").strip()

            if not league_id:
                errors["base"] = "invalid_league_id"
            else:
                await self.async_set_unique_id(f"web:{league_id}")
                self._abort_if_unique_id_configured()

                error = await self._async_validate(league_id, season)
                if error:
                    errors["base"] = error
                else:
                    data: dict[str, Any] = {
                        CONF_LEAGUE_ID: league_id,
                        CONF_NAME: name,
                    }
                    if season:
                        data[CONF_SEASON] = int(season)
                    if team_id:
                        data[CONF_TEAM_ID] = team_id
                    return self.async_create_entry(title=name, data=data)

        schema = vol.Schema(
            {
                vol.Required(CONF_LEAGUE_ID): str,
                vol.Optional(CONF_NAME, default=DEFAULT_NAME): str,
                vol.Optional(CONF_SEASON): vol.Coerce(int),
                vol.Optional(CONF_TEAM_ID): str,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def _async_validate(self, league_id: str, season: int | None) -> str | None:
        """Fetch the league once. Returns an error key, or None on success."""
        session = async_get_clientsession(self.hass)

        async def _fetch(url: str) -> tuple[str, str]:
            async with session.get(
                url, headers={"User-Agent": USER_AGENT}, allow_redirects=True
            ) as resp:
                return await resp.text(), str(resp.url)

        client = YahooWebClient(_fetch, league_id, season)
        try:
            await client.async_get_week()
        except LeagueIsPrivate:
            return "league_private"
        except YahooWebError as err:
            _LOGGER.debug("League %s failed validation: %s", league_id, err)
            return "cannot_connect"
        return None
