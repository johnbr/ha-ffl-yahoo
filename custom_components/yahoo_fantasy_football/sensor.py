"""Sensor platform for Yahoo Fantasy Football.

Two entities per league: the whole-league scoreboard, and the configured team's
matchup. Both keep their **state** low-churn and put the live payload in
attributes, which are excluded from the recorder — a live scoreboard is a large,
fast-changing blob that is meaningless as history and would blow past the
recorder's attribute cap.

Entity ids come from the entity **name**, which is Home Assistant's own rule:
a league called "Kush" yields ``sensor.kush_scoreboard``. There is no
``_attr_suggested_object_id`` override here because there cannot be one —
``Entity.suggested_object_id`` is a read-only property computed from ``name``,
so assigning ``_attr_suggested_object_id`` sets an attribute nothing ever
reads. This module had exactly that dead line until 2026-09-09, and the README
documented the ids it was supposed to produce, which never existed. Forcing an
id needs ``async_generate_entity_id`` and a rename of anything already
registered; the natural name is better anyway, and users can rename in the UI.

Full rosters and the complete play history are deliberately *not* in attributes.
Attributes are pushed to every connected client on every state change, so they
carry only what the resting card renders; the rest is served on demand.
"""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import MATCH_ALL
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_LEAGUE_ID, CONF_NAME, CONF_TEAM_ID, DEFAULT_NAME, DOMAIN
from .coordinator import YahooFantasyCoordinator
from .league_state import (
    find_team,
    nfl_games_attributes,
    nfl_games_state,
    play_dict,
    scoreboard_attributes,
    scoreboard_state,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the league's sensors."""
    coordinator: YahooFantasyCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]

    entities: list[SensorEntity] = [
        YahooScoreboardSensor(coordinator, entry),
        YahooNflGamesSensor(coordinator, entry),
    ]
    if entry.data.get(CONF_TEAM_ID):
        entities.append(YahooMyTeamSensor(coordinator, entry))
    async_add_entities(entities)


class _LeagueEntity(CoordinatorEntity[YahooFantasyCoordinator], SensorEntity):
    """Shared device identity and recorder policy."""

    _attr_has_entity_name = False
    _unrecorded_attributes = frozenset({MATCH_ALL})

    def __init__(self, coordinator: YahooFantasyCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._league_id = str(entry.data.get(CONF_LEAGUE_ID, ""))
        self._league_name = str(entry.data.get(CONF_NAME) or DEFAULT_NAME)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=self._league_name,
            manufacturer="Yahoo",
            model="Fantasy Football League",
            entry_type=DeviceEntryType.SERVICE,
        )


class YahooScoreboardSensor(_LeagueEntity):
    """Every matchup in the league."""

    _attr_icon = "mdi:football"

    def __init__(self, coordinator: YahooFantasyCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_scoreboard"
        self._attr_name = f"{self._league_name} Scoreboard"

    @property
    def native_value(self) -> str:
        return scoreboard_state(self.coordinator.league_data)

    @property
    def extra_state_attributes(self) -> dict:
        return scoreboard_attributes(
            self.coordinator.league_data,
            self.coordinator.feed,
            self._league_id,
            self._league_name,
        )


class YahooNflGamesSensor(_LeagueEntity):
    """The week's real NFL slate, independent of anyone's roster.

    The relay's games feed covers the whole week whether or not a rostered
    player is involved, so this reports the league-wide picture that the
    fantasy scoreboard can only see through its own players.

    Its **state** is the number of games in progress — a handful of changes a
    day, which is what belongs in the recorder. The slate rides in attributes
    with the rest of the live payload.
    """

    _attr_icon = "mdi:scoreboard"

    def __init__(self, coordinator: YahooFantasyCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_nfl_games"
        self._attr_name = f"{self._league_name} NFL Games"

    @property
    def native_value(self) -> str:
        return nfl_games_state(self.coordinator.league_data)

    @property
    def extra_state_attributes(self) -> dict:
        return nfl_games_attributes(
            self.coordinator.league_data,
            self._league_id,
            self.coordinator.nfl_last_plays,
        )


class YahooMyTeamSensor(_LeagueEntity):
    """The configured team's own matchup.

    Unlike the scoreboard, this one's state *is* the live score: it is the
    number worth graphing, and it belongs to a single team, so the write rate is
    one row per refresh rather than one per team.
    """

    _attr_icon = "mdi:account-group"
    _attr_native_unit_of_measurement = "pts"

    def __init__(self, coordinator: YahooFantasyCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._team_id = str(entry.data.get(CONF_TEAM_ID, ""))
        self._attr_unique_id = f"{entry.entry_id}_my_team"
        self._attr_name = f"{self._league_name} My Team"

    @property
    def _found(self) -> dict | None:
        return find_team(self.coordinator.league_data, self._team_id)

    @property
    def native_value(self) -> float | None:
        found = self._found
        return found["me"]["points"] if found else None

    @property
    def extra_state_attributes(self) -> dict:
        found = self._found
        if not found:
            return {"league_id": self._league_id, "team_id": self._team_id, "matchup_id": None}
        last = self.coordinator.feed.last_play(matchup_id=found["matchup_id"])
        return {
            # The card pairs this sensor with its league's scoreboard by
            # matching attributes, never by entity-id prefix — ids follow the
            # league NAME, so a prefix match breaks on every league but one.
            "league_id": self._league_id,
            "team_id": self._team_id,
            "matchup_id": found["matchup_id"],
            "team_name": found["me"]["name"],
            "projected": found["me"]["projected"],
            "opponent": found["opponent"]["name"],
            "opponent_points": found["opponent"]["points"],
            "opponent_projected": found["opponent"]["projected"],
            "winning": found["winning"],
            "last_play": play_dict(last) if last else None,
        }
