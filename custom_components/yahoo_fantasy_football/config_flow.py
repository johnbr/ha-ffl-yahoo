"""Config flow for Yahoo Fantasy Football.

Scaffold: a league is identified by its Yahoo league key. Yahoo requires OAuth2
for every data call, so this flow will be replaced by an
``application_credentials`` OAuth handshake (redirect URI
``https://my.home-assistant.io/redirect/oauth``) plus a league picker in the
next milestone. Nothing is published to HACS yet, so that swap is not a
breaking change for anyone.
"""

from __future__ import annotations

import re

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult

from .const import CONF_LEAGUE_KEY, CONF_NAME, DEFAULT_NAME, DOMAIN

# Yahoo league keys look like ``nfl.l.123456`` or ``461.l.123456`` — a game key
# (numeric for a specific season, or the ``nfl`` alias for the current one),
# then ``.l.``, then the numeric league id.
_LEAGUE_KEY_RE = re.compile(r"^(?:[a-z]{3}|\d+)\.l\.\d+$")


class YahooFantasyFootballConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for a Yahoo Fantasy Football league."""

    VERSION = 1

    async def async_step_user(self, user_input: dict | None = None) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            league_key = str(user_input[CONF_LEAGUE_KEY]).strip().lower()
            name = str(user_input.get(CONF_NAME) or DEFAULT_NAME).strip()

            if not _LEAGUE_KEY_RE.match(league_key):
                errors["base"] = "invalid_league_key"
            else:
                await self.async_set_unique_id(league_key)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=name,
                    data={
                        CONF_LEAGUE_KEY: league_key,
                        CONF_NAME: name,
                    },
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_LEAGUE_KEY): str,
                vol.Optional(CONF_NAME, default=DEFAULT_NAME): str,
            }
        )

        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)
