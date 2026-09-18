"""Fetch Yahoo's GameChannel tier, with the failure handling that needs.

Parsing lives in :mod:`yahoo_redzone`; this module is only about *getting* the
payloads and deciding what to do when that goes wrong. The network call is
**injected** as a coroutine, so all of it is exercised in tests with a fake
fetcher and none of it needs aiohttp or a live Yahoo.

Why this replaced the HTML scraper
----------------------------------
:mod:`web_client` reads Yahoo's public fantasy *web pages*. That works only for
leagues whose privacy setting is public, and it costs one request per matchup.
This source is strictly better on both counts:

* **Three requests per poll for the whole league**, regardless of size — the
  league seed plus two league-wide live feeds — against 1 + one-per-matchup.
* **Private leagues work.** Verified against one. There is no login and no
  token involved; this is the same anonymous tier the GameChannel page uses.

The error taxonomy is deliberately shared with :mod:`web_client` so the
coordinator did not have to learn a second one. :class:`LeagueIsPrivate` is
kept importable from here but is never raised: privacy is simply not a
condition this tier has.

Politeness
----------
This is still a scraper against a site that did not invite us. Cadence is the
coordinator's job, but the shape is bounded here: three requests per refresh
plus one *conditional* request per live game's play-by-play, no per-matchup
fan-out, and no browser impersonation beyond a single ``User-Agent``. The
endpoints answer without one — it is sent so the traffic is identifiable rather
than to evade anything. If Yahoo starts refusing, the correct response is to
fetch less, not to evade harder.

The play feeds are the one thing polled every refresh, and they are polled with
``If-Modified-Since`` because the relay honours it (verified 2026-09-17: a real
``Last-Modified``, ``304`` on an unchanged file). A ``304`` is a couple of
hundred bytes, so asking every 10 s costs less than the old scheme of fetching
the whole feed whenever the games feed *looked* like a play had run — and it
stops missing plays. The games feed reflects a snap 2-30 s before the play's
text reaches the play feed (measured live, six plays), so a fetch triggered by
the games feed was usually too early, and nothing triggered a second one until
the next snap: the last-play line ran a play behind, and a touchdown whose text
landed between the score change and the PAT was never shown at all.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .web_client import (  # noqa: F401  (LeagueIsPrivate re-exported for callers)
    STALE_AFTER_SECONDS,
    FetchFailed,
    LeagueData,
    LeagueIsPrivate,
    YahooWebError,
)
from .yahoo_redzone import (
    league_from_payloads,
    league_name,
    parse_relay_players,
    redzone_url,
    relay_url,
    team_choices,
)

_LOGGER = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

# ``(url) -> body``. Unlike the HTML tier there is no login bounce to detect,
# so the fetcher does not have to report where it landed.
Fetcher = Callable[[str], Awaitable[str]]

# ``(url, if_modified_since) -> (body, last_modified)``, with ``body`` ``None``
# when the server answered ``304 Not Modified``. Optional: the config flow and
# the tests that only need the seed get by on the plain fetcher, and without
# this one the play feeds are simply fetched whole every time.
ConditionalFetcher = Callable[[str, str | None], Awaitable[tuple[str | None, str]]]

# A redzone payload for a real league runs to ~190 KB; the relay feeds are
# small but never empty, since each carries a comment header. Anything under
# this is a truncated response or an error page, not data.
MIN_BODY = 40

# How long the league seed is reused between refreshes.
#
# The three payloads are wildly different sizes — measured against a real
# league: seed 178 KB, stats 5.7 KB, games 2.1 KB — and they change at wildly
# different rates. The seed carries rosters, projections and league settings,
# which move when somebody edits a lineup; the two relays carry every number
# that changes during a game. Refetching the seed on the live cadence was
# spending 96% of the bandwidth on the part that had not changed, and that
# cost is what kept the live cadence slow.
#
# Caching it decouples the two: the live feeds can be polled several times a
# minute while the seed is refreshed every few minutes. The cost is that a
# lineup change takes up to this long to appear, which is the right trade
# during a game — points move constantly, lineups do not.
SEED_TTL_SECONDS = 180.0

# How long the NFL player dictionary is reused.
#
# It answers "who is [42654]", which changes when somebody is signed, not
# during a game. Refetching 34 KB of it on every poll to learn nothing would
# undo the point of caching the seed.
PLAYERS_TTL_SECONDS = 1800.0

# How often a play feed is fetched whole regardless of ``If-Modified-Since``.
#
# ``Last-Modified`` has one-second resolution and comes from whichever origin
# answered, so a ``304`` is trusted only for this long before an unconditional
# fetch confirms it. Cheap insurance: one full (gzip, ~5 KB) fetch per live
# game every minute and a half.
PLAYS_RECHECK_SECONDS = 90.0

# The floor between two asks for the same play feed. The coordinator asks once
# per poll and the games card asks again for each expanded game a moment later;
# the second ask is answered from the first's result rather than with a second
# round trip.
PLAYS_MIN_INTERVAL_SECONDS = 5.0


@dataclass
class _PlaysCache:
    """One game's play feed as last seen, and when."""

    body: str
    last_modified: str
    fetched_at: float
    """Last time the server was asked, whether it answered 200 or 304."""
    loaded_at: float
    """Last time a full body arrived."""


