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

# How long an on-demand NFL play list is reused before being refetched.
#
# A game's plays feed is ~20 KB, so the games CARD cannot have them pushed to
# it — a full Sunday would be a quarter of a megabyte per poll to render one
# line per game. They are fetched only when a reader expands a game, and this
# TTL keeps a card that repaints on every 10s poll from refetching each time.
# Raised from 25s when the games card started showing a last play for every
# live game rather than only for one a reader had expanded. One cache serves
# both, so the TTL is what bounds the cost of the always-on line:
#
#   ~20 KB a game x 13 live games on a Sunday = ~260 KB a refresh.
#   At 45s that is ~21 MB/hr; on the 10s poll it would have been ~93 MB/hr.
#
# A play lands every 30-40s of real time, so 45s rarely skips one, and the
# expanded list is at most this stale before the next poll refreshes it.
NFL_PLAYS_TTL_SECONDS = 45.0

# Ceiling on last-play feeds per refresh. Sixteen is the whole week's slate, so
# this cannot silently drop a game — it is a runaway guard, not a sample.
MAX_NFL_LAST_PLAY_FEEDS = 16


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
        # {plays_id: (fetched_at, rows)} for the games card's on-demand lists.
        self._nfl_plays: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        # {plays_id: newest play text} for live games, refreshed every poll.
        self._nfl_last_plays: dict[str, str] = {}

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
        await self._refresh_nfl_last_plays(data)
        self.update_interval = _interval(data)
        return data

    async def _refresh_nfl_last_plays(self, data: LeagueData) -> None:
        """Newest play text for each LIVE NFL game, for the games card.

        Live games only. A finished game's last play is a snapshot nobody is
        watching change, and a scheduled one has none — fetching either would
        be paying for a line that will never move.

        Shares :meth:`async_game_plays`' cache, so a game a reader has expanded
        is not fetched twice, and every failure is swallowed: a missing line is
        cosmetic where a failed refresh would blank the scores.
        """
        live = [
            str(game.plays_id)
            for game in getattr(data, "nfl_games", [])
            if getattr(game, "state", "") == "in" and getattr(game, "plays_id", "")
        ]
        if not live:
            self._nfl_last_plays = {}
            return

        results = await asyncio.gather(
            *(self.async_game_plays(plays_id, 1) for plays_id in live[:MAX_NFL_LAST_PLAY_FEEDS]),
            return_exceptions=True,
        )
        latest: dict[str, str] = {}
        for plays_id, result in zip(live, results, strict=False):
            if isinstance(result, BaseException) or not result:
                continue
            latest[plays_id] = result[0].get("text", "")
        self._nfl_last_plays = latest

    @property
    def nfl_last_plays(self) -> dict[str, str]:
        """``{plays-feed id: newest play}`` for games in progress."""
        return self._nfl_last_plays

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

    async def async_game_plays(self, plays_id: str, limit: int = 12) -> list[dict[str, Any]]:
        """Recent plays for ONE NFL game, newest first, fetched on demand.

        Lives here rather than in :mod:`websocket` because it reaches upstream
        and caches, and that module's whole contract is that it does neither.

        Returns ``[]`` rather than raising: an unreadable feed should leave the
        expanded game empty, not fail the card.
        """
        if not plays_id:
            return []
        now = dt_util.utcnow().timestamp()
        cached = self._nfl_plays.get(plays_id)
        if cached and now - cached[0] < NFL_PLAYS_TTL_SECONDS:
            return cached[1][:limit]

        try:
            names = await self.client.async_players(now)
            plays = parse_relay_plays(await self.client.async_plays(plays_id))
        except Exception as err:  # a missing feed is not worth failing over
            _LOGGER.debug("Could not read plays for NFL game %s: %s", plays_id, err)
            return cached[1][:limit] if cached else []

        from .yahoo_redzone import humanize_play

        rows: list[dict[str, Any]] = []
        for play in reversed(plays):  # newest first, the way a reader scans
            text = humanize_play(play.text, names)
            if not text:
                continue
            rows.append(
                {
                    "play_id": f"{play.game_key}.{play.sequence}",
                    "text": text,
                    "period": play.period,
                    "clock": play.clock,
                }
            )
        self._nfl_plays[plays_id] = (now, rows)
        return rows[:limit]

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
