"""Tests for the public-web-tier parser, against real captured pages.

The fixtures are a genuine public league (2025 season, week 13) fetched with no
cookies and no account, so these tests pin the parser to markup Yahoo actually
served rather than to markup we imagined.

What is deliberately asserted here is *cross-checkable* facts — the roster sizes
implied by league settings, and the totals row agreeing with the sum of the
starters it heads. Those catch a column-index slip, which is the failure this
parser is most prone to and the one that would otherwise surface as quietly
wrong numbers on the card rather than as an exception.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from yahoo_fantasy_football.yahoo_web import (
    WebPlayer,
    is_login_redirect,
    matchup_url,
    parse_matchup,
    parse_scoreboard,
    scoreboard_url,
    to_snapshot,
)

FIXTURES = Path(__file__).parent / "fixtures"
MATCHUP = (FIXTURES / "yahoo_web_matchup_2025_w13.html").read_text(encoding="utf-8")
SCOREBOARD = (FIXTURES / "yahoo_web_scoreboard_2025_w13.html").read_text(encoding="utf-8")

PARSED = parse_matchup(MATCHUP, week=13)


# ---------------------------------------------------------------------------
# URLs
# ---------------------------------------------------------------------------


def test_urls_match_the_shapes_that_were_actually_fetched() -> None:
    assert scoreboard_url(476807, 13, season=2025) == (
        "https://football.fantasysports.yahoo.com/2025/f1/476807?week=13"
    )
    assert matchup_url(476807, 13, 1, season=2025) == (
        "https://football.fantasysports.yahoo.com/2025/f1/476807/matchup?week=13&mid1=1"
    )


def test_current_season_omits_the_year_segment() -> None:
    assert "/f1/476807?week=1" in scoreboard_url(476807, 1)
    assert "/2025/" not in scoreboard_url(476807, 1)


def test_a_private_league_is_recognised_as_a_login_bounce() -> None:
    """Private leagues 302 to sign-in; that must not read as an empty league."""
    assert is_login_redirect("https://login.yahoo.com/?.src=Fantasy&.done=x")
    assert is_login_redirect(None, '<html><meta url="https://login.yahoo.com/">')
    assert not is_login_redirect("https://football.fantasysports.yahoo.com/f1/476807")


# ---------------------------------------------------------------------------
# Roster parsing
# ---------------------------------------------------------------------------


def test_both_teams_parse_from_the_single_mirrored_table() -> None:
    left, right = PARSED.roster(0), PARSED.roster(1)
    assert len(left) == 14, "9 starters + 5 bench, counted off the page"
    assert len(right) == 14
    assert len(PARSED.players) == 28


def test_starters_and_bench_split_correctly() -> None:
    for side in (0, 1):
        roster = PARSED.roster(side)
        assert sum(1 for p in roster if p.starter) == 9
        assert sum(1 for p in roster if not p.starter) == 5


def test_team_defenses_are_not_dropped() -> None:
    """DEF is the one slot with no player link — it must not silently vanish.

    Losing it costs real points and the totals cross-check below is what would
    otherwise catch it, so pin it directly.
    """
    defenses = [p for p in PARSED.players if p.slot == "DEF"]
    assert len(defenses) == 2
    names = sorted(p.name for p in defenses)
    assert names == ["Lions - DEF", "Vikings - DEF"]
    assert all(p.player_id.startswith("def-") for p in defenses)
    assert {p.player_id for p in defenses} == {"def-lions-def", "def-vikings-def"}


def test_team_names_come_off_the_page() -> None:
    assert PARSED.teams[0].name == "Tesla"
    assert PARSED.teams[1].name == "Your daddy"


def test_the_known_starting_lineup_parses_exactly() -> None:
    """Spot-check against values read directly off the rendered page."""
    left = {p.name: p for p in PARSED.roster(0)}
    allen = left["Josh Allen"]
    assert allen.player_id == "30977"
    assert allen.slot == "QB"
    assert allen.projected == pytest.approx(25.38)
    assert allen.points == pytest.approx(20.47)
    assert allen.stat_line == "1 Rush TD, 123 Pass Yds, 1 Pass TD"

    brown = left["A.J. Brown"]
    assert brown.points == pytest.approx(35.20)
    assert brown.slot == "WR"


def test_the_opposing_side_parses_from_the_mirrored_columns() -> None:
    """The right-hand columns are reversed; an index slip shows up here."""
    right = {p.name: p for p in PARSED.roster(1)}
    jackson = right["Lamar Jackson"]
    assert jackson.slot == "QB"
    assert jackson.points == pytest.approx(10.79)
    assert jackson.projected == pytest.approx(26.34)
    assert jackson.stat_line == "246 Pass Yds, 17 Comp, 2 Fum Lost"


def test_every_player_has_a_stable_id() -> None:
    ids = [p.player_id for p in PARSED.players]
    # Real players carry Yahoo's numeric id; team defenses have no link on the
    # page at all and get a deterministic slug instead.
    assert all(i.isdigit() or i.startswith("def-") for i in ids)
    assert len(set(ids)) == len(ids), "a duplicate id means a row was double-counted"


def test_no_promo_text_leaks_into_names_or_notes() -> None:
    for p in PARSED.players:
        assert "Video Forecast" not in p.name
        assert "Video Forecast" not in p.game_note
        assert p.name.strip() == p.name


# ---------------------------------------------------------------------------
# Totals — the column-index canary
# ---------------------------------------------------------------------------


def test_team_totals_parse() -> None:
    assert PARSED.teams[0].points == pytest.approx(180.67)
    assert PARSED.teams[0].projected == pytest.approx(138.49)
    assert PARSED.teams[1].points == pytest.approx(104.09)


def test_the_totals_row_agrees_with_the_sum_of_starters() -> None:
    """Yahoo's own total vs ours — catches a mis-mapped points column."""
    for side in (0, 1):
        starters = sum(p.points or 0.0 for p in PARSED.roster(side) if p.starter)
        assert starters == pytest.approx(PARSED.teams[side].points, abs=0.02)


