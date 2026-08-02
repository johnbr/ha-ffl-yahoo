"""Fetch Yahoo's public fantasy pages, with the failure handling that needs.

The parsing lives in :mod:`yahoo_web`; this module is only about *getting* the
HTML and deciding what to do when that goes wrong. The network call itself is
**injected** as a coroutine, so everything here is exercised in tests with a
fake fetcher and none of it needs aiohttp or a live Yahoo.

Design notes that are not obvious
---------------------------------
* **A private league is permanent, not transient.** Yahoo redirects it to
  sign-in. Retrying costs requests and will never succeed, so it raises
  :class:`LeagueIsPrivate` and the caller is expected to stop, not back off.
* **Serve stale rather than blank.** A card that empties itself on one failed
  request is worse than one showing scores from four minutes ago, especially
  mid-game. The client keeps the last good payload and reports its age.
* **This is a scraper against a site that did not invite us.** Requests are
  therefore conservative and clearly bounded: one scoreboard fetch plus one per
  matchup, a hard cap on that count, and a single browser ``User-Agent`` as the
  only impersonation. Retry cadence is the coordinator's job — it backs off on
  failure and speeds up only when games are actually live. If Yahoo starts
  refusing, the correct response is to fetch less, not to evade harder.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from .yahoo_web import (
    WebMatchup,
    WebTeam,
    is_login_redirect,
    matchup_url,
    parse_current_week,
    parse_matchup,
    parse_scoreboard,
    scoreboard_url,
)

_LOGGER = logging.getLogger(__name__)

# A plain scripted request gets a different page (or none) from Yahoo. This is
# the minimum to be served the normal HTML, and deliberately the *only* piece of
# browser impersonation here — see the module docstring.
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

# Ceiling on matchup pages fetched per refresh. A 10-team league is 5, so this
# accommodates a 24-team league without ever becoming unbounded if a page
# starts reporting nonsense.
MAX_MATCHUP_FETCHES = 12

# Beyond this, cached data is too old to show as if it were live.
STALE_AFTER_SECONDS = 900.0


class YahooWebError(Exception):
    """Base class for this client's failures."""


class LeagueIsPrivate(YahooWebError):
    """The league is not public, so this data source can never serve it.

    Distinct from a transient error on purpose: the caller must surface this to
    the user and stop retrying, because no amount of retrying will help.
    """


class FetchFailed(YahooWebError):
    """A transient problem — network, timeout, or an unexpected page."""


# ``(url) -> (body, final_url)``. The final URL is what reveals a login bounce,
# so a fetcher that follows redirects MUST report where it landed.
Fetcher = Callable[[str], Awaitable[tuple[str, str]]]


@dataclass
class LeagueData:
    """One refresh's worth of league state."""

    week: int
    matchups: list[WebMatchup] = field(default_factory=list)
    standings: list[tuple[WebTeam, WebTeam]] = field(default_factory=list)
    fetched_at: float = 0.0
    partial: bool = False
    """True when some matchup pages failed — scores are shown but incomplete."""

    @property
    def team_count(self) -> int:
        return sum(len(pair) for pair in self.standings)


class YahooWebClient:
    """Read a public Yahoo league over the anonymous web tier."""

    def __init__(
        self,
        fetch: Fetcher,
        league_id: str | int,
        season: int | None = None,
        *,
        max_matchups: int = MAX_MATCHUP_FETCHES,
    ) -> None:
        self._fetch = fetch
        self.league_id = str(league_id)
        self.season = season
        self._max_matchups = max_matchups
        self._last_good: LeagueData | None = None

    # -- internals ---------------------------------------------------------

    async def _get(self, url: str) -> str:
        """Fetch one page, translating failures into this module's taxonomy."""
        try:
            body, final_url = await self._fetch(url)
        except YahooWebError:
            raise
        except Exception as err:  # any transport error is transient
            raise FetchFailed(f"fetching {url}: {err}") from err

        if is_login_redirect(final_url, body):
            raise LeagueIsPrivate(
                f"league {self.league_id} is not public — Yahoo redirected to sign-in"
            )
        if not body or len(body) < 500:
            raise FetchFailed(f"suspiciously short response from {url} ({len(body)}B)")
        return body

    # -- public API --------------------------------------------------------

    async def async_get_week(self, week: int | None = None) -> int:
        """Discover the current week from the league page.

        Requested without ``?week=``, Yahoo serves the current week, so this
        avoids maintaining a season calendar in the integration.
        """
        if week is not None:
            return week
        body = await self._get(scoreboard_url(self.league_id, 0, self.season).split("?")[0])
        found = parse_current_week(body)
        if found is None:
            raise FetchFailed("could not determine the current week from the league page")
        return found

    async def async_refresh(self, now: float, week: int | None = None) -> LeagueData:
        """Fetch the scoreboard plus every matchup's rosters.

        ``now`` is supplied by the caller — nothing in this module reads a clock,
        which keeps refresh behaviour deterministic under test.
        """
        resolved = await self.async_get_week(week)

        board_body = await self._get(scoreboard_url(self.league_id, resolved, self.season))
        standings = parse_scoreboard(board_body, resolved)
        if not standings:
            raise FetchFailed(f"no matchups found on the week {resolved} scoreboard")

        matchups: list[WebMatchup] = []
        partial = False
        for index in range(min(len(standings), self._max_matchups)):
            url = matchup_url(self.league_id, resolved, index + 1, self.season)
            try:
                matchups.append(parse_matchup(await self._get(url), resolved))
            except LeagueIsPrivate:
                raise
            except (YahooWebError, ValueError) as err:
                # One bad matchup must not lose the other four. The scoreboard
                # already carries every team's score, so the cost is that this
                # matchup's roster popup has no data this cycle.
                _LOGGER.debug("matchup %s failed: %s", index + 1, err)
                partial = True

        if len(standings) > self._max_matchups:
            _LOGGER.warning(
                "league %s reports %d matchups; fetching only the first %d",
                self.league_id,
                len(standings),
                self._max_matchups,
            )
            partial = True

        data = LeagueData(
            week=resolved,
            matchups=matchups,
            standings=standings,
            fetched_at=now,
            partial=partial,
        )
        self._last_good = data
        return data

    async def async_refresh_or_stale(self, now: float, week: int | None = None) -> LeagueData:
        """Refresh, falling back to the last good payload on a transient failure.

        Re-raises :class:`LeagueIsPrivate` untouched — that is a configuration
        problem the user has to fix, and hiding it behind stale data would leave
        them staring at a frozen card with no explanation.
        """
        try:
            return await self.async_refresh(now, week)
        except LeagueIsPrivate:
            raise
        except YahooWebError as err:
            stale = self._last_good
            if stale is None:
                raise
            age = now - stale.fetched_at
            if age > STALE_AFTER_SECONDS:
                raise FetchFailed(f"no fresh data for {age:.0f}s: {err}") from err
            _LOGGER.debug("serving %.0fs-old data after a failed refresh: %s", age, err)
            return stale
