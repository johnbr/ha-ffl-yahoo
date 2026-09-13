"""Polling coordinator for a league.

Deliberately thin. Every decision worth testing — what to fetch, how to degrade,
what the entities see, how often to poll — lives in :mod:`redzone_client`,
:mod:`league_state` and :mod:`plays`, which are pure and unit-tested. This file
is the Home Assistant plumbing around them, because HA machinery cannot be
exercised in this repo's test harness.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
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
from .plays import PlayFeed, ScoringEvent, diff_snapshots, match_relay_play
from .redzone_client import USER_AGENT, RedzoneClient
from .web_client import LeagueData, LeagueIsPrivate, YahooWebError
from .yahoo_redzone import parse_relay_plays, to_snapshot

_LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)
STORAGE_VERSION = 1

# How long an event keeps being re-matched against its game's play feed.
#
# Yahoo posts a play tersely first — "Dak Prescott complete for 29 yards" — and
# fills in the receiver a moment later. Re-reading the feed for a few minutes
# after an event is what lets the better wording replace the worse one on a
# card that is already showing it. Long enough to catch the revision, short
# enough that a Sunday's whole history is not re-read every 20 seconds.
PLAY_REVISION_SECONDS = 300.0

# Ceiling on play feeds fetched per refresh. One per live game with a scoring
# event, and on the busiest Sunday afternoon that is still a handful — but the
# cap is what guarantees it, since this is a scraper and the cost has to be
# bounded by construction rather than by expectation.
MAX_PLAY_FEEDS = 8


class YahooFantasyCoordinator(DataUpdateCoordinator[LeagueData]):
    """Fetch a league on an adaptive cadence and derive scoring plays.

    Reads Yahoo's anonymous GameChannel tier (:mod:`redzone_client`), which
    serves private leagues as well as public ones and costs three requests per
    poll regardless of league size.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.entry = entry
        self.league_id = str(entry.data.get(CONF_LEAGUE_ID, ""))
        season = entry.data.get(CONF_SEASON)
        self.season = int(season) if season else None

        session = async_get_clientsession(hass)

        async def _fetch(url: str) -> str:
            async with session.get(
                url, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT
            ) as resp:
                resp.raise_for_status()
                return await resp.text()

        self.client = RedzoneClient(_fetch, self.league_id)
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
            # This tier has no such condition, but the taxonomy is shared with
            # the HTML source, so the branch stays rather than becoming a
            # surprise traceback if that ever changes.
            raise UpdateFailed(str(err)) from err
        except YahooWebError as err:
            raise UpdateFailed(str(err)) from err

        await self._process_plays(data, now)
        self.update_interval = _interval(data)
        return data

    async def _process_plays(self, data: LeagueData, now: float) -> None:
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

        # Events already on the card are re-matched too, not just new ones —
        # that is the whole point of the revision window.
        revisable = [
            event
            for event in self.feed.recent(50, include_corrections=True)
            if now - event.timestamp <= PLAY_REVISION_SECONDS
        ]
        if events or revisable:
            events = await self._describe(data, events, revisable, now)
        if not events:
            return

        for event in self.feed.add(events):
            self.hass.bus.async_fire(EVENT_SCORING_PLAY, play_dict(event))
        self.hass.async_create_task(self._async_save_history())

    async def _describe(
        self,
        data: LeagueData,
        events: list[ScoringEvent],
        revisable: list[ScoringEvent],
        now: float,
    ) -> list[ScoringEvent]:
        """Attach Yahoo's own play description to events, and refresh old ones.

        New events are enriched BEFORE they are stored, so the text that goes
        out on the event bus is the text the card will show — an automation
        announcing "Nacua +1.60" when the card says "Sam Darnold passed to Puka
        Nacua for 11 yard gain" would be the same play told two ways.

        Every failure here is swallowed: a missing description is a cosmetic
        loss, and losing the scoring event itself over one would not be.
        """
        wanted: dict[str, str] = {}
        for event in [*events, *revisable]:
            feed_id = data.plays_feeds.get(event.nfl_team or "")
            if feed_id:
                wanted[feed_id] = feed_id
        if not wanted:
            return events

        try:
            names = await self.client.async_players(now)
        except Exception as err:  # cosmetic, never fatal
            _LOGGER.debug("Could not read the player dictionary: %s", err)
            return events

        # Fetched concurrently: these are different games and nothing here
        # depends on another's result, so paying eight round trips end to end
        # would be latency added to the scores themselves — this runs inside
        # the refresh the entity is waiting on.
        ids = list(wanted)[:MAX_PLAY_FEEDS]
        results = await asyncio.gather(
            *(self.client.async_plays(feed_id) for feed_id in ids),
            return_exceptions=True,
        )
        feeds: dict[str, list] = {}
        for feed_id, result in zip(ids, results, strict=True):
            if isinstance(result, BaseException):  # cosmetic, never fatal
                _LOGGER.debug("Could not read plays for game %s: %s", feed_id, result)
                continue
            try:
                feeds[feed_id] = parse_relay_plays(result)
            except Exception as err:  # cosmetic, never fatal
                _LOGGER.debug("Could not parse plays for game %s: %s", feed_id, err)

        def described(event: ScoringEvent) -> ScoringEvent:
            plays = feeds.get(data.plays_feeds.get(event.nfl_team or "", ""))
            if not plays:
                return event
            # An event that already matched is re-read for BETTER WORDING of
            # the same play, never re-picked — see ``match_relay_play``. A new
            # event has nothing to pin to and takes the newest-wins path.
            pin = event.plays[-1].play_id if event.plays else None
            match = match_relay_play(event, plays, names, pin=pin)
            if match is None:
                return event
            return event if event.plays and event.plays[-1] == match else replace(
                event, plays=(match,)
            )

        for event in revisable:
            updated = described(event)
            if updated is not event:
                self.feed.revise(event.event_id, updated.plays)
        return [described(event) for event in events]

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
