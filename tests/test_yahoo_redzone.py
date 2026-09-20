"""Pin the GameChannel parser to payloads Yahoo actually served.

The numbers asserted here are not invented: the capture is week 1 of 2026 with
one game live, and the totals were cross-checked against Yahoo's own
StatTracker display at the moment of capture. If a change breaks these, the
computed score has stopped matching what Yahoo shows its own users.
"""

from __future__ import annotations

import json

import pytest
from conftest import FIXTURES
from yahoo_fantasy_football.yahoo_redzone import (
    GAME_STATUS,
    NFL_STAT_NAMES,
    active_game_count,
    clock_text,
    describe_delta,
    humanize_play,
    league_from_payloads,
    league_name,
    live_clubs,
    live_projection,
    parse_relay_defense,
    parse_relay_games,
    parse_relay_players,
    parse_relay_plays,
    parse_relay_stats,
    play_ids_needed,
    plays_feeds,
    redzone_url,
    relay_sequence,
    relay_url,
    remaining_fraction,
    score,
    scoring_modifiers,
    stat_line,
    team_abbr,
    team_choices,
    to_snapshot,
)

LEAGUE = "999999"


@pytest.fixture(scope="module")
def redzone() -> str:
    return (FIXTURES / "yahoo_redzone_2026_w1.json").read_text()


@pytest.fixture(scope="module")
def stats_text() -> str:
    return (FIXTURES / "yahoo_relay_stats_2026_w1.txt").read_text()


@pytest.fixture(scope="module")
def games_text() -> str:
    return (FIXTURES / "yahoo_relay_games_2026_w1.txt").read_text()


@pytest.fixture(scope="module")
def plays_text() -> str:
    return (FIXTURES / "yahoo_relay_plays_26_2026_w1.txt").read_text()


@pytest.fixture(scope="module")
def players_text() -> str:
    return (FIXTURES / "yahoo_relay_players_2026_w1.txt").read_text()


@pytest.fixture(scope="module")
def league(redzone: str, stats_text: str, games_text: str):
    return league_from_payloads(redzone, stats_text, games_text, LEAGUE, now=1000.0)


# ---------------------------------------------------------------------------
# URLs
# ---------------------------------------------------------------------------


def test_redzone_url_carries_the_league_and_format():
    url = redzone_url(LEAGUE)
    assert url.startswith("https://pub-api.fantasysports.yahoo.com/fantasy/v3/redzone/nfl?")
    assert f"league_id={LEAGUE}" in url
    assert "format=json" in url


def test_relay_url_names_the_feed():
    assert relay_url("stats") == "https://relay-stream.sports.yahoo.com/nfl/stats.txt"
    assert relay_url("plays-26") == "https://relay-stream.sports.yahoo.com/nfl/plays-26.txt"


# ---------------------------------------------------------------------------
# Relay feeds
# ---------------------------------------------------------------------------


def test_relay_sequence_read_from_the_comment_header(stats_text: str):
    assert relay_sequence(stats_text) == 150
    assert relay_sequence("no header here") is None


def test_relay_stats_key_on_yahoo_player_ids(stats_text: str):
    stats = parse_relay_stats(stats_text)
    # 40881 is Drake Maye — the same id the fantasy tier uses, which is the
    # whole reason no name matching is needed anywhere in this integration.
    assert stats["40881"]["passingYards"] == 66
    assert stats["40881"]["passingTDs"] == 1
    assert stats["40881"]["completions"] == 10


def test_a_players_stat_groups_accumulate(stats_text: str):
    """A QB who ran appears on both the ``q`` and ``r`` rows."""
    maye = parse_relay_stats(stats_text)["40881"]
    assert maye["passingAttempts"] == 12  # from ``q``
    assert maye["rushingYards"] == 36  # from ``r``


def test_relay_defense_keys_on_nfl_team_id(stats_text: str):
    defense = parse_relay_defense(stats_text)
    assert defense["26"]["pointsAllowed"] == 7
    assert defense["26"]["specialTeamsReturnYards"] == 50


def test_defense_point_bands_are_derived(stats_text: str):
    """Yahoo scores a defence on bands, not on the raw points allowed."""
    seattle = parse_relay_defense(stats_text)["26"]
    assert seattle["pointsAllowed7through13"] == 1.0
    assert seattle["pointsAllowed0"] == 0.0
    assert seattle["pointsAllowed14through20"] == 0.0


def test_shutout_bonus_needs_the_game_to_have_started():
    """Every defence allows zero points before kickoff; none has earned it."""
    not_started = parse_relay_defense("f|99|0|0|0|0|0|0|0|0|0|0|0|0|0|0|0|0|0|0|0|0|0|0|0|0")
    assert not_started["99"]["pointsAllowed0"] == 0.0

    started = parse_relay_defense("f|99|0|0|0|0|0|0|0|0|0|0|0|0|0|0|0|1|0|0|0|0|0|0|0|0")
    assert started["99"]["pointsAllowed0"] == 1.0


