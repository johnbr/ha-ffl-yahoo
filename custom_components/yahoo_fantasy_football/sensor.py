"""Sensor platform for Yahoo Fantasy Football.

Scaffold: publishes a single placeholder scoreboard entity so the integration
loads cleanly and the entity id is reserved. The real entity set — league
scoreboard, my-team, and a ``scoring_active`` binary sensor — arrives with the
coordinator.
"""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import MATCH_ALL
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_LEAGUE_KEY, CONF_NAME, DEFAULT_NAME, DOMAIN


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the scoreboard sensor for a league."""
    async_add_entities([YahooFantasyFootballScoreboardSensor(entry)])


class YahooFantasyFootballScoreboardSensor(SensorEntity):
    """League scoreboard entity.

    Live matchup data is a large, high-churn payload that is meaningless as
    history and would blow past the recorder's 16 KB attribute cap, so keep the
    whole attribute set out of the recorder.
    """

    _attr_icon = "mdi:football"
    _attr_has_entity_name = False
    _attr_should_poll = False
    _unrecorded_attributes = frozenset({MATCH_ALL})

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._league_key = str(entry.data.get(CONF_LEAGUE_KEY, ""))
        name = str(entry.data.get(CONF_NAME) or DEFAULT_NAME)

        self._attr_unique_id = f"{entry.entry_id}_scoreboard"
        self._attr_name = f"{name} Scoreboard"
        # sensor.<slug>_scoreboard — derived from the league id so multiple
        # leagues don't collide.
        self._attr_suggested_object_id = f"ffl_{self._league_key.replace('.', '_')}_scoreboard"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=name,
            manufacturer="Yahoo",
            model="Fantasy Football League",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def native_value(self) -> str:
        """Low-churn state so HA history isn't spammed."""
        return "idle"

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "league_key": self._league_key,
            "week": None,
            "matchups": [],
            "last_play": None,
            "recent_plays": [],
        }
