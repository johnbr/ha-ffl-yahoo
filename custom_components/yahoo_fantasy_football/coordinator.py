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
from .yahoo_redzone import humanize_play, parse_relay_plays, to_snapshot

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

        async def _fetch_if_modified(url: str, since: str | None) -> tuple[str | None, str]:
            headers = {"User-Agent": USER_AGENT}
            if since:
                headers["If-Modified-Since"] = since
            async with session.get(url, headers=headers, timeout=REQUEST_TIMEOUT) as resp:
                if resp.status == 304:
                    return None, since or ""
                resp.raise_for_status()
                return await resp.text(), resp.headers.get("Last-Modified", "")

        self.client = RedzoneClient(
            _fetch, self.league_id, fetch_if_modified=_fetch_if_modified
        )
        self.feed = PlayFeed()
        self._previous = None
        self._store: Store = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}.plays")
        # {plays_id: (feed body it was rendered from, rows)} — the games card's
        # lists, re-rendered only when the feed actually changed.
        self._nfl_plays: dict[str, tuple[str, list[dict[str, Any]]]] = {}
        # {plays_id: newest play text} for live games, refreshed every poll.
        self._nfl_last_plays: dict[str, str] = {}
        # {plays_id: newest play sequence} as of the END of the last poll. A
        # scoring event raised this poll can only have come from a play above
        # it — see ``ScoringEvent.play_floor``.
        self._plays_floor: dict[str, int] = {}

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
        await self._refresh_nfl_last_plays(data, now)
        self.update_interval = _interval(data)
        return data

    async def _refresh_nfl_last_plays(self, data: LeagueData, now: float) -> None:
        """Newest play text for each LIVE NFL game, for the games card.

        Live games only. A finished game's last play is a snapshot nobody is
        watching change, and a scheduled one has none — fetching either would
        be paying for a line that will never move.

        Every live game, every poll. The play feed is the only way to know a
        play's text has landed: the games feed reflects the snap 2-30 s before
        the text follows (measured live 2026-09-17), so a fetch cued by the
        games feed was too early more often than not, and the line then sat a
        play behind until the next snap cued another. The client answers an
        unchanged feed with a ``304``, which is what makes every poll cheap.

        Every failure is swallowed: a missing line is cosmetic where a failed
        refresh would blank the scores.
        """
        live = [
            str(game.plays_id)
            for game in getattr(data, "nfl_games", [])
            if getattr(game, "state", "") == "in" and getattr(game, "plays_id", "")
        ][:MAX_NFL_LAST_PLAY_FEEDS]
        if not live:
            self._nfl_last_plays = {}
            return

        results = await asyncio.gather(
            *(self.async_game_plays(plays_id, 1, now=now) for plays_id in live),
            return_exceptions=True,
        )
        last_plays: dict[str, str] = {}
        for plays_id, result in zip(live, results, strict=True):
            if isinstance(result, BaseException) or not result:
                # Keep the line we had rather than blanking it for one bad poll.
                if plays_id in self._nfl_last_plays:
                    last_plays[plays_id] = self._nfl_last_plays[plays_id]
                continue
            last_plays[plays_id] = result[0].get("text", "")
            sequence = _sequence(result[0].get("play_id", ""))
            if sequence is not None:
                self._plays_floor[plays_id] = sequence
        self._nfl_last_plays = last_plays

    @property
    def nfl_last_plays(self) -> dict[str, str]:
        """``{plays-feed id: newest play}`` for games in progress."""
        return self._nfl_last_plays

    async def _process_plays(self, data: LeagueData, now: float) -> None:
        """Diff against the previous poll and publish anything new."""
        if not data.matchups:
            return

        snapshot = to_snapshot(data.matchups, data.week, now, self.league_id)

        if self.feed.start_week(data.week):
            # A new week starts a new history rather than appending to the last.
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

        # A new event's play is usually NOT in the feed yet (the stat feed
        # runs 15-20 s ahead of the play text), so it is told where the feed
        # stood at the end of the last poll and matches only above that.
        events = [
            replace(event, play_floor=self._plays_floor[feed_id])
            if (feed_id := data.plays_feeds.get(event.nfl_team or "")) in self._plays_floor
            else event
            for event in events
        ]

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
            *(self.client.async_plays(feed_id, now) for feed_id in ids),
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

    async def async_game_plays(
        self, plays_id: str, limit: int = 12, now: float | None = None
    ) -> list[dict[str, Any]]:
        """Recent plays for ONE NFL game, newest first.

        Lives here rather than in :mod:`websocket` because it reaches upstream
        and caches, and that module's whole contract is that it does neither.

        The client decides whether anything is fetched (see ``async_plays``);
        this only re-renders when the body it is handed is not the one the rows
        were rendered from, so a ``304`` costs no parsing either.

        Returns ``[]`` rather than raising: an unreadable feed should leave the
        expanded game empty, not fail the card.
        """
        if not plays_id:
            return []
        if now is None:
            now = dt_util.utcnow().timestamp()
        cached = self._nfl_plays.get(plays_id)

        try:
            names = await self.client.async_players(now)
            body = await self.client.async_plays(plays_id, now)
        except Exception as err:  # a missing feed is not worth failing over
            _LOGGER.debug("Could not read plays for NFL game %s: %s", plays_id, err)
            return cached[1][:limit] if cached else []
        if cached and cached[0] == body:
            return cached[1][:limit]

        rows: list[dict[str, Any]] = []
        for play in reversed(parse_relay_plays(body)):  # newest first, the way a reader scans
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
        self._nfl_plays[plays_id] = (body, rows)
        return rows[:limit]

    @property
    def league_data(self) -> LeagueData | None:
        return self.data if isinstance(self.data, LeagueData) else None


def _interval(data: LeagueData | None) -> timedelta:
    return timedelta(seconds=poll_interval(data))


def _sequence(play_id: str) -> int | None:
    """The play number out of a ``game_key.sequence`` id."""
    try:
        return int(play_id.rsplit(".", 1)[-1])
    except (TypeError, ValueError):
        return None


def coordinator_for(hass: HomeAssistant, entry_id: str) -> YahooFantasyCoordinator | None:
    """Look up a league's coordinator, for the WebSocket commands."""
    data: dict[str, Any] = hass.data.get(DOMAIN, {})
    entry = data.get(entry_id)
    if isinstance(entry, dict):
        found = entry.get("coordinator")
        if isinstance(found, YahooFantasyCoordinator):
            return found
    return None