def test_relay_games_indexed_by_both_teams(games_text: str):
    games = parse_relay_games(games_text)
    assert games["26"] is games["17"]  # one game, reachable from either side
    assert games["26"].state == "in"
    assert games["26"].plays_id == "26"


def test_scheduled_games_read_as_pre(games_text: str):
    assert parse_relay_games(games_text)["14"].state == "pre"


def test_game_status_letters_cover_the_finished_states():
    assert GAME_STATUS["F"] == "post"
    assert GAME_STATUS["S"] == "pre"
    assert GAME_STATUS["P"] == "in"


def test_game_note_reads_from_the_asking_teams_side(games_text: str):
    game = parse_relay_games(games_text)["26"]
    home, away = game.note_for("26"), game.note_for("17")
    # Named, not numbered — the relay carries club ids, the card shows clubs.
    assert home.endswith("vs NE")
    assert away.endswith("@ Sea")
    # Same game, so the scores are the same pair the other way round.
    assert home.split()[2] == "-".join(reversed(away.split()[2].split("-")))


def test_a_player_at_half_time_says_so(games_text: str):
    """The lineup used to print "Q2 0:00" under a player whose game the card
    beside it called "Halftime"; both now speak the same rule."""
    text = games_text.replace("|P|26|3|14:52|", "|P|26|2|0:00|")
    game = parse_relay_games(text)["26"]
    assert game.note_for("26") == "Halftime 0-7 vs NE"
    assert game.note_for("17") == "Halftime 7-0 @ Sea"

    assert clock_text("3", "14:52") == "Q3 14:52"
    assert clock_text("2", "0:00") == "Halftime"
    assert clock_text("2", "00:00") == "Halftime"
    assert clock_text("4", "0:00") == "Q4 0:00", "the end of regulation is not half time"
    assert clock_text("5", "8:11") == "OT 8:11", "the feed numbers overtime as a fifth period"
    assert clock_text("", "12:00") == "12:00"


def test_comment_lines_are_not_data(stats_text: str):
    assert "" not in parse_relay_stats(stats_text)
    assert not any(k.startswith("#") for k in parse_relay_stats(stats_text))


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def test_modifiers_come_from_the_league_not_from_defaults(redzone: str):
    payload = json.loads(redzone)
    modifiers = scoring_modifiers(payload["service"]["leagues"][LEAGUE])
    assert modifiers["passingYards"] == 0.04
    assert modifiers["passingTDs"] == 4
    assert modifiers["passingInterceptions"] == -2
    assert modifiers["receptions"] == 1  # full PPR in this league


def test_non_scoring_stats_are_excluded(redzone: str):
    payload = json.loads(redzone)
    league = payload["service"]["leagues"][LEAGUE]
    league["stats"].append({"id": "1", "modifier": 99, "isScoring": False})
    assert "passingAttempts" not in scoring_modifiers(league)


def test_score_matches_yahoos_own_display(redzone: str, stats_text: str):
    """Drake Maye read 12.74 on Yahoo at the moment of capture."""
    payload = json.loads(redzone)
    modifiers = scoring_modifiers(payload["service"]["leagues"][LEAGUE])
    maye = parse_relay_stats(stats_text)["40881"]
    # 10 comp x0.25 + 66 pass yds x0.04 + 1 pass TD x4 + 36 rush yds x0.1
    assert score(maye, modifiers) == 12.74


def test_unscored_stats_contribute_nothing(redzone: str):
    payload = json.loads(redzone)
    modifiers = scoring_modifiers(payload["service"]["leagues"][LEAGUE])
    assert score({"somethingYahooInvented": 500.0}, modifiers) == 0.0


def test_stat_ids_map_onto_relay_names():
    """The two vocabularies must agree or every modifier silently misses."""
    assert NFL_STAT_NAMES[4] == "passingYards"
    assert NFL_STAT_NAMES[11] == "receptions"
    assert NFL_STAT_NAMES[31] == "pointsAllowed"


# ---------------------------------------------------------------------------
# Presentation
# ---------------------------------------------------------------------------


def test_stat_line_uses_yahoos_phrasing(stats_text: str):
    stats = parse_relay_stats(stats_text)
    assert stat_line(stats["31883"]) == "3 Rec, 26 Rec Yds"  # A.J. Brown, as shown


def test_stat_line_drops_the_noughts(stats_text: str):
    line = stat_line(parse_relay_stats(stats_text)["40881"])
    assert "0 Rec" not in line
    assert "Int" not in line


def test_stat_line_counts_the_yards_not_the_carries(stats_text: str):
    """"5 Rush, 36 Rush Yds" told a manager how many times the ball was handed
    off, which is not what they opened the lineup for. Yards and touchdowns
    score; the count of carries does not, and it led every runner's line."""
    stats = parse_relay_stats(stats_text)
    assert stats["40881"]["rushingAttempts"] == 5, "the number is still parsed and still scores"
    assert stat_line(stats["40881"]) == "10 Comp, 66 Pass Yds, 1 Pass TD, 36 Rush Yds"
    assert stat_line(stats["33508"]) == "18 Rush Yds, 1 Rec, 5 Rec Yds"
    # A scoring event's delta is a different reading and keeps every stat
    # that moved — see describe_delta.
    assert "1 Rush" in describe_delta({}, {"rushingAttempts": 1, "rushingYards": 3})


