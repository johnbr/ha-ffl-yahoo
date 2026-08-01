"""Tests for demo mode.

Two things are being protected here:

* **Determinism.** The demo must be a pure function of (seed, elapsed). If the
  same instant can yield two different leagues, re-polling invents scoring
  events out of nothing and the cards get built against noise.
* **Shape fidelity.** Demo output feeds the real engine unmodified. If it drifts
  from what the live coordinator will produce, every card built against it is
  built against a fiction.
"""

from __future__ import annotations

from collections import Counter

import pytest
from yahoo_fantasy_football.demo import (
    DEMO_CYCLE_SECONDS,
    build_league,
    elapsed_for,
    game_state,
    points_at,
    snapshot_at,
    team_totals,
)
from yahoo_fantasy_football.plays import PlayFeed, diff_snapshots

LEAGUE = build_league()


# ---------------------------------------------------------------------------
# League construction
# ---------------------------------------------------------------------------


def test_league_has_full_rosters_and_paired_matchups() -> None:
    assert len(LEAGUE.teams) == 10
    assert len(LEAGUE.matchups) == 5
    sizes = set(Counter(p.team_key for p in LEAGUE.players).values())
    assert sizes == {12}, f"uneven rosters: {sizes}"


def test_every_team_fields_a_legal_starting_lineup() -> None:
    for team in LEAGUE.teams:
        slots = Counter(p.slot for p in LEAGUE.players if p.team_key == team.team_key)
        assert slots["QB"] == 1
        assert slots["RB"] == 2
        assert slots["WR"] == 3
        assert slots["TE"] == 1
        assert slots["K"] == 1
        assert slots["DEF"] == 1
        assert slots["BN"] == 3


def test_no_player_is_rostered_twice() -> None:
    names = Counter(p.name for p in LEAGUE.players)
    assert [n for n, c in names.items() if c > 1] == []


def test_every_matchup_pairs_two_real_teams() -> None:
    keys = {t.team_key for t in LEAGUE.teams}
    for home, away in LEAGUE.matchups:
        assert home in keys and away in keys and home != away


def test_teammates_share_a_kickoff_window() -> None:
    """Two Chargers are in the same NFL game, so they change state together."""
    by_nfl_team: dict[str, set[int]] = {}
    for player in LEAGUE.players:
        by_nfl_team.setdefault(player.nfl_team, set()).add(player.window)
    offenders = {k: v for k, v in by_nfl_team.items() if len(v) > 1}
    assert offenders == {}, f"teammates in different games: {offenders}"


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_the_same_seed_builds_the_same_league() -> None:
    a, b = build_league(seed=7), build_league(seed=7)
    assert [(p.player_key, p.name, p.moments) for p in a.players] == [
        (p.player_key, p.name, p.moments) for p in b.players
    ]


def test_different_seeds_build_different_leagues() -> None:
    a, b = build_league(seed=1), build_league(seed=2)
    assert [p.name for p in a.players] != [p.name for p in b.players]


def test_the_same_instant_always_yields_the_same_snapshot() -> None:
    """Re-polling an unchanged instant must not invent events."""
    first = snapshot_at(LEAGUE, 600.0)
    second = snapshot_at(LEAGUE, 600.0)
    assert first == second
    assert diff_snapshots(first, second) == []


def test_no_clock_is_read_internally() -> None:
    """Elapsed time is an argument, so the module never surprises a caller."""
    assert elapsed_for(0.0) == 0.0
    assert elapsed_for(DEMO_CYCLE_SECONDS) == 0.0
    assert elapsed_for(DEMO_CYCLE_SECONDS + 5.0) == pytest.approx(5.0)
    assert elapsed_for(123.0, cycle=0) == 0.0


# ---------------------------------------------------------------------------
# The simulated day
# ---------------------------------------------------------------------------


def test_nobody_has_scored_before_kickoff() -> None:
    snapshot = snapshot_at(LEAGUE, 0.0)
    assert all(p.points == 0.0 for p in snapshot.players.values())


def test_every_game_is_final_by_the_end_of_the_cycle() -> None:
    assert all(game_state(p, DEMO_CYCLE_SECONDS) == "post" for p in LEAGUE.players)


