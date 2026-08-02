"""Polling coordinator for a league.

Deliberately thin. Every decision worth testing — what to fetch, how to degrade,
what the entities see, how often to poll — lives in :mod:`web_client`,
:mod:`league_state` and :mod:`plays`, which are pure and unit-tested. This file
is the Home Assistant plumbing around them, because HA machinery cannot be
exercised in this repo's test harness.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    CONF_LEAGUE_ID,
    CONF_SEASON,
    DOMAIN,
    EVENT_SCORING_PLAY,
)
from .league_state import play_dict, poll_interval
from .plays import PlayFeed, diff_snapshots
from .web_client import USER_AGENT, LeagueData, LeagueIsPrivate, YahooWebClient, YahooWebError
from .yahoo_web import to_snapshot

_LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)
STORAGE_VERSION = 1


class YahooFantasyCoordinator(DataUpdateCoordinator[LeagueData]):
    """Fetch a public league on an adaptive cadence and derive scoring plays."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.entry = entry
        self.league_id = str(entry.data.get(CONF_LEAGUE_ID, ""))
        season = entry.data.get(CONF_SEASON)
        self.season = int(season) if season else None

        session = async_get_clientsession(hass)

        async def _fetch(url: str) -> tuple[str, str]:
            # The final URL is what reveals a login bounce, so redirects are
            # followed and the landing URL reported back.
            async with session.get(
                url,
                headers={"User-Agent": USER_AGENT},
                timeout=REQUEST_TIMEOUT,
                allow_redirects=True,
            ) as resp:
                return await resp.text(), str(resp.url)

        self.client = YahooWebClient(_fetch, self.league_id, self.season)
        self.feed = PlayFeed()
        self._previous = None
        self._store: Store = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}.plays")

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} {self.league_id}",
            update_interval=None,  # set from the data after every refresh
        )

    async def async_load_history(self) -> None:
        """Restore the play feed so a mid-Sunday restart keeps its history.

        Corrupt or unreadable history is not worth failing setup over — the feed
        simply starts empty and refills from the next poll.
        """
        try:
            stored = await self._store.async_load()
        except Exception as err:  # history is best-effort
            _LOGGER.debug("Could not load stored plays: %s", err)
            return
        if stored:
            self.feed = PlayFeed.from_dict(stored)

    async def _async_save_history(self) -> None:
        try:
            await self._store.async_save(self.feed.to_dict())
        except Exception as err:  # never fail a refresh over history
            _LOGGER.debug("Could not persist plays: %s", err)

    async def _async_update_data(self) -> LeagueData:
        now = dt_util.utcnow().timestamp()
        try:
            data = await self.client.async_refresh_or_stale(now)
        except LeagueIsPrivate as err:
            # Permanent: retrying cannot help, and the user needs to know why.
            raise UpdateFailed(str(err)) from err
        except YahooWebError as err:
            raise UpdateFailed(str(err)) from err

        self._process_plays(data, now)
        self.update_interval = _interval(data)
        return data

    def _process_plays(self, data: LeagueData, now: float) -> None:
        """Diff against the previous poll and publish anything new."""
        if not data.matchups:
            return

        snapshot = to_snapshot(data.matchups, data.week, now, self.league_id)

        if self.feed.week not in (None, data.week):
            # A new week starts a new history rather than appending to the last.
            self.feed.clear()
            self._previous = None

        events = diff_snapshots(self._previous, snapshot)
        self._previous = snapshot
        if not events:
            return

        for event in self.feed.add(events):
            self.hass.bus.async_fire(EVENT_SCORING_PLAY, play_dict(event))
        self.hass.async_create_task(self._async_save_history())

    @property
    def league_data(self) -> LeagueData | None:
        return self.data if isinstance(self.data, LeagueData) else None


def _interval(data: LeagueData | None) -> timedelta:
    return timedelta(seconds=poll_interval(data))


def coordinator_for(hass: HomeAssistant, entry_id: str) -> YahooFantasyCoordinator | None:
    """Look up a league's coordinator, for the WebSocket commands."""
    data: dict[str, Any] = hass.data.get(DOMAIN, {})
    entry = data.get(entry_id)
    if isinstance(entry, dict):
        found = entry.get("coordinator")
        if isinstance(found, YahooFantasyCoordinator):
            return found
    return None
