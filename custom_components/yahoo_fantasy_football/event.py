"""Your matchup's moments as an event entity, for automations.

One entity per league with a configured team: ``my_player_scored``,
``opponent_scored`` and ``lead_change``. It is fed from this entry's own bus
events (see :mod:`automation`) rather than from the coordinator, so it reports
exactly what an automation listening to the bus would see.

Without a team there is no "mine", so there is no entity; the bus events still
fire for every scoring play in the league.
"""

from __future__ import annotations

from collections import deque

from homeassistant.components.event import EventEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .automation import EVENT_TYPE_LEAD_CHANGE, EVENT_TYPES, entity_event_type
from .const import CONF_NAME, CONF_TEAM_ID, DEFAULT_NAME, DOMAIN, EVENT_LEAD_CHANGE, EVENT_SCORING_PLAY

# A play that lands in pieces is re-fired on the bus under its first id with
# the running total. The entity fires once per play; this is how many ids it
# remembers to tell a later piece from a new play — far more than one poll adds.
_SEEN_PLAYS = 500


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the matchup event entity when a team is configured."""
    if entry.data.get(CONF_TEAM_ID):
        async_add_entities([YahooMatchupEvent(entry)])


class YahooMatchupEvent(EventEntity):
    """Scoring plays and lead changes in the configured team's matchup."""

    _attr_has_entity_name = False
    _attr_icon = "mdi:football"
    _attr_event_types = EVENT_TYPES
    _attr_translation_key = "my_matchup"

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry_id = entry.entry_id
        league_name = str(entry.data.get(CONF_NAME) or DEFAULT_NAME)
        self._attr_unique_id = f"{entry.entry_id}_my_matchup"
        self._attr_name = f"{league_name} My Matchup"
        # Same identifiers as the sensors, so this joins the league's device.
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=league_name,
            manufacturer="Yahoo",
            model="Fantasy Football League",
            entry_type=DeviceEntryType.SERVICE,
        )
        self._seen: deque[str] = deque(maxlen=_SEEN_PLAYS)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self.hass.bus.async_listen(EVENT_SCORING_PLAY, self._on_scoring_play))
        self.async_on_remove(self.hass.bus.async_listen(EVENT_LEAD_CHANGE, self._on_lead_change))

    @callback
    def _on_scoring_play(self, event: Event) -> None:
        if event.data.get("entry_id") != self._entry_id:
            return
        event_type = entity_event_type(event.data)
        if event_type is None:
            return
        play_id = event.data.get("event_id")
        if play_id in self._seen:
            return
        self._seen.append(play_id)
        self._trigger_event(event_type, _without_entry(event.data))
        self.async_write_ha_state()

    @callback
    def _on_lead_change(self, event: Event) -> None:
        if event.data.get("entry_id") != self._entry_id:
            return
        self._trigger_event(EVENT_TYPE_LEAD_CHANGE, _without_entry(event.data))
        self.async_write_ha_state()


def _without_entry(data: dict) -> dict:
    """The payload as attributes, less the routing id the entity already is."""
    return {key: value for key, value in data.items() if key != "entry_id"}