def test_a_shutout_still_prints(stats_text: str):
    """Zero points allowed is a defence's best line, not an absent one."""
    line = stat_line(parse_relay_defense(stats_text)["17"])
    assert line == "2 Sack, 30 ST Ret Yds, 0 Pts Allow"
    assert line.endswith("0 Pts Allow")


def test_defence_return_yards_are_named_not_left_as_a_bare_number():
    """A defence's score moves on kick returns; "+0.90" says nothing."""
    before = {"specialTeamsReturnYards": 71.0, "gamesPlayed": 1.0}
    after = {"specialTeamsReturnYards": 80.0, "gamesPlayed": 1.0}
    assert describe_delta(before, after) == "9 ST Ret Yds"


def test_describe_delta_says_what_happened():
    before = {"completions": 11.0, "passingYards": 71.0}
    after = {"completions": 12.0, "passingYards": 84.0}
    assert describe_delta(before, after) == "1 Comp, 13 Pass Yds"


def test_describe_delta_signs_a_correction():
    assert describe_delta({"receptionYards": 26.0}, {"receptionYards": 20.0}) == "-6 Rec Yds"


def test_describe_delta_is_empty_when_nothing_moved():
    assert describe_delta({"receptions": 3.0}, {"receptions": 3.0}) == ""


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def test_every_matchup_is_built(league):
    assert league.week == 1
    assert len(league.matchups) == 5
    assert len(league.standings) == 5
    assert not league.partial


def test_team_totals_count_starters_only(league):
    """A bench player's points are carried but never added to the team."""
    sides = [
        (m.teams[side], m.roster(side))
        for m in league.matchups
        for side in (0, 1)
    ]
    with_bench_points = [
        (team, roster)
        for team, roster in sides
        if any(p.points for p in roster if not p.starter)
    ]
    assert with_bench_points, "capture should include a scoring bench player"

    for team, roster in with_bench_points:
        assert team.points == round(sum(p.points for p in roster if p.starter), 2)
        assert team.points < round(sum(p.points for p in roster), 2)


def test_a_team_counts_the_starters_still_to_play(league):
    """What decides whether a matchup's result is in. The capture has one live
    game and fifteen scheduled, so every team still has somebody to play; a
    bench player never counts, and neither does a starter with no game."""
    for m in league.matchups:
        for side in (0, 1):
            team, roster = m.teams[side], m.roster(side)
            expected = sum(1 for p in roster if p.starter and p.game_state in ("pre", "in"))
            assert team.remaining == expected
            assert team.remaining > 0, "nobody is finished in the week-1 capture"


def test_team_defense_is_scored_from_the_team_feed(league):
    """A D/ST has ``primaryPosition: null`` and its stats live under an NFL
    team id, not the synthetic ``100000+`` fantasy id on the roster."""
    defenses = [
        p for m in league.matchups for p in m.players if p.slot == "DEF" and p.points
    ]
    assert defenses, "the Seahawks D/ST was live and scoring in this capture"
    seattle = next(p for p in defenses if p.name == "Seahawks")
    # 7 points allowed (band = 4.0) + 50 special-teams return yards (5.0).
    assert seattle.points == 9.0


def test_projections_survive_for_players_and_teams(league):
    team = league.standings[0][0]
    assert team.projected and team.projected > 0
    assert any(p.projected for m in league.matchups for p in m.players)


def test_game_state_comes_from_the_feed_not_from_english(league):
    states = {p.game_state for m in league.matchups for p in m.players}
    assert "in" in states
    assert "pre" in states


def test_a_player_yet_to_play_knows_when_their_game_kicks_off(league, games_text):
    """"vs Min" is only half the answer before the game; the roster wants "Sun 10:00 AM vs Min"."""
    from yahoo_fantasy_football.yahoo_redzone import NFL_TEAMS

    games = parse_relay_games(games_text)
    by_abbr = {abbr: games.get(club) for club, abbr in NFL_TEAMS.items()}
    for player in (p for m in league.matchups for p in m.players):
        game = by_abbr.get(player.nfl_team)
        if game is None:
            assert player.kickoff is None
            continue
        assert player.kickoff == int(game.start_time), player.name
        assert player.kickoff > 1_700_000_000, "an epoch, not a relative time"


def test_players_carry_their_stat_line(league):
    maye = next(
        p for m in league.matchups for p in m.players if p.name == "Drake Maye"
    )
    assert maye.stats["passingTDs"] == 1
    assert "1 Pass TD" in maye.stat_line
    assert maye.nfl_team == "NE"


def test_a_league_that_is_not_in_the_payload_is_an_error(redzone, stats_text, games_text):
    with pytest.raises(ValueError, match="not present"):
        league_from_payloads(redzone, stats_text, games_text, "123", now=0.0)


