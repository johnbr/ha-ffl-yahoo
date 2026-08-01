"""Tests for the ESPN scoring-play parser and player matcher.

Every play string here is a REAL capture from ESPN's 2025 NFL season (see
``tests/fixtures/README.md``), not hand-written. That matters: several of the
formats are things nobody would invent, including a typo in ESPN's own data and
a parenthetical that is a team abbreviation rather than a kicker.

The module imports cleanly without Home Assistant — the parsing half has no HA
imports at all — so these run in CI with just pytest.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "custom_components" / "yahoo_fantasy_football"))

from espn import (
    ROLE_DEFENSIVE_TD,
    ROLE_FIELD_GOAL,
    ROLE_PASSING_TD,
    ROLE_PAT,
    ROLE_PAT_MISSED,
    ROLE_RECEIVING_TD,
    ROLE_RETURN_TD,
    ROLE_RUSHING_TD,
    ROLE_TWO_POINT_PASS,
    ROLE_TWO_POINT_RECEPTION,
    match_play_to_player,
    name_parts,
    normalize_name,
    normalize_scoreboard,
    parse_scoring_play,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _fixture_texts() -> dict[str, dict]:
    plays = json.loads((FIXTURES / "espn_scoring_plays.json").read_text(encoding="utf-8"))
    return {p["text"]: p for p in plays}


PLAYS = _fixture_texts()


def _credits(text: str):
    """Parse a fixture play, asserting the fixture actually contains it."""
    assert text in PLAYS, f"fixture missing this exact capture: {text!r}"
    return parse_scoring_play(text)


# ---------------------------------------------------------------------------
# Name normalization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Michael Penix Jr.", "michael penix"),
        ("Kenny Moore II", "kenny moore"),
        ("Ka'imi Fairbairn", "kaimi fairbairn"),
        ("De'Von Achane", "devon achane"),
        ("D.K. Metcalf", "dk metcalf"),
        ("DK Metcalf", "dk metcalf"),
        ("Amon-Ra St. Brown", "amon-ra st brown"),
        ("", ""),
    ],
)
def test_normalize_name(raw: str, expected: str) -> None:
    assert normalize_name(raw) == expected


def test_normalize_name_makes_suffix_variants_equal() -> None:
    """Yahoo and ESPN disagree about suffixes constantly; both sides normalize."""
    assert normalize_name("Michael Penix Jr.") == normalize_name("Michael Penix")


def test_name_parts_uses_last_word_as_surname() -> None:
    assert name_parts("Amon-Ra St. Brown") == ("amon-ra", "brown")
    assert name_parts("Cher") == ("", "cher")


# ---------------------------------------------------------------------------
# Play parsing — one test per real format
# ---------------------------------------------------------------------------


def test_passing_touchdown_credits_receiver_passer_and_kicker() -> None:
    """A passing TD names THREE fantasy scorers, not one."""
    credits = _credits("Bijan Robinson 50 Yd pass from Michael Penix Jr. (Younghoe Koo Kick)")
    assert [(c.name, c.role, c.yards) for c in credits] == [
        ("Bijan Robinson", ROLE_RECEIVING_TD, 50),
        ("Michael Penix Jr.", ROLE_PASSING_TD, 50),
        ("Younghoe Koo", ROLE_PAT, None),
    ]


def test_rushing_touchdown_with_suffix_before_parenthetical() -> None:
    """'Penix Jr. (' is the awkward boundary — the suffix abuts the paren."""
    credits = _credits("Michael Penix Jr. 4 Yd Rush (Younghoe Koo Kick)")
    assert [(c.name, c.role, c.yards) for c in credits] == [
        ("Michael Penix Jr.", ROLE_RUSHING_TD, 4),
        ("Younghoe Koo", ROLE_PAT, None),
    ]


def test_field_goal_with_trailing_whitespace_and_apostrophe() -> None:
    """ESPN emits trailing spaces on a large fraction of field-goal texts."""
    credits = _credits("Ka'imi Fairbairn 51 Yd Field Goal ")
    assert [(c.name, c.role, c.yards) for c in credits] == [("Ka'imi Fairbairn", ROLE_FIELD_GOAL, 51)]


def test_field_goal_without_trailing_whitespace() -> None:
    credits = _credits("Chase McLaughlin 48 Yd Field Goal")
    assert [(c.name, c.role, c.yards) for c in credits] == [("Chase McLaughlin", ROLE_FIELD_GOAL, 48)]


def test_interception_return_touchdown() -> None:
    credits = _credits("Kenny Moore II 32 Yd Interception Return (Spencer Shrader Kick)")
    assert [(c.name, c.role) for c in credits] == [
        ("Kenny Moore II", ROLE_DEFENSIVE_TD),
        ("Spencer Shrader", ROLE_PAT),
    ]


def test_fumble_recovery_touchdown() -> None:
    credits = _credits("Isaiah Rodgers 66 Yd Fumble Recovery (Will Reichard Kick)")
    assert credits[0].name == "Isaiah Rodgers"
    assert credits[0].role == ROLE_DEFENSIVE_TD


def test_punt_return_touchdown_is_not_a_defensive_td() -> None:
    """A return TD credits the returner as an offensive scorer in most leagues."""
    credits = _credits("Jaylin Lane 90 Yd Punt Return (Matt Gay Kick)")
    assert [(c.name, c.role) for c in credits] == [
        ("Jaylin Lane", ROLE_RETURN_TD),
        ("Matt Gay", ROLE_PAT),
    ]


def test_blocked_kick_parenthetical_is_a_team_not_a_kicker() -> None:
    """The trap: '(PHI)' would become an athlete named 'PHI' under a naive rule.

    This real string also contains ESPN's own typo, 'Touchown'.
    """
    credits = _credits("Blocked Kick Recovered by Jordan Davis (PHI) Jordan Davis 61 Yd Touchown Return")
    assert [(c.name, c.role, c.yards) for c in credits] == [("Jordan Davis", ROLE_DEFENSIVE_TD, 61)]
    assert not any(c.name == "PHI" for c in credits)


def test_safety_with_no_named_athlete_yields_nothing() -> None:
    assert _credits("Defensive Holding in Endzone for Safety") == []


def test_missed_pat_is_distinguished_from_a_made_one() -> None:
    """The kicker is named either way — the roles must differ."""
    credits = _credits("Emeka Egbuka 25 Yd pass from Baker Mayfield (Chase McLaughlin PAT Failed)")
    roles = {c.name: c.role for c in credits}
    assert roles["Chase McLaughlin"] == ROLE_PAT_MISSED
    assert ROLE_PAT not in roles.values()


def test_successful_two_point_conversion_credits_both_players() -> None:
    credits = _credits(
        "De'Von Achane 11 Yd pass from Tua Tagovailoa (Tua Tagovailoa Pass to Julian Hill for Two-Point Conversion)"
    )
    assert [(c.name, c.role) for c in credits] == [
        ("De'Von Achane", ROLE_RECEIVING_TD),
        ("Tua Tagovailoa", ROLE_PASSING_TD),
        ("Tua Tagovailoa", ROLE_TWO_POINT_PASS),
        ("Julian Hill", ROLE_TWO_POINT_RECEPTION),
    ]


def test_failed_two_point_conversion_names_nobody() -> None:
    credits = _credits("Garrett Wilson 33 Yd pass from Justin Fields (Two-Point Run Conversion Failed)")
    assert [(c.name, c.role) for c in credits] == [
        ("Garrett Wilson", ROLE_RECEIVING_TD),
        ("Justin Fields", ROLE_PASSING_TD),
    ]


def test_every_fixture_parses_without_raising() -> None:
    """Whatever the shape, parsing must never blow up — enrichment is optional."""
    for text in PLAYS:
        parse_scoring_play(text)


def test_unrecognised_and_empty_text_degrade_quietly() -> None:
    assert parse_scoring_play("") == []
    assert parse_scoring_play("Something ESPN invented last Tuesday") == []


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def test_exact_name_and_team_is_full_confidence() -> None:
    credits = _credits("Bijan Robinson 50 Yd pass from Michael Penix Jr. (Younghoe Koo Kick)")
    result = match_play_to_player(credits, player_name="Bijan Robinson", player_team="ATL", play_team="ATL")
    assert result.matched
    assert result.confidence == 1.0
    assert result.credit.role == ROLE_RECEIVING_TD


def test_suffix_mismatch_still_matches() -> None:
    """Yahoo may carry 'Michael Penix' where ESPN prints 'Michael Penix Jr.'."""
    credits = _credits("Michael Penix Jr. 4 Yd Rush (Younghoe Koo Kick)")
    result = match_play_to_player(credits, player_name="Michael Penix", player_team="ATL", play_team="ATL")
    assert result.matched
    assert result.confidence == 1.0


def test_wrong_team_is_rejected_outright() -> None:
    """The same surname on the other sideline is the likeliest false positive."""
    credits = _credits("Bijan Robinson 50 Yd pass from Michael Penix Jr. (Younghoe Koo Kick)")
    result = match_play_to_player(credits, player_name="Bijan Robinson", player_team="TB", play_team="ATL")
    assert not result.matched
    assert result.confidence == 0.0


def test_two_different_players_sharing_surname_and_initial_do_not_match() -> None:
    """Regression: 'Bijan Robinson' and 'Brian Robinson' are different people.

    Both are real contemporary NFL running backs. They share a surname AND a
    first initial, so comparing initials whenever surnames agree scores this at
    0.8 and confidently attributes Bijan's touchdown to Brian. The initial is
    only evidence when one side is genuinely abbreviated; two spelled-out first
    names must be compared in full.
    """
    credits = _credits("Bijan Robinson 50 Yd pass from Michael Penix Jr. (Younghoe Koo Kick)")
    result = match_play_to_player(credits, player_name="Brian Robinson", player_team="ATL", play_team="ATL")
    assert not result.matched
    assert result.confidence == pytest.approx(0.6)
    assert result.reason == "surname only"


def test_first_initial_plus_surname_matches() -> None:
    credits = _credits("Bijan Robinson 50 Yd pass from Michael Penix Jr. (Younghoe Koo Kick)")
    result = match_play_to_player(credits, player_name="B. Robinson", player_team="ATL", play_team="ATL")
    assert result.matched
    assert result.confidence == pytest.approx(0.8)


def test_kicker_matches_the_pat_credit() -> None:
    credits = _credits("Bijan Robinson 50 Yd pass from Michael Penix Jr. (Younghoe Koo Kick)")
    result = match_play_to_player(credits, player_name="Younghoe Koo", player_team="ATL", play_team="ATL")
    assert result.matched
    assert result.credit.role == ROLE_PAT


def test_team_defense_matches_by_team_not_by_defender_name() -> None:
    """Fantasy D/ST scores the return TD; the named defender is irrelevant."""
    credits = _credits("Kenny Moore II 32 Yd Interception Return (Spencer Shrader Kick)")
    result = match_play_to_player(credits, player_name="Colts", player_team="IND", play_team="IND", position="DEF")
    assert result.matched
    assert result.credit.role == ROLE_DEFENSIVE_TD


def test_team_defense_does_not_match_the_opposing_units_score() -> None:
    credits = _credits("Kenny Moore II 32 Yd Interception Return (Spencer Shrader Kick)")
    result = match_play_to_player(credits, player_name="Raiders", player_team="LV", play_team="IND", position="D/ST")
    assert not result.matched


def test_team_defense_ignores_a_plain_offensive_score() -> None:
    credits = _credits("Chase McLaughlin 48 Yd Field Goal")
    result = match_play_to_player(credits, player_name="Buccaneers", player_team="TB", play_team="TB", position="DEF")
    assert not result.matched


def test_no_credits_never_matches() -> None:
    assert not match_play_to_player([], player_name="Anyone").matched


# ---------------------------------------------------------------------------
# Scoreboard normalization
# ---------------------------------------------------------------------------


def test_normalize_scoreboard_handles_an_empty_payload() -> None:
    assert normalize_scoreboard({}) == []
    assert normalize_scoreboard({"events": []}) == []


def test_normalize_scoreboard_tolerates_a_game_with_no_competition() -> None:
    assert normalize_scoreboard({"events": [{"id": "1"}]}) == []


def test_normalize_scoreboard_extracts_teams_and_state() -> None:
    payload = {
        "events": [
            {
                "id": "401772842",
                "date": "2025-09-21T17:00Z",
                "status": {"type": {"state": "in", "completed": False, "shortDetail": "Q2 8:12"}},
                "competitions": [
                    {
                        "competitors": [
                            {"homeAway": "home", "score": "7", "team": {"abbreviation": "CLE"}},
                            {"homeAway": "away", "score": "10", "team": {"abbreviation": "GB"}},
                        ],
                        "situation": {"lastPlay": {"text": "Jordan Love pass complete"}},
                    }
                ],
            }
        ]
    }
    (game,) = normalize_scoreboard(payload)
    assert game["event_id"] == "401772842"
    assert game["state"] == "in"
    assert game["completed"] is False
    assert game["home"]["abbr"] == "CLE"
    assert game["away"]["score"] == "10"
    assert game["last_play"] == "Jordan Love pass complete"


def test_normalize_scoreboard_last_play_absent_when_not_live() -> None:
    """`situation` only exists during a live game — confirmed against a final."""
    payload = {
        "events": [
            {
                "id": "1",
                "status": {"type": {"state": "post", "completed": True}},
                "competitions": [{"competitors": []}],
            }
        ]
    }
    (game,) = normalize_scoreboard(payload)
    assert game["last_play"] is None
