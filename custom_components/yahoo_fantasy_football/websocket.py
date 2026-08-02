"""WebSocket commands serving the on-demand card payloads.

Rosters and full play history are deliberately kept **out** of entity
attributes: attributes are pushed to every connected client on every state
change, so a whole league's rosters there would be pure websocket churn for data
that is only looked at when someone opens a popup.

These commands read straight from coordinator memory. They never touch Yahoo, so
opening a popup costs nothing upstream and works fine while rate-limited or
serving stale data.

Cards address a league by ``league_id`` (which they already have, from the
scoreboard entity's attributes) rather than by config-entry id, which the
frontend cannot obtain reliably.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN
from .league_state import play_dict, player_rows

_LOGGER = logging.getLogger(__name__)

# Guard against double registration — the commands are global, not per entry.
_REGISTERED = "_ws_registered"

MAX_HISTORY = 200


def _find_coordinator(hass: HomeAssistant, league_id: str):
    """Locate the coordinator for a league id, or None."""
    for entry in hass.data.get(DOMAIN, {}).values():
        if not isinstance(entry, dict):
            continue
        coordinator = entry.get("coordinator")
        if coordinator is not None and str(coordinator.league_id) == str(league_id):
            return coordinator
    return None


@callback
def async_register_commands(hass: HomeAssistant) -> None:
    """Register the WebSocket API commands once per HA run.

    ``voluptuous`` and ``websocket_api`` are imported here rather than at module
    scope so the stubbed unit-test harness does not need them.
    """
    if hass.data.setdefault(DOMAIN, {}).get(_REGISTERED):
        return

    import voluptuous as vol
    from homeassistant.components import websocket_api

    @websocket_api.websocket_command(
        {
            vol.Required("type"): f"{DOMAIN}/matchup_detail",
            vol.Required("league_id"): str,
            vol.Required("matchup_index"): vol.Coerce(int),
        }
    )
    @callback
    def handle_matchup_detail(
        hass: HomeAssistant, connection: Any, msg: dict[str, Any]
    ) -> None:
        """Both rosters for one matchup — the click-to-expand popup."""
        coordinator = _find_coordinator(hass, msg["league_id"])
        if coordinator is None:
            connection.send_error(msg["id"], "not_found", "No such league configured")
            return

        data = coordinator.league_data
        if data is None:
            connection.send_result(msg["id"], {"matchup_id": None, "sides": []})
            return

        # ``matchup_index`` is 1-based on the wire, matching Yahoo's own mid1.
        payload = player_rows(data, int(msg["matchup_index"]) - 1)
        connection.send_result(msg["id"], {**payload, "week": data.week})

    @websocket_api.websocket_command(
        {
            vol.Required("type"): f"{DOMAIN}/play_history",
            vol.Required("league_id"): str,
            vol.Optional("matchup_id"): vol.Any(str, None),
            vol.Optional("team_key"): vol.Any(str, None),
            vol.Optional("limit", default=50): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=MAX_HISTORY)
            ),
            vol.Optional("include_corrections", default=True): bool,
        }
    )
    @callback
    def handle_play_history(
        hass: HomeAssistant, connection: Any, msg: dict[str, Any]
    ) -> None:
        """The week's scoring history, newest first, optionally filtered."""
        coordinator = _find_coordinator(hass, msg["league_id"])
        if coordinator is None:
            connection.send_error(msg["id"], "not_found", "No such league configured")
            return

        events = coordinator.feed.recent(
            msg.get("limit", 50),
            matchup_id=msg.get("matchup_id"),
            team_key=msg.get("team_key"),
            include_corrections=msg.get("include_corrections", True),
        )
        connection.send_result(
            msg["id"],
            {
                "week": coordinator.feed.week,
                "plays": [play_dict(e) for e in events],
            },
        )

    websocket_api.async_register_command(hass, handle_matchup_detail)
    websocket_api.async_register_command(hass, handle_play_history)
    hass.data[DOMAIN][_REGISTERED] = True
    _LOGGER.debug("Registered %s WebSocket commands", DOMAIN)
