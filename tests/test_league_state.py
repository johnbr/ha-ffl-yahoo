"""Tests for the view layer the entities and cards consume.

These shapes are a contract with the frontend, so they are pinned here rather
than discovered when a card renders wrong. The coordinator is intentionally a
thin shell over this module, since HA machinery cannot be unit-tested in this
repo's harness.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from yahoo_fantasy_football.const import (
    SCAN_INTERVAL_IDLE_SECONDS,
    SCAN_INTERVAL_LIVE_SECONDS,
    SCAN_INTERVAL_NEAR_GAME_SECONDS,
)
from yahoo_fantasy_football.league_state import (
    ATTR_RECENT_PLAYS,
    find_team,
    matchup_rows,
    play_dict,
    player_rows,
    poll_interval,
    scoreboard_attributes,
    scoreboard_state,
)
from yahoo_fantasy_football.plays import PlayFeed, diff_snapshots
from yahoo_fantasy_football.web_client import LeagueData, YahooWebClient
from yahoo_fantasy_football.yahoo_web import to_snapshot

FIXTURES = Path(__file__).parent / "fixtures"
MATCHUP = (FIXTURES / "yahoo_web_matchup_2025_w13.html").read_text(encoding="utf-8")
SCOREBOARD = (FIXTURES / "yahoo_web_scoreboard_2025_w13.html").read_text(encoding="utf-8")


async def _fetch(url: str) -> tuple[str, str]:
    return (MATCHUP if "/matchup?" in url else SCOREBOARD), url


DATA: LeagueData = asyncio.run(
    YahooWebClient(_fetch, league_id=476807, season=2025).async_refresh(now=100.0)
)


# ---------------------------------------------------------------------------
# Poll cadence
# ---------------------------------------------------------------------------


def test_a_completed_slate_drops_to_idle() -> None:
    """Every game in the fixture is final, so nothing needs fast polling."""
    assert poll_interval(DATA) == SCAN_INTERVAL_IDLE_SECONDS


def test_a_live_game_forces_live_cadence() -> None:
    from dataclasses import replace

    m = DATA.matchups[0]
    live = replace(m, players=[replace(m.players[0], game_note="Q3 5:22 vs Pit"), *m.players[1:]])
    assert poll_interval(replace(DATA, matchups=[live])) == SCAN_INTERVAL_LIVE_SECONDS


def test_unknown_game_state_polls_conservatively_not_idly() -> None:
    """Live wording is unverified, so unknown must not be mistaken for 'done'."""
    from dataclasses import replace

    m = DATA.matchups[0]
    odd = replace(m, players=[replace(m.players[0], game_note="???"), *m.players[1:]])
    assert poll_interval(replace(DATA, matchups=[odd])) == SCAN_INTERVAL_NEAR_GAME_SECONDS


def test_no_data_yet_polls_at_the_middle_cadence() -> None:
    assert poll_interval(None) == SCAN_INTERVAL_NEAR_GAME_SECONDS


# ---------------------------------------------------------------------------
# Scoreboard rows
# ---------------------------------------------------------------------------


def test_every_matchup_produces_a_row() -> None:
    rows = matchup_rows(DATA)
    assert len(rows) == 5
    assert all(r["home"]["name"] and r["away"]["name"] for r in rows)


def test_the_leader_is_identified() -> None:
    row = next(r for r in matchup_rows(DATA) if r["home"]["name"] == "Tesla")
    assert row["home"]["points"] == pytest.approx(180.67)
    assert row["away"]["points"] == pytest.approx(104.09)
    assert row["leader"] == row["home"]["team_id"]


def test_a_tie_has_no_leader() -> None:
    from dataclasses import replace

    home, away = DATA.standings[0]
    tied = replace(DATA, standings=[(home, replace(away, points=home.points))])
    assert matchup_rows(tied)[0]["leader"] is None


def test_rows_come_from_the_scoreboard_so_a_failed_roster_still_scores() -> None:
    """A matchup whose roster fetch failed must still show its score."""
    from dataclasses import replace

    degraded = replace(DATA, matchups=DATA.matchups[:2], partial=True)
    rows = matchup_rows(degraded)
    assert len(rows) == 5, "all five scores present"
    assert [r["has_roster"] for r in rows] == [True, True, False, False, False]


# ---------------------------------------------------------------------------
# Roster popup
# ---------------------------------------------------------------------------


def test_the_popup_payload_splits_starters_from_bench() -> None:
    payload = player_rows(DATA, 0)
    assert payload["matchup_id"] == "w13.m1"
    assert len(payload["sides"]) == 2
    for side in payload["sides"]:
        assert len(side["starters"]) == 9
        assert len(side["bench"]) == 5


def test_the_popup_carries_the_fields_the_card_renders() -> None:
    starter = player_rows(DATA, 0)["sides"][0]["starters"][0]
    assert set(starter) >= {
        "name",
        "slot",
        "points",
        "projected",
        "stat_line",
        "game_state",
    }
    assert starter["name"] == "Josh Allen"
    assert starter["projected"] == pytest.approx(25.38)


def test_an_out_of_range_matchup_returns_empty_rather_than_raising() -> None:
    """A card can request a matchup that failed to fetch; that is not an error."""
    assert player_rows(DATA, 99) == {"matchup_id": None, "sides": []}


# ---------------------------------------------------------------------------
# Entity payload
# ---------------------------------------------------------------------------


def test_the_state_is_low_churn() -> None:
    """The state hits the recorder on every change, so it must not be a score."""
    assert scoreboard_state(DATA) == "w13-5"
    assert scoreboard_state(None) == "unknown"


def test_attributes_carry_the_board_and_a_bounded_play_list() -> None:
    feed = PlayFeed()
    before = to_snapshot(DATA.matchups, 13, 0.0, 476807)
    after = to_snapshot(DATA.matchups, 13, 60.0, 476807)

    from dataclasses import replace

    bumped = dict(after.players)
    for key in list(bumped)[:20]:
        bumped[key] = replace(bumped[key], points=bumped[key].points + 6.0)
    from yahoo_fantasy_football.plays import LeagueSnapshot

    feed.add(diff_snapshots(before, LeagueSnapshot(13, 60.0, bumped)))

    attrs = scoreboard_attributes(DATA, feed, "476807")
    assert attrs["week"] == 13
    assert len(attrs["matchups"]) == 5
    assert attrs["last_play"] is not None
    assert len(attrs["recent_plays"]) == ATTR_RECENT_PLAYS, "bounded, not the whole feed"
    assert attrs["source"] == "web"


def test_attributes_are_safe_before_the_first_refresh() -> None:
    attrs = scoreboard_attributes(None, PlayFeed(), "476807")
    assert attrs["week"] is None
    assert attrs["matchups"] == []
    assert attrs["last_play"] is None


def test_a_play_renders_with_text_for_the_banner() -> None:
    before = to_snapshot(DATA.matchups, 13, 0.0, 476807)
    from dataclasses import replace

    from yahoo_fantasy_football.plays import LeagueSnapshot

    after = to_snapshot(DATA.matchups, 13, 60.0, 476807)
    key = next(iter(after.players))
    bumped = dict(after.players)
    bumped[key] = replace(bumped[key], points=bumped[key].points + 6.4)

    event = diff_snapshots(before, LeagueSnapshot(13, 60.0, bumped))[0]
    d = play_dict(event)
    assert d["delta"] == pytest.approx(6.4)
    assert d["text"]
    assert d["correction"] is False


# ---------------------------------------------------------------------------
# My-team lookup
# ---------------------------------------------------------------------------


def test_a_team_is_located_with_its_opponent() -> None:
    tesla = matchup_rows(DATA)[0]["home"]
    found = find_team(DATA, tesla["team_id"])
    assert found is not None
    assert found["me"]["name"] == "Tesla"
    assert found["opponent"]["name"] == "Your daddy"
    assert found["winning"] is True


def test_the_away_side_is_found_too() -> None:
    away = matchup_rows(DATA)[0]["away"]
    found = find_team(DATA, away["team_id"])
    assert found is not None
    assert found["me"]["name"] == "Your daddy"
    assert found["winning"] is False


def test_an_unconfigured_or_missing_team_is_not_an_error() -> None:
    assert find_team(DATA, None) is None
    assert find_team(DATA, "99999") is None
    assert find_team(None, "1") is None