def test_missing_live_feeds_still_produce_a_scoreboard(redzone: str):
    """Before the first kickoff there is no stat feed; zeroes are correct."""
    league = league_from_payloads(redzone, "", "", LEAGUE, now=0.0)
    assert len(league.standings) == 5
    assert all(t.points == 0.0 for pair in league.standings for t in pair)
    assert all(t.projected for pair in league.standings for t in pair)


def test_league_name_and_team_choices(redzone: str):
    assert league_name(redzone, LEAGUE) == "Test League"
    teams = team_choices(redzone, LEAGUE)
    assert len(teams) == 10
    assert list(teams) == [str(i) for i in range(1, 11)]  # numeric, not lexical


# ---------------------------------------------------------------------------
# Snapshot handoff to the play engine
# ---------------------------------------------------------------------------


def test_snapshot_carries_stats_for_the_play_feed(league):
    snapshot = to_snapshot(league.matchups, league.week, taken_at=1.0, league_id=LEAGUE)
    maye = next(p for p in snapshot.players.values() if p.name == "Drake Maye")
    assert maye.stats["passingYards"] == 66
    assert maye.matchup_id.startswith("w1.m")
    assert maye.team_key.startswith(f"{LEAGUE}.t.")


def test_snapshot_covers_every_rostered_player(league):
    snapshot = to_snapshot(league.matchups, league.week, taken_at=1.0, league_id=LEAGUE)
    assert len(snapshot.players) == sum(len(m.players) for m in league.matchups)


def test_club_ids_become_club_names() -> None:
    assert team_abbr("29") == "Car"
    assert team_abbr("9") == "GB"
    # Yahoo never issued 31/32; an id it does not know falls back to itself
    # rather than leaving the opponent blank.
    assert team_abbr("31") == "31"


def test_live_games_are_counted_once_each(games_text: str) -> None:
    """The feed is keyed per team, so a game must not be counted twice."""
    games = parse_relay_games(games_text)
    live = {g.game_id for g in games.values() if g.state == "in"}
    assert active_game_count(games) == len(live) == 1


# ---------------------------------------------------------------------------
# Live projections
# ---------------------------------------------------------------------------


class _Game:
    """Just enough GameState for the projection maths."""

    def __init__(self, state: str, period: str = "1", clock: str = "15:00") -> None:
        self.state, self.period, self.clock = state, period, clock


def test_a_game_not_yet_played_has_all_of_itself_left() -> None:
    assert remaining_fraction(_Game("pre")) == 1.0
    assert remaining_fraction(None) == 1.0, "an unknown game is not a finished one"


def test_a_finished_game_has_nothing_left() -> None:
    assert remaining_fraction(_Game("post", "4", "0:00")) == 0.0


# The row Yahoo served for Cle at TB on 2026-09-20, held at Q4 2:00 by weather
# with the Browns up 23-19: status "U", which its own scoreboard called "Susp".
DELAYED_ROW = "g|20260920027|5|27|U|26|4|2:00|23|19|1789923600|2|3|28|27|1|3|4|23|43|0|0|1|1|3|0|1"


def test_a_delayed_game_is_stopped_not_over(games_text: str) -> None:
    """Unmapped, "U" read as unknown: the games card folded the game under
    "later this week" and the fantasy side stopped counting its players as
    still to play, so a matchup could go final with two minutes left."""
    assert GAME_STATUS["U"] == "delayed"
    game = parse_relay_games(games_text + DELAYED_ROW + "\n")["27"]
    assert game.state == "delayed"
    # The clock stands where it stopped, and that is what is left to play.
    assert remaining_fraction(game) == pytest.approx(2 / 60)
    # Nobody is on the field, so nobody has the ball.
    assert not game.has_ball("27") and not game.in_red_zone("27")
    # The blurb under a player says so, score and all.
    assert game.note_for("27") == "Delayed 19-23 vs Cle"
    assert game.note_for("5") == "Delayed 23-19 @ TB"
    # A stopped game is not a running one.
    assert active_game_count(parse_relay_games(games_text + DELAYED_ROW + "\n")) == 1
    assert "5" not in live_clubs(parse_relay_games(games_text + DELAYED_ROW + "\n"))


def test_a_starter_in_a_delayed_game_is_still_to_play() -> None:
    """The matchup's "final" flag is remaining == 0 on both sides; a delayed
    game's starters must hold it open."""
    from yahoo_fantasy_football.yahoo_redzone import _team

    def player(state):
        return type("P", (), {"game_state": state, "points": 1.0, "live_projected": 1.0, "starter": True})()

    team = _team({"id": "1", "name": "x", "projectedPoints": 0}, [player("post"), player("delayed"), player("in")])
    assert team.remaining == 2


def test_the_clock_drives_the_remaining_fraction() -> None:
    # 14:57 left in the 2nd: one quarter gone plus three seconds.
    assert remaining_fraction(_Game("in", "2", "14:57")) == pytest.approx(44.95 / 60)
    assert remaining_fraction(_Game("in", "1", "15:00")) == 1.0
    assert remaining_fraction(_Game("in", "4", "0:00")) == 0.0