class RedzoneClient:
    """Read one league over Yahoo's anonymous GameChannel tier."""

    def __init__(
        self,
        fetch: Fetcher,
        league_id: str | int,
        *,
        sport: str = "nfl",
        seed_ttl: float = SEED_TTL_SECONDS,
        fetch_if_modified: ConditionalFetcher | None = None,
    ) -> None:
        self._fetch = fetch
        self._fetch_if_modified = fetch_if_modified
        self.league_id = str(league_id)
        self.sport = sport
        self._seed_ttl = seed_ttl
        self._last_good: LeagueData | None = None
        # Parsed, not raw: the seed is re-read on every refresh, and parsing
        # 178 KB of JSON several times a minute in the event loop is a cost
        # worth paying exactly once per fetch.
        self._seed: dict | None = None
        self._seed_at: float = 0.0
        self._players: dict[str, str] | None = None
        self._players_at: float = 0.0
        self._plays: dict[str, _PlaysCache] = {}

    # -- internals ---------------------------------------------------------

    async def _get(self, url: str) -> str:
        try:
            body = await self._fetch(url)
        except YahooWebError:
            raise
        except Exception as err:  # any transport error is transient
            raise FetchFailed(f"fetching {url}: {err}") from err

        if not body or len(body) < MIN_BODY:
            raise FetchFailed(f"suspiciously short response from {url} ({len(body or '')}B)")
        return body

    async def _get_optional(self, url: str) -> str:
        """A live feed that may legitimately be missing.

        Between seasons, and in the hours before the first game of a week,
        Yahoo serves these empty or not at all. That is not a failure — it
        means nobody has scored yet, and the league seed alone still renders a
        complete scoreboard of zeroes.
        """
        try:
            return await self._get(url)
        except YahooWebError as err:
            _LOGGER.debug("live feed %s unavailable: %s", url, err)
            return ""

    # -- public API --------------------------------------------------------

    async def async_fetch_seed(self) -> str:
        """The league seed on its own — used by the config flow to validate."""
        return await self._get(redzone_url(self.league_id, self.sport))

    async def async_league_name(self) -> str:
        return league_name(await self.async_fetch_seed(), self.league_id)

    async def async_team_choices(self) -> dict[str, str]:
        """``{team_id: name}`` for the config flow's team picker."""
        return team_choices(await self.async_fetch_seed(), self.league_id)

    async def _seed_payload(self, now: float) -> dict:
        """The league seed, refetched only once it has aged out.

        See :data:`SEED_TTL_SECONDS` for why this is cached at all.
        """
        if self._seed is not None and (now - self._seed_at) < self._seed_ttl:
            return self._seed
        body = await self._get(redzone_url(self.league_id, self.sport))
        try:
            payload = json.loads(body)
        except ValueError as err:
            raise FetchFailed(f"league {self.league_id} seed is not JSON: {err}") from err
        self._seed, self._seed_at = payload, now
        return payload

    async def async_players(self, now: float) -> dict[str, str]:
        """``{player_id: name}`` for everyone in today's games, cached.

        Best-effort: a play description with unresolved ids degrades to a
        shorter sentence, which is a far better outcome than failing a refresh
        over it.
        """
        if self._players is not None and (now - self._players_at) < PLAYERS_TTL_SECONDS:
            return self._players
        body = await self._get_optional(relay_url("players", self.sport))
        if not body:
            return self._players or {}
        self._players, self._players_at = parse_relay_players(body), now
        return self._players

    async def async_plays(self, plays_id: str, now: float) -> str:
        """One game's play-by-play feed, or ``""`` if it is not being served.

        This is the feed whose *text* changes — Yahoo posts a terse description
        first and revises it in place — so it is never served from a cache by
        age. It is served from the cache when the server says the file has
        not changed (``304``), which is what makes asking on every poll
        affordable. See the module docstring for why every poll.

        ``now`` is the poll's timestamp. Two callers in one poll — the fantasy
        enrichment and the games card's last play — share one request.
        """
        if not plays_id:
            return ""
        url = relay_url(f"plays-{plays_id}", self.sport)
        if self._fetch_if_modified is None:
            return await self._get_optional(url)

        cached = self._plays.get(plays_id)
        if cached is not None and now - cached.fetched_at < PLAYS_MIN_INTERVAL_SECONDS:
            return cached.body

        since = None
        if cached is not None and now - cached.loaded_at < PLAYS_RECHECK_SECONDS:
            since = cached.last_modified or None
        try:
            body, last_modified = await self._fetch_if_modified(url, since)
        except Exception as err:  # a missing feed is cosmetic, never fatal
            _LOGGER.debug("live feed %s unavailable: %s", url, err)
            return cached.body if cached is not None else ""

        if body is None:  # 304: what we have is what there is
            if cached is None:
                return ""
            cached.fetched_at = now
            return cached.body
        if len(body) < MIN_BODY:
            _LOGGER.debug("suspiciously short response from %s (%dB)", url, len(body))
            return cached.body if cached is not None else ""
        self._plays[plays_id] = _PlaysCache(body, last_modified, now, now)
        return body

    async def async_refresh(self, now: float) -> LeagueData:
        """One poll: the two live feeds, plus the league seed when it is due.

        ``now`` is supplied by the caller — nothing in this module reads a
        clock, which keeps refresh behaviour deterministic under test.
        """
        # Concurrent, not sequential: three independent endpoints, and this
        # runs inside the refresh the card is waiting on, so serialising them
        # was three round trips of pure latency on every live poll. The seed is
        # usually a cache hit and returns without any IO at all.
        seed, stats, games = await asyncio.gather(
            self._seed_payload(now),
            self._get_optional(relay_url("stats", self.sport)),
            self._get_optional(relay_url("games", self.sport)),
        )

        try:
            data = league_from_payloads(seed, stats, games, self.league_id, now)
        except (ValueError, KeyError, TypeError) as err:
            # A seed that will not parse into a league is worse than no cache:
            # every subsequent poll would fail the same way until the TTL ran
            # out. Drop it so the next refresh fetches a fresh one.
            self._seed = None
            raise FetchFailed(f"could not read league {self.league_id}: {err}") from err

        if not data.standings:
            raise FetchFailed(f"no matchups in league {self.league_id}'s week {data.week}")

        self._last_good = data
        return data

    async def async_refresh_or_stale(self, now: float) -> LeagueData:
        """Refresh, falling back to the last good payload on a transient failure.

        Serving four-minute-old scores beats emptying the card mid-game; past
        :data:`STALE_AFTER_SECONDS` it stops pretending and raises.
        """
        try:
            return await self.async_refresh(now)
        except YahooWebError as err:
            stale = self._last_good
            if stale is None:
                raise
            age = now - stale.fetched_at
            if age > STALE_AFTER_SECONDS:
                raise FetchFailed(f"no fresh data for {age:.0f}s: {err}") from err
            _LOGGER.debug("serving %.0fs-old data after a failed refresh: %s", age, err)
            return stale
