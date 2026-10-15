"""Fetching and caching a player's season, against the captured fixtures.

The fake fetch serves the week-1 seed relabelled as whichever week is asked for (the rosters are
beside the point), ESPN's player list, schedule and five players' lines, and counts every request.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from conftest import FIXTURES
from yahoo_fantasy_football.history import (
    espn_players_url,
    espn_schedule_url,
    espn_stats_url,
    seed_week_url,
)
from yahoo_fantasy_football.history_client import (
    PROJECTION_TTL_SECONDS,
    PlayerNotRostered,
    SeasonHistory,
)

LEAGUE = "999999"
MAYE = "40881"
NOW = 1_792_000_000.0  # Wednesday of week 6, 2026


class FakeFetch:
    def __init__(self, fail: tuple[str, ...] = ()) -> None:
        self.seed = json.loads((FIXTURES / "yahoo_redzone_2026_w1.json").read_text())
        self.fail = fail
        self.calls: list[str] = []

    async def __call__(self, url: str, headers: dict[str, str] | None) -> str:
        self.calls.append(url)
        for prefix in self.fail:
            if url.startswith(prefix):
                raise OSError(f"fetch failed: {url}")
        if url == espn_players_url(2026):
            assert headers and "X-Fantasy-Filter" in headers
            return (FIXTURES / "espn_players_2026.json").read_text()
        if url == espn_schedule_url(2026):
            return (FIXTURES / "espn_schedule_2026.json").read_text()
        if url == espn_stats_url(2026):
            assert "4431452" in headers["X-Fantasy-Filter"]
            return (FIXTURES / "espn_player_stats_2026.json").read_text()
        for week in range(1, 19):
            if url == seed_week_url(LEAGUE, week):
                seed = json.loads(json.dumps(self.seed))
                seed["service"]["leagues"][LEAGUE]["weekInfo"] = {
                    "week": week, "start": "2026-09-09", "end": "2026-09-14",
                }
                return json.dumps(seed)
        raise AssertionError(f"unexpected fetch {url}")

    def seeds(self) -> int:
        return sum(1 for url in self.calls if "redzone" in url)


def _season(history: SeasonHistory, player: str = MAYE, week: int = 6, now: float = NOW) -> dict:
    return asyncio.run(history.player(player, week, 0.0, now))


def test_a_season_from_the_three_sources() -> None:
    fetch = FakeFetch()
    saved = []
    season = _season(SeasonHistory(fetch, LEAGUE, save=lambda: saved.append(1)))

    assert (season["name"], season["nfl_team"], season["position"]) == ("Drake Maye", "NE", "QB")
    assert season["matched"] and season["season"] == 2026 and season["week"] == 6
    rows = {r["week"]: r for r in season["rows"]}
    assert sorted(rows) == list(range(1, 19))
    assert rows[1]["opponent"] == "@ Sea" and rows[11]["status"] == "bye"
    assert all(rows[w]["status"] == "final" and rows[w]["points"] is not None for w in range(1, 6))
    # 26/34, 374 yards and four touchdowns through the air, 25 and one on the ground: the 45.96 the
    # integration scored live that week.
    assert rows[5]["points"] == 45.96
    assert rows[6]["status"] == "live" and rows[7]["status"] == "upcoming"
    assert rows[7]["projected"] is not None
    assert season["totals"]["weeks"] == 5 and season["totals"]["exact"]
    # The whole season's seeds, once each; finished weeks handed over for saving.
    assert fetch.seeds() == 18 and saved


def test_a_second_opening_fetches_nothing() -> None:
    fetch = FakeFetch()
    history = SeasonHistory(fetch, LEAGUE)
    _season(history)
    before = len(fetch.calls)
    _season(history)
    assert len(fetch.calls) == before


def test_finished_weeks_survive_a_restart_and_open_ones_go_stale() -> None:
    history = SeasonHistory(FakeFetch(), LEAGUE)
    _season(history)
    stored = json.loads(json.dumps(history.to_dict()))
    assert sorted(stored["projections"]) == ["1", "2", "3", "4", "5"]

    fetch = FakeFetch()
    restarted = SeasonHistory(fetch, LEAGUE)
    restarted.load(stored)
    _season(restarted)
    # This week's seed, and weeks 7-18 again; weeks 1-5 came from the store.
    assert fetch.seeds() == 13

    later = len(fetch.calls)
    _season(restarted, now=NOW + PROJECTION_TTL_SECONDS + 1)
    assert fetch.seeds() == 13 + 13 and len(fetch.calls) > later


def test_another_leagues_store_is_ignored() -> None:
    history = SeasonHistory(FakeFetch(), LEAGUE)
    history.load({"league_id": "1", "season": 2026, "projections": {"1": {MAYE: 9.0}}})
    assert history.to_dict()["projections"] == {}


def test_without_espn_the_projections_and_schedule_still_show() -> None:
    fetch = FakeFetch(fail=(espn_players_url(2026),))
    season = _season(SeasonHistory(fetch, LEAGUE))
    assert season["matched"] is False
    rows = {r["week"]: r for r in season["rows"]}
    assert rows[1]["points"] is None and rows[1]["projected"] is not None


def test_a_missing_weeks_seed_is_a_blank_column() -> None:
    fetch = FakeFetch(fail=(seed_week_url(LEAGUE, 9),))
    season = _season(SeasonHistory(fetch, LEAGUE))
    rows = {r["week"]: r for r in season["rows"]}
    assert rows[9]["projected"] is None and rows[10]["projected"] is not None


def test_no_schedule_no_season() -> None:
    with pytest.raises(OSError):
        _season(SeasonHistory(FakeFetch(fail=(espn_schedule_url(2026),)), LEAGUE))


def test_a_player_on_no_roster_is_said_so() -> None:
    with pytest.raises(PlayerNotRostered):
        _season(SeasonHistory(FakeFetch(), LEAGUE), player="1")