def test_overtime_projects_nothing_further() -> None:
    """No scheduled time left, so the pro-rata share is zero, not negative."""
    assert remaining_fraction(_Game("in", "5", "10:00")) == pytest.approx(10 / 60)
    assert remaining_fraction(_Game("in", "9", "0:00")) == 0.0


def test_the_formula_matches_yahoos_own_live_numbers() -> None:
    """Read off StatTracker at a known clock — these are Yahoo's figures."""
    mid = _Game("in", "2", "14:57")
    assert live_projection(6.50, 19.97, mid) == 21.46   # P. Nacua
    assert live_projection(9.80, 13.94, mid) == 20.24   # K. Williams
    assert live_projection(3.00, 6.99, mid) == 8.24     # E. Pineiro


def test_a_player_yet_to_play_is_worth_their_projection() -> None:
    assert live_projection(0.0, 17.99, _Game("pre")) == 17.99


def test_a_player_whose_game_is_over_is_worth_what_they_scored() -> None:
    assert live_projection(5.60, 14.32, _Game("post", "4", "0:00")) == 5.60


def test_a_player_with_no_projection_falls_back_to_their_points() -> None:
    assert live_projection(4.2, None, _Game("in", "2", "7:00")) == 4.2
    assert live_projection(None, None, None) is None


# ---------------------------------------------------------------------------
# Possession and the red zone
# ---------------------------------------------------------------------------


def test_the_club_with_the_ball_is_identified(games_text: str) -> None:
    game = parse_relay_games(games_text)["26"]
    assert game.has_ball("17"), "the feed says 17 has it"
    assert not game.has_ball("26")


def test_possession_is_only_a_thing_while_the_game_is_running(games_text: str) -> None:
    """The row keeps the last drive's possession after the whistle."""
    game = parse_relay_games(games_text)["26"]
    game.status = "F"
    assert not game.has_ball("17")


def test_the_red_zone_is_inside_the_opponents_twenty(games_text: str) -> None:
    game = parse_relay_games(games_text)["26"]
    assert game.yards_to_goal == "70"
    assert not game.in_red_zone("17"), "70 yards out is not the red zone"

    game.yards_to_goal = "20"
    assert game.in_red_zone("17"), "exactly the 20 counts"
    game.yards_to_goal = "3"
    assert game.in_red_zone("17")
    game.yards_to_goal = "21"
    assert not game.in_red_zone("17")


def test_the_red_zone_needs_the_ball(games_text: str) -> None:
    game = parse_relay_games(games_text)["26"]
    game.yards_to_goal = "5"
    assert not game.in_red_zone("26"), "their opponent is on the 5, not them"


def test_clubs_in_progress_are_listed_by_abbreviation(games_text: str) -> None:
    clubs = live_clubs(parse_relay_games(games_text))
    assert clubs == frozenset({"NE", "Sea"}), clubs


# ---------------------------------------------------------------------------
# Play descriptions
# ---------------------------------------------------------------------------


def test_plays_parse_oldest_first_with_everyone_named(plays_text: str) -> None:
    plays = parse_relay_plays(plays_text)
    assert len(plays) == 80
    assert [p.sequence for p in plays] == sorted(p.sequence for p in plays)
    assert plays[1].player_ids == ("42654", "29298")
    assert plays[1].period == "1" and plays[1].clock == "14:55"


def test_a_play_carries_the_situation_it_was_run_from(plays_text: str) -> None:
    """Pre-snap down, distance and spot — blank on anything that is not a snap."""
    from yahoo_fantasy_football.league_state import down_and_distance

    plays = {p.sequence: p for p in parse_relay_plays(plays_text)}
    kickoff, first_snap, punt = plays[1], plays[2], plays[8]
    assert (kickoff.down, kickoff.distance, kickoff.yards_to_goal) == ("0", "0", "0")
    assert down_and_distance(kickoff.down, kickoff.distance, kickoff.yards_to_goal) == ""
    assert (first_snap.down, first_snap.distance, first_snap.yards_to_goal) == ("1", "10", "76")
    assert down_and_distance(first_snap.down, first_snap.distance, first_snap.yards_to_goal) == "1st & 10"
    assert down_and_distance(punt.down, punt.distance, punt.yards_to_goal) == "4th & 11"


def test_a_two_sentence_play_keeps_both_halves(plays_text: str) -> None:
    """A punt and its return arrive in two columns, not one."""
    punt = next(p for p in parse_relay_plays(plays_text) if "punted" in p.text)
    assert "|" in punt.text, "the split must not be thrown away at parse time"


def test_the_player_dictionary_resolves_ids(players_text: str) -> None:
    names = parse_relay_players(players_text)
    assert names["42654"] == "Jadarian Price"
    assert len(names) > 100, "everyone in today's games, not just the rostered ones"


