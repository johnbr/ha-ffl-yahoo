"""A player's season: Yahoo's weekly projections, ESPN's weekly lines, ESPN's schedule.

The scoring assertions are not invented. ``espn_week5_vs_yahoo.json`` pairs ESPN's week-5 line for
every player the integration scored that week with the total the integration computed live from
Yahoo's own feed, and the league's modifiers are the ones in the week-1 seed fixture.
"""

from __future__ import annotations

import json

import pytest
from conftest import FIXTURES
from yahoo_fantasy_football.history import (
    ESPN_PROJECTED,
    Fixture,
    Identity,
    match_espn_player,
    parse_espn_players,
    parse_espn_schedule,
    parse_espn_weeks,
    player_identity,
    season_of,
    season_rows,
    season_totals,
    seed_week_url,
    unresolved_stats,
    week_points,
    week_projections,
)
from yahoo_fantasy_football.yahoo_redzone import scoring_modifiers

LEAGUE = "999999"
MAYE, MAHOMES, WALKER, AUBREY, TEXANS = 4431452, 3139477, 4567048, 3953687, -16034


@pytest.fixture(scope="module")
def seed() -> dict:
    return json.loads((FIXTURES / "yahoo_redzone_2026_w1.json").read_text())


@pytest.fixture(scope="module")
def modifiers(seed: dict) -> dict[str, float]:
    return scoring_modifiers(seed["service"]["leagues"][LEAGUE])


@pytest.fixture(scope="module")
def espn_players():
    return parse_espn_players(json.loads((FIXTURES / "espn_players_2026.json").read_text()))


@pytest.fixture(scope="module")
def espn_stats() -> dict:
    return json.loads((FIXTURES / "espn_player_stats_2026.json").read_text())


@pytest.fixture(scope="module")
def schedule():
    return parse_espn_schedule(json.loads((FIXTURES / "espn_schedule_2026.json").read_text()))


# ---------------------------------------------------------------------------
# Scoring ESPN's lines the league's way
# ---------------------------------------------------------------------------


def test_week_five_scores_to_the_cent_as_yahoo_did(modifiers: dict[str, float]) -> None:
    """Every QB, back, receiver, kicker and defence the integration scored in week 5."""
    cases = json.loads((FIXTURES / "espn_week5_vs_yahoo.json").read_text())["cases"]
    assert len(cases) == 115
    unresolved = unresolved_stats(modifiers)
    wrong = []
    for case in cases:
        points, exact = week_points(case["espn_stats"], case["defense"], modifiers, unresolved)
        assert exact
        if abs(points - case["yahoo_points"]) >= 0.005:
            wrong.append((case["player"], points, case["yahoo_points"]))
    assert wrong == []
    assert sum(1 for c in cases if c["defense"]) == 9


def test_this_leagues_scoring_is_all_within_espns_reach(modifiers: dict[str, float]) -> None:
    assert unresolved_stats(modifiers) == frozenset()


def test_short_field_goals_paid_unevenly_cannot_be_split_from_espns_line(
    modifiers: dict[str, float],
) -> None:
    uneven = {**modifiers, "fieldGoalsMade0through19": 2.0}
    unresolved = unresolved_stats(uneven)
    assert unresolved == {"fieldGoalsMade0through19", "fieldGoalsMade20through29", "fieldGoalsMade30through39"}
    # Only a week with a short kick in it is in doubt.
    assert week_points({"80": 1.0, "86": 3.0}, False, uneven, unresolved)[1] is False
    assert week_points({"77": 1.0, "86": 3.0}, False, uneven, unresolved)[1] is True


def test_a_stat_espn_does_not_report_makes_every_week_approximate(modifiers: dict[str, float]) -> None:
    bonus = {**modifiers, "fortyPlusPassTD": 2.0}
    unresolved = unresolved_stats(bonus)
    assert unresolved == {"fortyPlusPassTD"}
    assert week_points({"3": 250.0}, False, bonus, unresolved)[1] is False


# ---------------------------------------------------------------------------
# Matching ESPN's players to Yahoo's
# ---------------------------------------------------------------------------