def test_bench_points_are_excluded_from_the_team_total() -> None:
    """A bench player scored; if bench leaked in, the total would not match."""
    bench = sum(p.points or 0.0 for p in PARSED.roster(0) if not p.starter)
    assert bench > 0, "fixture should contain scoring bench players"
    everyone = sum(p.points or 0.0 for p in PARSED.roster(0))
    assert everyone != pytest.approx(PARSED.teams[0].points, abs=0.02)


# ---------------------------------------------------------------------------
# Game state
# ---------------------------------------------------------------------------


def test_completed_games_read_as_post() -> None:
    assert all(p.game_state in ("post", "bye") for p in PARSED.players)


def test_unrecognised_game_text_is_not_forced_into_a_bucket() -> None:
    """Live wording is unobserved, so it must degrade to 'unknown', not 'pre'."""
    p = WebPlayer("1", "X", "QB", 0.0, 0.0, game_note="something new from Yahoo")
    assert p.game_state == "unknown"
    assert WebPlayer("1", "X", "QB", 0.0, 0.0).game_state == "unknown"
    assert WebPlayer("1", "X", "QB", 0.0, 0.0, game_note="Q3 5:22 vs Pit").game_state == "in"
    assert WebPlayer("1", "X", "QB", 0.0, 0.0, game_note="Bye").game_state == "bye"


# ---------------------------------------------------------------------------
# Scoreboard page
# ---------------------------------------------------------------------------


def test_scoreboard_lists_every_matchup_in_the_league() -> None:
    pairs = parse_scoreboard(SCOREBOARD, week=13)
    assert len(pairs) == 5, "10 teams = 5 head-to-head matchups"
    teams = [t for pair in pairs for t in pair]
    assert len({t.team_id for t in teams}) == 10, "each team appears exactly once"
    assert all(t.name for t in teams)


def test_scoreboard_scores_cross_check_against_the_matchup_page() -> None:
    """The same league, same week, two different pages — they must agree.

    This is the strongest available check that the scoreboard block parser is
    reading the right numbers: it is validated against a page parsed by
    completely separate code.
    """
    scores = {t.name: t for pair in parse_scoreboard(SCOREBOARD, week=13) for t in pair}
    assert scores["Tesla"].points == pytest.approx(180.67)
    assert scores["Tesla"].projected == pytest.approx(138.49)
    assert scores["Your daddy"].points == pytest.approx(104.09)
    assert scores["Your daddy"].projected == pytest.approx(126.61)


def test_scoreboard_ignores_standings_and_leader_widgets() -> None:
    """The page prints other decimals; none may be mistaken for a score."""
    pairs = parse_scoreboard(SCOREBOARD, week=13)
    for home, away in pairs:
        for t in (home, away):
            assert t.points is None or 0 <= t.points < 400


# ---------------------------------------------------------------------------
# Bridge into the engine
# ---------------------------------------------------------------------------


def test_parsed_pages_convert_into_an_engine_snapshot() -> None:
    snap = to_snapshot([PARSED], week=13, taken_at=1000.0, league_id=476807)
    assert snap.week == 13
    assert snap.taken_at == 1000.0
    assert len(snap.players) == 28
    for key, player in snap.players.items():
        assert player.player_key == key
        assert isinstance(player.points, float)
        assert player.team_key.startswith("476807.t.")
        assert player.matchup_id == "w13.m1"


def test_the_snapshot_drives_the_existing_scoring_engine() -> None:
    """A change between two web polls must produce a scoring event."""
    from yahoo_fantasy_football.plays import diff_snapshots

    before = to_snapshot([PARSED], week=13, taken_at=0.0, league_id=476807)
    after = to_snapshot([PARSED], week=13, taken_at=60.0, league_id=476807)
    assert diff_snapshots(before, after) == [], "identical polls invent nothing"

    key = next(iter(after.players))
    bumped = dict(after.players)
    from dataclasses import replace

    bumped[key] = replace(bumped[key], points=bumped[key].points + 6.0)
    from yahoo_fantasy_football.plays import LeagueSnapshot

    events = diff_snapshots(before, LeagueSnapshot(13, 60.0, bumped))
    assert len(events) == 1
    assert events[0].delta == pytest.approx(6.0)


def test_starters_survive_the_bridge_as_starters() -> None:
    snap = to_snapshot([PARSED], week=13, taken_at=0.0, league_id=476807)
    starters = [p for p in snap.players.values() if p.starter]
    assert len(starters) == 18, "9 per team"