def test_a_play_reads_as_english(plays_text: str, players_text: str) -> None:
    names = parse_relay_players(players_text)
    plays = parse_relay_plays(plays_text)
    assert (
        humanize_play(plays[2].text, names)
        == "Sam Darnold passed to Jaxon Smith-Njigba down the middle for 13 yard gain"
    )
    punt = next(p for p in plays if "punted" in p.text)
    assert humanize_play(punt.text, names) == (
        "Michael Dickson punted for 43 yards. Marcus Jones returned punt for no gain"
    )


def test_the_tackler_is_left_out_unless_the_tackle_is_the_play() -> None:
    """Who made the tackle means nothing in fantasy terms, so a clause that is
    purely the tackler goes. One that says more keeps it — there the tackle IS
    what happened."""
    names = {"1": "Jadarian Price", "2": "Pat Surtain II", "3": "Zach Allen"}

    assert humanize_play("[1] rushed to the right for 13 yard gain, tackled by [2]", names) == (
        "Jadarian Price rushed to the right for 13 yard gain"
    )
    assert humanize_play("[1] rushed for 2 yard gain, tackled by [2] and [3]", names) == (
        "Jadarian Price rushed for 2 yard gain"
    )
    # More than attribution: the tackle produced the result.
    assert humanize_play(
        "[1] rushed to the left for 3 yard loss, tackled by [2] in the end zone for a safety",
        names,
    ) == "Jadarian Price rushed to the left for 3 yard loss, tackled by Pat Surtain II in the end zone for a safety"
    # The whole sentence is the tackle — observed live as a play's entire
    # text, and it printed as a line about who tackled the manager's receiver.
    # Empty, so the event falls back to its stat line instead.
    assert humanize_play("tackled by [2]", names) == ""
    assert humanize_play("[1] punted for 40 yards|tackled by [2]", names) == (
        "Jadarian Price punted for 40 yards"
    )
    # Dropping the tackler does not rescue a clause naming a stranger.
    assert humanize_play("[1] rushed for 4 yard gain, fumbled, recovered by [9]", names) == (
        "Jadarian Price rushed for 4 yard gain, fumbled"
    )


def test_a_clause_naming_a_stranger_is_dropped_whole(plays_text: str) -> None:
    """Better a shorter sentence than "tackled by" with nothing after it."""
    plays = parse_relay_plays(plays_text)
    assert humanize_play(plays[1].text, {"42654": "Jadarian Price"}) == (
        "Jadarian Price rushed to the right for 13 yard gain"
    )


def test_a_play_says_whose_names_it_needs() -> None:
    """The ids whose absence from the dictionary would cost a clause — so the
    tackler is not one, and a subject nobody knows is the whole play gone."""
    assert play_ids_needed("[1] rushed to the right for 13 yard gain, tackled by [2]") == ("1",)
    assert play_ids_needed("[1] passed to [3] to the left for 30 yard gain, tackled by [2] and [4]") == (
        "1",
        "3",
    )
    # More than attribution: that tackle is kept, so its name is needed.
    assert play_ids_needed(
        "[1] rushed to the left for 3 yard loss, tackled by [2] in the end zone for a safety"
    ) == ("1", "2")
    # Both sentences of a two-sentence play; a repeat is reported once.
    assert play_ids_needed("[1] pass intercepted|[5] intercepted [1] for 5 yards") == ("1", "5")
    assert play_ids_needed("Chicago committed 5 yard penalty (False Start)") == ()


def test_stored_text_loses_its_tackler_too() -> None:
    """History persisted before the rule existed still carries the tackler."""
    from yahoo_fantasy_football.yahoo_redzone import strip_tackler

    assert strip_tackler("Jadarian Price rushed to the right for 13 yard gain, tackled by Pat Surtain II") == (
        "Jadarian Price rushed to the right for 13 yard gain"
    )
    # Apostrophes, hyphens, initials, suffixes, two tacklers.
    for tackler in ("L'Jarius Sneed", "Amon-Ra St. Brown", "T.J. Watt", "Kenneth Walker III",
                    "Odell Beckham Jr.", "Zach Allen and Pat Surtain II"):
        assert strip_tackler(f"Bo Nix rushed for 2 yard gain, tackled by {tackler}") == (
            "Bo Nix rushed for 2 yard gain"
        ), tackler
    # More than attribution stays.
    kept = "Bo Nix rushed for 3 yard loss, tackled by Zach Allen in the end zone for a safety"
    assert strip_tackler(kept) == kept
    # The tackle-only text observed live goes entirely; sentences survive around it.
    assert strip_tackler("tackled by L'Jarius Sneed") == ""
    assert strip_tackler("Matt Araiza punted for 59 yards. Marvin Mims Jr. returned punt for 17 yards") == (
        "Matt Araiza punted for 59 yards. Marvin Mims Jr. returned punt for 17 yards"
    )
    # A sentence after the tackle — stored text from 2026-09-14 — keeps the penalty.
    assert strip_tackler(
        "Dak Prescott passed to Ryan Flournoy for 17 yard gain, tackled by Jevon Holland. "
        "NY Giants committed 15 yard penalty (Unnecessary Roughness)"
    ) == "Dak Prescott passed to Ryan Flournoy for 17 yard gain. NY Giants committed 15 yard penalty (Unnecessary Roughness)"
    assert strip_tackler("Bo Nix rushed for 2 yard gain, tackled by Amon-Ra St. Brown. Denver committed 5 yard penalty") == (
        "Bo Nix rushed for 2 yard gain. Denver committed 5 yard penalty"
    )
    assert strip_tackler("tackled by P.J. Locke. Dallas committed 15 yard penalty (Face Mask)") == (
        "Dallas committed 15 yard penalty (Face Mask)"
    )


