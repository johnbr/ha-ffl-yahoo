"""Fetching and caching for a player's season — :mod:`history` does the rest.

Opening a player costs nothing upstream after the first: what never changes is kept for good, and
what does is kept for hours, not polls.

* **A finished week's projections** never change once the week is played, so they are fetched
  once and saved with the entry; a restart does not fetch them again. That is most of the cost:
  each week is the whole league seed, about 190 KB.
* **This week's and future weeks' projections** move with injury news, so they keep for
  ``PROJECTION_TTL_SECONDS``.
* **ESPN's player list and schedule** keep for a day, and **a player's ESPN lines** — actual and
  projected, in one request — for ``ESPN_TTL_SECONDS``: corrections land in the days after a game.

A source that fails leaves its part of the rows empty rather than failing the whole player,
except the schedule, without which there are no weeks to show.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from .history import (
    ESPN_ACTUAL,
    ESPN_PLAYERS_FILTER,
    ESPN_PROJECTED,
    espn_players_url,
    espn_schedule_url,
    espn_stats_filter,
    espn_stats_url,
    match_espn_player,
    parse_espn_players,
    parse_espn_schedule,
    parse_espn_weeks,
    player_identity,
    season_of,
    season_rows,
    season_totals,
    season_weeks,
    seed_week_info,
    seed_week_url,
    week_projections,
)
from .yahoo_redzone import scoring_modifiers, team_abbr

_LOGGER = logging.getLogger(__name__)

PROJECTION_TTL_SECONDS = 6 * 3600
ESPN_TTL_SECONDS = 6 * 3600
DIRECTORY_TTL_SECONDS = 24 * 3600

# Seeds fetched at once on a cold start. Eighteen weeks of them is the first opening's whole
# cost; four at a time keeps it to a few seconds without asking Yahoo for eighteen at once.
SEEDS_AT_ONCE = 4

Fetch = Callable[[str, dict[str, str] | None], Awaitable[str]]


class PlayerNotRostered(LookupError):
    """The player is on no roster in the league this week."""


@dataclass
class _Kept:
    fetched_at: float
    value: Any

    def fresh(self, now: float, ttl: float) -> bool:
        return now - self.fetched_at < ttl


class SeasonHistory:
    """One league's player seasons, with everything they are built from cached."""

    def __init__(
        self,
        fetch: Fetch,
        league_id: str,
        save: Callable[[], None] | None = None,
    ) -> None:
        self._fetch = fetch
        self.league_id = str(league_id)
        self._save = save
        self._season: int | None = None
        # Finished weeks: {week: {player id: projection}}, kept for good and saved.
        self._final: dict[int, dict[str, float]] = {}
        # This week's and later: {week: _Kept(projections)}; and this week's whole seed.
        self._open: dict[int, _Kept] = {}
        self._current: _Kept | None = None
        self._schedule: _Kept | None = None
        self._players: _Kept | None = None
        self._lines: dict[int, _Kept] = {}
        self._lock = asyncio.Lock()

    # -- persistence -------------------------------------------------------------------------

    def load(self, stored: dict[str, Any] | None) -> None:
        if not stored or str(stored.get("league_id")) != self.league_id:
            return
        self._season = stored.get("season")
        self._final = {
            int(week): {str(k): float(v) for k, v in projections.items()}
            for week, projections in (stored.get("projections") or {}).items()
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "league_id": self.league_id,
            "season": self._season,
            "projections": {str(week): projections for week, projections in self._final.items()},
        }

    # -- the season --------------------------------------------------------------------------

    async def player(
        self, player_id: str, current_week: int, live_points: float | None, now: float
    ) -> dict[str, Any]:
        """A player's season as rows, for the card."""
        async with self._lock:
            seed = await self._current_seed(current_week, now)
            identity = player_identity(seed, self.league_id, player_id)
            if identity is None:
                raise PlayerNotRostered(player_id)
            season = season_of(seed_week_info(seed, self.league_id))
            if season is None:
                raise ValueError("the league seed carries no week dates")
            if season != self._season:
                self._season, self._final = season, {}
            league = ((seed.get("service") or {}).get("leagues") or {}).get(self.league_id) or {}
            modifiers = scoring_modifiers(league)

            schedule = await self._schedule_for(season, now)
            weeks = season_weeks(schedule)
            projections = await self._projections(weeks, current_week, now)
            lines = await self._espn_lines(identity, season, now)
            actuals, espn_projections = lines if lines is not None else (None, None)

        rows = season_rows(
            identity,
            weeks,
            current_week,
            projections,
            actuals,
            schedule.get(identity.team_id, {}),
            modifiers,
            live_points,
            espn_projections,
        )
        return {
            "player_id": identity.player_id,
            "name": identity.name,
            "nfl_team": team_abbr(identity.team_id),
            "position": identity.position,
            "season": season,
            "week": current_week,
            # False when ESPN has no player to match: the finished weeks' points are then unknown.
            "matched": actuals is not None,
            "rows": rows,
            "totals": season_totals(rows),
        }

    async def _current_seed(self, week: int, now: float) -> dict[str, Any]:
        kept = self._current
        if kept is None or kept.value[0] != week or not kept.fresh(now, PROJECTION_TTL_SECONDS):
            seed = json.loads(await self._fetch(seed_week_url(self.league_id, week), None))
            self._current = kept = _Kept(now, (week, seed))
            self._open[week] = _Kept(now, week_projections(seed, self.league_id))
        return kept.value[1]

    async def _schedule_for(self, season: int, now: float) -> dict[str, Any]:
        kept = self._schedule
        if kept is None or kept.value[0] != season or not kept.fresh(now, DIRECTORY_TTL_SECONDS):
            payload = json.loads(await self._fetch(espn_schedule_url(season), None))
            kept = self._schedule = _Kept(now, (season, parse_espn_schedule(payload)))
        return kept.value[1]

    async def _projections(
        self, weeks: list[int], current_week: int, now: float
    ) -> dict[int, dict[str, float]]:
        """Every week's projections, fetching only what is missing or stale."""
        wanted = [
            week
            for week in weeks
            if week not in self._final
            and not (week in self._open and self._open[week].fresh(now, PROJECTION_TTL_SECONDS))
        ]
        gate = asyncio.Semaphore(SEEDS_AT_ONCE)

        async def one(week: int) -> tuple[int, dict[str, float] | None]:
            async with gate:
                try:
                    seed = json.loads(await self._fetch(seed_week_url(self.league_id, week), None))
                except Exception as err:  # one missing week is a blank column, not a failure
                    _LOGGER.debug("Week %d projections unavailable: %s", week, err)
                    return week, None
            return week, week_projections(seed, self.league_id)

        finished = False
        for week, projections in await asyncio.gather(*(one(w) for w in wanted)):
            if projections is None:
                continue
            if week < current_week:
                self._final[week] = projections
                self._open.pop(week, None)
                finished = True
            else:
                self._open[week] = _Kept(now, projections)
        if finished and self._save is not None:
            self._save()
        return {
            **{week: kept.value for week, kept in self._open.items()},
            **self._final,
        }

    async def _espn_lines(
        self, identity: Any, season: int, now: float
    ) -> tuple[dict[int, dict[str, float]], dict[int, dict[str, float]]] | None:
        """ESPN's weekly ``(actual, projected)`` lines for the player.

        None when ESPN cannot be read, or has no player to match.
        """
        try:
            kept = self._players
            if kept is None or kept.value[0] != season or not kept.fresh(now, DIRECTORY_TTL_SECONDS):
                payload = json.loads(
                    await self._fetch(espn_players_url(season), {"X-Fantasy-Filter": ESPN_PLAYERS_FILTER})
                )
                kept = self._players = _Kept(now, (season, parse_espn_players(payload)))
            match = match_espn_player(kept.value[1], identity.name, identity.team_id, identity.position)
            if match is None:
                _LOGGER.debug("No ESPN player matches %s (%s)", identity.name, identity.team_id)
                return None
            lines = self._lines.get(match.espn_id)
            if lines is None or not lines.fresh(now, ESPN_TTL_SECONDS):
                payload = json.loads(
                    await self._fetch(
                        espn_stats_url(season), {"X-Fantasy-Filter": espn_stats_filter(match.espn_id)}
                    )
                )
                lines = self._lines[match.espn_id] = _Kept(
                    now,
                    (
                        parse_espn_weeks(payload, match.espn_id, season, ESPN_ACTUAL),
                        parse_espn_weeks(payload, match.espn_id, season, ESPN_PROJECTED),
                    ),
                )
            return lines.value
        except Exception as err:  # the projections and schedule are still worth showing
            _LOGGER.debug("ESPN lines unavailable for %s: %s", identity.name, err)
            return None