def test_players_match_on_name_position_and_club(espn_players) -> None:
    def espn_id(name: str, team: str, position: str) -> int | None:
        found = match_espn_player(espn_players, name, team, position)
        return found.espn_id if found else None

    assert espn_id("Drake Maye", "17", "QB") == MAYE
    assert espn_id("Kenneth Walker III", "12", "RB") == WALKER
    # A defence is its club, whatever Yahoo calls it.
    assert espn_id("Texans", "34", "DEF") == TEXANS
    # Two of each name on ESPN's books; the position decides.
    lamar = match_espn_player(espn_players, "Lamar Jackson", "33", "QB")
    assert (lamar.team_id, lamar.position_id) == ("33", 1)
    jefferson = match_espn_player(espn_players, "Justin Jefferson", "16", "WR")
    assert (jefferson.team_id, jefferson.position_id) == ("16", 3)
    # A name written differently: the one quarterback in Kansas City called Mahomes.
    assert espn_id("Pat Mahomes", "12", "QB") == MAHOMES
    assert espn_id("Nobody Atall", "12", "QB") is None


# ---------------------------------------------------------------------------
# ESPN's weeks and schedule
# ---------------------------------------------------------------------------


def test_weekly_lines_skip_a_bye(espn_stats: dict) -> None:
    assert sorted(parse_espn_weeks(espn_stats, MAYE, 2026)) == [1, 2, 3, 4, 5]
    # Kansas City's bye was week 5.
    assert sorted(parse_espn_weeks(espn_stats, MAHOMES, 2026)) == [1, 2, 3, 4]
    assert parse_espn_weeks(espn_stats, MAYE, 2025) == {}
    maye_five = parse_espn_weeks(espn_stats, MAYE, 2026)[5]
    assert (maye_five["1"], maye_five["3"], maye_five["4"]) == (26.0, 374.0, 4.0)


def test_projected_lines_cover_every_week(espn_stats: dict) -> None:
    """ESPN keeps its projection for a week after the week is played, and projects a bye as nothing."""
    projected = parse_espn_weeks(espn_stats, MAYE, 2026, ESPN_PROJECTED)
    assert sorted(projected) == list(range(1, 19))
    assert projected[11] == {}  # New England's bye
    assert projected[2]["0"] != parse_espn_weeks(espn_stats, MAYE, 2026)[2]["0"]


def test_the_schedule_names_each_week_and_leaves_the_bye_empty(schedule) -> None:
    assert schedule["17"][1] == Fixture(week=1, opponent_id="26", home=False, kickoff=1788999600.0)
    assert schedule["26"][1].home and schedule["26"][1].opponent_id == "17"
    assert 11 not in schedule["17"]  # New England's bye
    assert 5 not in schedule["12"]  # Kansas City's
    assert "0" not in schedule  # ESPN's free-agent "club"


def test_season_is_the_year_of_its_autumn() -> None:
    assert season_of({"start": "2026-10-15"}) == 2026
    assert season_of({"start": "2027-01-07"}) == 2026
    assert season_of({}) is None


# ---------------------------------------------------------------------------
# Yahoo's side
# ---------------------------------------------------------------------------


def test_the_seed_names_the_player_and_projects_the_whole_league(seed: dict) -> None:
    assert player_identity(seed, LEAGUE, "40881") == Identity("40881", "Drake Maye", "17", "QB")
    texans = player_identity(seed, LEAGUE, "100034")
    assert texans.defense and texans.team_id == "34"
    assert player_identity(seed, LEAGUE, "1") is None
    projections = week_projections(seed, LEAGUE)
    assert len(projections) == 140
    assert projections["40881"] == pytest.approx(float(_entry(seed, "40881")["projectedPoints"]))


def test_a_week_is_asked_for_by_number() -> None:
    url = seed_week_url(LEAGUE, 8)
    assert url.endswith("&week=8") and f"league_id={LEAGUE}" in url


def _entry(seed: dict, player_id: str) -> dict:
    for team in seed["service"]["leagues"][LEAGUE]["teams"].values():
        for entry in team["players"]:
            if entry["id"] == player_id:
                return entry
    raise KeyError(player_id)


# ---------------------------------------------------------------------------
# The season, assembled
# ---------------------------------------------------------------------------