def test_only_live_games_offer_a_play_feed(games_text: str) -> None:
    feeds = plays_feeds(parse_relay_games(games_text))
    assert feeds == {"NE": "26", "Sea": "26"}, feeds


# ---------------------------------------------------------------------------
# The NFL slate, per game
# ---------------------------------------------------------------------------


def test_games_in_order_returns_each_game_once() -> None:
    """The per-team dict holds every game twice; a slate wants it once."""
    from yahoo_fantasy_football.yahoo_redzone import games_in_order, parse_relay_games

    games = parse_relay_games((FIXTURES / "yahoo_relay_games_2026_w1.txt").read_text())
    slate = games_in_order(games)

    ids = [g.game_id for g in slate]
    assert len(ids) == len(set(ids)), "each game exactly once"
    assert len(slate) * 2 >= len(games), "and no game dropped on the way"


def test_games_in_order_keeps_games_no_rostered_player_touches() -> None:
    """The whole point of a league-wide view — see NOTES #13."""
    from yahoo_fantasy_football.yahoo_redzone import games_in_order, parse_relay_games

    games = parse_relay_games((FIXTURES / "yahoo_relay_games_2026_w1.txt").read_text())
    slate = games_in_order(games)
    # Every distinct game id in the raw feed survives into the slate.
    raw = {
        line.split("|")[1]
        for line in (FIXTURES / "yahoo_relay_games_2026_w1.txt").read_text().splitlines()
        if line.startswith("g|")
    }
    assert {g.game_id for g in slate} == raw


def test_games_in_order_is_stable_across_calls() -> None:
    """A list that reshuffles under a reader is worse than an arbitrary one."""
    from yahoo_fantasy_football.yahoo_redzone import games_in_order, parse_relay_games

    games = parse_relay_games((FIXTURES / "yahoo_relay_games_2026_w1.txt").read_text())
    assert [g.game_id for g in games_in_order(games)] == [g.game_id for g in games_in_order(games)]


def test_games_in_order_sorts_by_kickoff() -> None:
    from yahoo_fantasy_football.yahoo_redzone import games_in_order, parse_relay_games

    games = parse_relay_games((FIXTURES / "yahoo_relay_games_2026_w1.txt").read_text())
    starts = [int(g.start_time) for g in games_in_order(games) if str(g.start_time).isdigit()]
    assert starts == sorted(starts)


# ---------------------------------------------------------------------------
# Games the relay forgets
# ---------------------------------------------------------------------------
#
# The relay restarts each morning with the schedule only: last night's game is
# back to ``S``, 0-0, no clock — while its line-score rows are still there and
# still sum to the result. ``yahoo_relay_games_2026_w2_reset.txt`` is that feed,
# captured the morning after Det at Buf (2026-09-18).

DET_AT_BUF = "20260917002"
DET_AT_BUF_KICKOFF = 1789690500


@pytest.fixture(scope="module")
def reset_games_text() -> str:
    return (FIXTURES / "yahoo_relay_games_2026_w2_reset.txt").read_text()


def test_line_scores_ride_on_the_h_rows(games_text: str, reset_games_text: str) -> None:
    from yahoo_fantasy_football.yahoo_redzone import parse_relay_games

    # Live, third quarter: three periods so far, the current one included.
    live = parse_relay_games(games_text)["26"]
    assert live.quarters == {"17": (0, 7, 0), "26": (0, 0, 0)}
    # The morning after: four periods each, and they sum to the result.
    done = parse_relay_games(reset_games_text)["2"]
    assert done.quarters == {"8": (0, 10, 7, 14), "2": (14, 13, 7, 7)}
    # A game that has not kicked off has no line score at all.
    assert parse_relay_games(reset_games_text)["12"].quarters == {}


def test_the_restarted_relay_lists_last_nights_game_as_scheduled(reset_games_text: str) -> None:
    """The raw parse, pinned so the settling below is testing a real shape."""
    from yahoo_fantasy_football.yahoo_redzone import parse_relay_games

    game = parse_relay_games(reset_games_text)["2"]
    assert game.game_id == DET_AT_BUF
    assert game.state == "pre"
    assert (game.away_score, game.home_score) == ("0", "0")