def test_game_states_progress_forwards_only() -> None:
    order = {"pre": 0, "in": 1, "post": 2}
    for player in LEAGUE.players:
        seen = [order[game_state(player, f * DEMO_CYCLE_SECONDS)] for f in (0.0, 0.25, 0.5, 0.75, 1.0)]
        assert seen == sorted(seen)


def test_the_board_shows_a_mix_of_game_states_mid_afternoon() -> None:
    """The layout worth designing against — not every game live at once."""
    states = Counter(game_state(p, 0.5 * DEMO_CYCLE_SECONDS) for p in LEAGUE.players)
    assert len(states) >= 2


def test_points_never_go_below_zero() -> None:
    for player in LEAGUE.players:
        for frac in (0.0, 0.3, 0.6, 0.9, 1.0):
            assert points_at(player, frac * DEMO_CYCLE_SECONDS) >= 0.0


def test_only_starters_count_toward_a_team_total() -> None:
    elapsed = DEMO_CYCLE_SECONDS
    totals = team_totals(LEAGUE, elapsed)
    for team in LEAGUE.teams:
        starters = sum(points_at(p, elapsed) for p in LEAGUE.players if p.team_key == team.team_key and p.slot != "BN")
        assert totals[team.team_key]["points"] == pytest.approx(starters, abs=0.01)


def test_projection_converges_on_the_live_total_once_every_game_is_final() -> None:
    totals = team_totals(LEAGUE, DEMO_CYCLE_SECONDS)
    for entry in totals.values():
        assert entry["projected"] == pytest.approx(entry["points"], abs=0.01)


def test_somebody_actually_scores() -> None:
    """A demo where nothing happens would pass every other test here."""
    totals = team_totals(LEAGUE, DEMO_CYCLE_SECONDS)
    assert sum(t["points"] for t in totals.values()) > 100


# ---------------------------------------------------------------------------
# End to end through the real engine
# ---------------------------------------------------------------------------


def _run_a_full_day(step: float = 30.0) -> PlayFeed:
    feed = PlayFeed(maxlen=500)
    previous = None
    elapsed = 0.0
    while elapsed <= DEMO_CYCLE_SECONDS:
        current = snapshot_at(LEAGUE, elapsed)
        feed.add(diff_snapshots(previous, current))
        previous = current
        elapsed += step
    return feed


def test_a_simulated_day_produces_a_plausible_feed() -> None:
    feed = _run_a_full_day()
    assert len(feed) > 30, "a whole Sunday should generate a real feed"

    last = feed.last_play()
    assert last is not None
    assert last.week == LEAGUE.week
    assert last.delta > 0

    recent = feed.recent(10)
    assert len(recent) == 10
    assert all(not e.correction for e in recent), "corrections must stay out of the banner"


def test_the_demo_exercises_the_correction_path() -> None:
    """Defensive scoring includes negative moments, so corrections do occur."""
    feed = _run_a_full_day()
    assert any(e.correction for e in feed.recent(500, include_corrections=True))


def test_replaying_the_day_produces_an_identical_feed() -> None:
    a = [(e.event_id, e.delta) for e in _run_a_full_day().recent(500, include_corrections=True)]
    b = [(e.event_id, e.delta) for e in _run_a_full_day().recent(500, include_corrections=True)]
    assert a == b


def test_a_coarser_poll_interval_loses_no_points() -> None:
    """Polling less often merges events; it must never drop scoring.

    This is the property that makes the adaptive poll interval safe: a slower
    cadence yields fewer, larger events rather than a lower total.
    """
    fine = sum(e.delta for e in _run_a_full_day(step=15.0).recent(999, include_corrections=True))
    coarse = sum(e.delta for e in _run_a_full_day(step=120.0).recent(999, include_corrections=True))
    assert fine == pytest.approx(coarse, abs=0.01)


def test_every_demo_player_matches_the_engines_snapshot_contract() -> None:
    snapshot = snapshot_at(LEAGUE, 0.5 * DEMO_CYCLE_SECONDS)
    assert snapshot.week == LEAGUE.week
    for key, player in snapshot.players.items():
        assert player.player_key == key
        assert isinstance(player.points, float)
        assert player.team_key
        assert player.matchup_id
        assert player.nfl_team
        assert player.position
        assert (player.selected_position == "BN") is not player.starter