def test_a_season_reads_week_by_week(espn_stats: dict, schedule, modifiers: dict[str, float]) -> None:
    """Mahomes on the Wednesday of week 6: four played, a bye, this week, and the rest to come."""
    mahomes = Identity("30123", "Patrick Mahomes", "12", "QB")
    projections = {1: {"30123": 21.5}, 3: {"30123": 19.0}, 6: {"30123": 22.4}, 7: {"30123": 20.1}}
    actuals = parse_espn_weeks(espn_stats, MAHOMES, 2026)
    rows = season_rows(
        mahomes, list(range(1, 19)), 6, projections, actuals, schedule["12"], modifiers, 0.0
    )
    by_week = {r["week"]: r for r in rows}

    assert [r["status"] for r in rows[:7]] == ["final", "final", "final", "final", "bye", "live", "upcoming"]
    assert by_week[5] == {
        "week": 5, "status": "bye", "opponent": "BYE", "home": None,
        "kickoff": None, "projected": None, "projected_source": None, "points": None, "exact": True,
    }
    # Week 1, from ESPN's line under the league's own modifiers.
    one = actuals[1]
    expected = round(
        0.25 * one.get("1", 0) + 0.04 * one.get("3", 0) + 4 * one.get("4", 0) - 2 * one.get("20", 0)
        + 0.1 * one.get("24", 0) + 6 * one.get("25", 0) + 2 * (one.get("19", 0) + one.get("26", 0))
        - 2 * one.get("72", 0),
        2,
    )
    assert by_week[1]["points"] == expected
    assert (by_week[1]["projected"], by_week[1]["projected_source"]) == (21.5, "yahoo")
    # A week he was on no roster in this league has no Yahoo projection, and no ESPN one was given.
    assert by_week[2]["projected"] is None and by_week[2]["projected_source"] is None
    assert by_week[2]["points"] is not None
    assert (by_week[6]["points"], by_week[6]["projected"]) == (0.0, 22.4)
    assert by_week[7]["points"] is None and by_week[7]["projected"] == 20.1
    assert by_week[7]["opponent"].startswith(("vs ", "@ "))

    totals = season_totals(rows)
    assert totals["weeks"] == 4
    assert totals["points"] == round(sum(by_week[w]["points"] for w in (1, 2, 3, 4)), 2)
    assert totals["projected_remaining"] == 20.1
    assert totals["exact"] is True


def test_espn_projects_the_weeks_he_was_on_no_roster(
    espn_stats: dict, schedule, modifiers: dict[str, float]
) -> None:
    """Maye in Kush: rostered in week 1, a free agent in weeks 2-4, picked up for week 5.

    Yahoo's seed for weeks 2-4 has no projection for him, so ESPN's projected line stands in,
    under the league's scoring as his points are. Yahoo's own number is kept wherever there is one.
    """
    maye = Identity("40881", "Drake Maye", "17", "QB")
    yahoo = {1: {"40881": 23.07}, 5: {"40881": 24.76}, 6: {"40881": 25.18}, 7: {"40881": 24.16}}
    espn = parse_espn_weeks(espn_stats, MAYE, 2026, ESPN_PROJECTED)
    rows = season_rows(
        maye, list(range(1, 19)), 6, yahoo, parse_espn_weeks(espn_stats, MAYE, 2026),
        schedule["17"], modifiers, 0.0, espn,
    )
    by_week = {r["week"]: r for r in rows}

    assert [(by_week[w]["projected"], by_week[w]["projected_source"]) for w in (1, 5, 6, 7)] == [
        (23.07, "yahoo"), (24.76, "yahoo"), (25.18, "yahoo"), (24.16, "yahoo"),
    ]
    for week in (2, 3, 4):
        assert by_week[week]["projected_source"] == "espn"
        assert by_week[week]["projected"] == week_points(espn[week], False, modifiers, frozenset())[0]
    # 21.1 of 31.4 for 239 yards, 1.5 touchdowns and 0.7 interceptions; 34 yards on the ground.
    assert by_week[2]["projected"] == 23.5
    # The rest of the season is ESPN's too, past the weeks Yahoo was asked for.
    assert by_week[8]["projected_source"] == "espn" and by_week[8]["projected"] > 0
    assert by_week[11]["status"] == "bye" and by_week[11]["projected_source"] is None


def test_an_empty_projected_line_is_nought_not_a_shutout(schedule, modifiers) -> None:
    """ESPN projecting nothing for a defence must not score the points-allowed-0 band."""
    texans = Identity("100034", "Houston", "34", "DEF")
    rows = season_rows(texans, [1], 2, {}, {}, schedule["34"], modifiers, None, {1: {}})
    assert (rows[0]["projected"], rows[0]["projected_source"]) == (0.0, "espn")


def test_a_finished_week_without_a_line_is_nought_not_unknown(schedule, modifiers) -> None:
    """The club played and ESPN has nothing for him: a scratch, or a game with no stat in it."""
    who = Identity("1", "Somebody", "17", "WR")
    rows = season_rows(who, [1, 2], 3, {}, {}, schedule["17"], modifiers, None)
    assert [r["points"] for r in rows] == [0.0, 0.0]
    # No ESPN player matched at all: unknown, not zero.
    rows = season_rows(who, [1, 2], 3, {}, None, schedule["17"], modifiers, None)
    assert [r["points"] for r in rows] == [None, None]