def test_a_full_line_score_settles_a_forgotten_game_from_the_feed_alone(reset_games_text: str) -> None:
    from yahoo_fantasy_football.yahoo_redzone import parse_relay_games, settle_forgotten_games

    games = parse_relay_games(reset_games_text)
    # A minute after kickoff, so the clock alone could not have decided this.
    settled = settle_forgotten_games(games, now=DET_AT_BUF_KICKOFF + 60)

    assert [g.game_id for g in settled] == [DET_AT_BUF]
    game = games["2"]
    assert game.state == "post"
    assert (game.away_score, game.home_score) == ("31", "41")
    assert game.note_for("2") == "Final W 41-31 vs Det"
    assert game.note_for("8") == "Final L 31-41 @ Buf"
    # The fifteen games still to come are untouched.
    assert sum(1 for g in games.values() if g.state == "pre") == 30


def test_settling_is_idempotent(reset_games_text: str) -> None:
    from yahoo_fantasy_football.yahoo_redzone import parse_relay_games, settle_forgotten_games

    games = parse_relay_games(reset_games_text)
    settle_forgotten_games(games, now=DET_AT_BUF_KICKOFF + 60)
    assert settle_forgotten_games(games, now=DET_AT_BUF_KICKOFF + 60) == []
    assert games["2"].home_score == "41"


def _scheduled(start: int, quarters: dict[str, tuple[int, ...]] | None = None):
    """A ``g`` row the relay calls scheduled, with optional line-score rows."""
    from yahoo_fantasy_football.yahoo_redzone import GameState

    game = GameState(["g", "2026092101", "19", "14", "S", "0", "0", "", "0", "0", str(start),
                      "1", "10", "0", "0"])
    game.quarters = dict(quarters or {})
    return game


def test_a_forgotten_game_without_a_line_score_is_final_with_no_score() -> None:
    from yahoo_fantasy_football.yahoo_redzone import FORGOTTEN_AFTER_SECONDS, settle_forgotten_games

    start = 1_790_036_100
    game = _scheduled(start)
    games = {"19": game, "14": game}

    # Up to the threshold it is what the feed says: still to come.
    assert settle_forgotten_games(games, now=start + FORGOTTEN_AFTER_SECONDS - 1) == []
    assert game.state == "pre"

    assert settle_forgotten_games(games, now=start + FORGOTTEN_AFTER_SECONDS) == [game]
    assert game.state == "post"
    # Blank, not the feed's 0-0: "Final" is known, the score is not.
    assert (game.away_score, game.home_score) == ("", "")
    assert game.note_for("14") == "Final vs NYG"
    assert game.note_for("19") == "Final @ LAR"


def test_a_partial_line_score_does_not_settle_a_game_by_itself() -> None:
    """Three periods is a game in progress, whatever the status letter says."""
    from yahoo_fantasy_football.yahoo_redzone import settle_forgotten_games

    start = 1_790_036_100
    game = _scheduled(start, {"19": (7, 0, 3), "14": (0, 10, 0)})
    assert settle_forgotten_games({"19": game, "14": game}, now=start + 60) == []
    assert game.state == "pre"


def test_an_overtime_line_score_settles_as_final_overtime() -> None:
    from yahoo_fantasy_football.yahoo_redzone import settle_forgotten_games

    start = 1_790_036_100
    game = _scheduled(start, {"19": (7, 0, 3, 7, 0), "14": (0, 10, 0, 7, 3)})
    settle_forgotten_games({"19": game, "14": game}, now=start + 60)
    assert game.status == "FO"
    assert game.state == "post"
    assert (game.away_score, game.home_score) == ("17", "20")


def test_a_kickoff_the_feed_left_unknown_cannot_be_stale() -> None:
    """``0`` is the relay's "unknown", and 0 is a very long time ago."""
    from yahoo_fantasy_football.yahoo_redzone import settle_forgotten_games

    game = _scheduled(0)
    assert settle_forgotten_games({"19": game, "14": game}, now=1_790_036_100) == []
    assert game.state == "pre"


def test_league_from_payloads_settles_forgotten_games_before_the_rosters(
    redzone: str, stats_text: str, reset_games_text: str
) -> None:
    """The morning after, seen from a roster: played, not "not yet".

    A player left at "pre" is projected for their whole day on top of what
    they scored, and counted among the starters still to play — which is what
    the FFL card showed on 2026-09-18 (81 scored + 140 projected = 221 "live").
    """
    from yahoo_fantasy_football.yahoo_redzone import league_from_payloads

    data = league_from_payloads(redzone, stats_text, reset_games_text, LEAGUE, now=DET_AT_BUF_KICKOFF + 60)

    (game,) = [g for g in data.nfl_games if g.game_id == DET_AT_BUF]
    assert game.state == "post"
    assert (game.away_score, game.home_score) == ("31", "41")

    players = {p.player_id: p for m in data.matchups for p in m.players}
    allen = players["30977"]  # Josh Allen, Buf
    assert allen.game_state == "post"
    assert allen.game_note == "Final W 41-31 vs Det"
    # Nothing left to play, so the live projection is what he has, whatever
    # the pre-game projection was.
    assert allen.live_projected == allen.points
    # A club with a game still to come reads as before.
    assert players["40900"].game_state == "pre"  # Caleb Williams, Chi
    assert data.active_games == 0
