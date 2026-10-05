"""The games card's scoring banner, decided once by the integration."""

from __future__ import annotations

from types import SimpleNamespace

from yahoo_fantasy_football.score_banners import BANNER_SECONDS, ScoreBanners

DET, BUF = "8", "2"


def _game(away=0, home=0, team=DET, state="in", game_id="g1"):
    """A games-feed row, Detroit at Buffalo, as ``GameState`` exposes it."""
    return SimpleNamespace(
        game_id=game_id,
        state=state,
        away=DET,
        home=BUF,
        team_with_ball=team,
        away_score=str(away),
        home_score=str(home),
    )


def _after(before, after, now=100.0):
    banners = ScoreBanners()
    banners.observe([before], now - 10)
    banners.observe([after], now)
    return banners.active(now).get("g1")


def test_a_jump_of_six_to_eight_is_a_touchdown_for_the_club_that_scored() -> None:
    for points in (6, 7, 8):
        banner = _after(_game(17, 24), _game(17 + points, 24))
        assert (banner["kind"], banner["abbr"]) == ("TOUCHDOWN", "Det")


def test_three_is_a_field_goal() -> None:
    assert _after(_game(17, 24), _game(17, 27))["kind"] == "FIELD GOAL"


def test_two_to_the_defence_is_a_safety_and_two_to_the_offence_is_only_the_try() -> None:
    assert _after(_game(17, 24, team=DET), _game(17, 26))["kind"] == "SAFETY"
    assert _after(_game(17, 24, team=DET), _game(19, 24)) is None
    assert _after(_game(17, 24, team="0"), _game(17, 26)) is None, "nobody had the ball"


def test_an_extra_point_a_correction_or_a_gap_is_no_banner() -> None:
    assert _after(_game(17, 24), _game(18, 24)) is None
    assert _after(_game(17, 24), _game(14, 24)) is None
    assert _after(_game(17, 24), _game(27, 24)) is None


def test_scores_already_on_the_board_at_startup_are_no_banner() -> None:
    banners = ScoreBanners()
    banners.observe([_game(21, 24)], 100.0)
    assert banners.active(100.0) == {}


def test_only_a_game_in_progress_raises_one() -> None:
    assert _after(_game(17, 24), _game(23, 24, state="post")) is None


def test_the_banner_carries_its_window_and_comes_down_at_the_end_of_it() -> None:
    banners = ScoreBanners()
    banners.observe([_game(17, 24)], 90.0)
    banners.observe([_game(24, 24)], 100.0)
    assert banners.active(100.0)["g1"] == {
        "kind": "TOUCHDOWN",
        "abbr": "Det",
        "at": 100.0,
        "until": 100.0 + BANNER_SECONDS,
    }
    # The extra point that follows leaves it up, on its original clock.
    banners.observe([_game(25, 24)], 110.0)
    assert banners.active(110.0)["g1"]["at"] == 100.0
    assert "g1" in banners.active(100.0 + BANNER_SECONDS - 1)
    banners.observe([_game(25, 24)], 100.0 + BANNER_SECONDS)
    assert banners.active(100.0 + BANNER_SECONDS) == {}


def test_a_new_scoring_play_replaces_the_banner() -> None:
    banners = ScoreBanners()
    banners.observe([_game(17, 24)], 90.0)
    banners.observe([_game(24, 24)], 100.0)
    banners.observe([_game(24, 27)], 115.0)
    banner = banners.active(115.0)["g1"]
    assert (banner["kind"], banner["abbr"], banner["at"]) == ("FIELD GOAL", "Buf", 115.0)


def test_the_games_card_row_carries_the_banner() -> None:
    from yahoo_fantasy_football.league_state import nfl_game_rows

    game = _game(24, 24)
    game.plays_id, game.clock, game.period, game.start_time, game.quarters = "2", "9:00", "2", 0, {}
    game.down, game.distance, game.yards_to_goal = "1", "10", "75"
    game.has_ball = lambda club: club == DET
    game.in_red_zone = lambda club: False
    data = SimpleNamespace(nfl_games=[game])
    banner = {"kind": "TOUCHDOWN", "abbr": "Det", "at": 100.0, "until": 130.0}

    (row,) = nfl_game_rows(data, banners={"g1": banner})
    assert row["score_banner"] == banner
    (row,) = nfl_game_rows(data)
    assert row["score_banner"] is None
