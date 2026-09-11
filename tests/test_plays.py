"""Tests for the scoring-play engine.

The engine is a pure function over two consecutive polls, so every case is
reachable with hand-built snapshots — no network, no Home Assistant, no clock.

Most of these tests are about what must NOT produce an event. Synthesizing a
feed by diffing totals means several ordinary situations look exactly like
scoring unless they are explicitly handled: a restart, a week rollover, a
waiver pickup arriving with points already on the board.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from yahoo_fantasy_football.espn import match_play_to_player, parse_scoring_play
from yahoo_fantasy_football.plays import (
    LeagueSnapshot,
    MatchedPlay,
    PlayerSnapshot,
    PlayFeed,
    abbreviate_name,
    describe,
    diff_snapshots,
    dumps,
    enrich_events,
    loads,
    match_relay_play,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def player(key: str, points: float, **kw) -> PlayerSnapshot:
    return PlayerSnapshot(
        player_key=key,
        name=kw.pop("name", "Puka Nacua"),
        points=points,
        team_key=kw.pop("team_key", "461.l.1.t.1"),
        matchup_id=kw.pop("matchup_id", "m1"),
        nfl_team=kw.pop("nfl_team", "LAR"),
        position=kw.pop("position", "WR"),
        selected_position=kw.pop("selected_position", "WR"),
        **kw,
    )


def snap(week: int, taken_at: float, *players: PlayerSnapshot) -> LeagueSnapshot:
    return LeagueSnapshot(week=week, taken_at=taken_at, players={p.player_key: p for p in players})


# ---------------------------------------------------------------------------
# The ordinary case
# ---------------------------------------------------------------------------


def test_a_player_scoring_emits_one_event() -> None:
    before = snap(3, 100.0, player("p1", 12.5))
    after = snap(3, 145.0, player("p1", 18.9))

    (event,) = diff_snapshots(before, after)
    assert event.player_key == "p1"
    assert event.delta == pytest.approx(6.4)
    assert event.old_points == pytest.approx(12.5)
    assert event.new_points == pytest.approx(18.9)
    assert event.correction is False
    assert event.starter is True
    assert event.timestamp == 145.0
    assert event.week == 3


def test_float_noise_does_not_leak_into_the_delta() -> None:
    """18.9 - 12.5 is 6.399999999999999 in binary floating point."""
    before = snap(3, 100.0, player("p1", 12.5))
    after = snap(3, 145.0, player("p1", 18.9))
    (event,) = diff_snapshots(before, after)
    assert str(event.delta) == "6.4"


def test_events_sort_biggest_first_with_corrections_last() -> None:
    before = snap(
        3, 100.0, player("p1", 10.0), player("p2", 10.0, name="Sam Darnold"), player("p3", 10.0, name="Cam Little")
    )
    after = snap(
        3, 145.0, player("p1", 12.0), player("p2", 18.0, name="Sam Darnold"), player("p3", 8.0, name="Cam Little")
    )
    events = diff_snapshots(before, after)
    assert [e.player_key for e in events] == ["p2", "p1", "p3"]
    assert events[-1].correction is True


# ---------------------------------------------------------------------------
# Things that look like scoring but are not
# ---------------------------------------------------------------------------


def test_no_previous_snapshot_establishes_a_baseline_silently() -> None:
    """The first poll after a restart must not replay the whole week."""
    assert diff_snapshots(None, snap(3, 100.0, player("p1", 22.4))) == []


def test_week_rollover_emits_nothing() -> None:
    """Points reset to zero, so every player would read as a huge correction."""
    before = snap(3, 100.0, player("p1", 22.4), player("p2", 15.0, name="Sam Darnold"))
    after = snap(4, 200.0, player("p1", 0.0), player("p2", 0.0, name="Sam Darnold"))
    assert diff_snapshots(before, after) == []


def test_a_player_added_mid_week_is_not_a_scoring_play() -> None:
    """A waiver pickup arrives carrying points already scored."""
    before = snap(3, 100.0, player("p1", 12.5))
    after = snap(3, 145.0, player("p1", 12.5), player("p9", 8.4, name="Jaylin Lane"))
    assert diff_snapshots(before, after) == []


def test_a_dropped_player_is_not_a_scoring_play() -> None:
    before = snap(3, 100.0, player("p1", 12.5), player("p9", 8.4, name="Jaylin Lane"))
    after = snap(3, 145.0, player("p1", 12.5))
    assert diff_snapshots(before, after) == []


def test_a_lineup_change_alone_emits_nothing() -> None:
    """Benching a player does not change that player's points."""
    before = snap(3, 100.0, player("p1", 12.5, selected_position="WR"))
    after = snap(3, 145.0, player("p1", 12.5, selected_position="BN"))
    assert diff_snapshots(before, after) == []


def test_starting_a_benched_player_flags_the_next_event_as_a_starter() -> None:
    before = snap(3, 100.0, player("p1", 12.5, selected_position="BN"))
    after = snap(3, 145.0, player("p1", 18.9, selected_position="WR"))
    (event,) = diff_snapshots(before, after)
    assert event.starter is True


def test_bench_points_still_emit_but_are_flagged_as_bench() -> None:
    before = snap(3, 100.0, player("p1", 12.5, selected_position="BN"))
    after = snap(3, 145.0, player("p1", 18.9, selected_position="BN"))
    (event,) = diff_snapshots(before, after)
    assert event.starter is False


def test_sub_threshold_jitter_is_ignored() -> None:
    before = snap(3, 100.0, player("p1", 12.50))
    after = snap(3, 145.0, player("p1", 12.53))
    assert diff_snapshots(before, after) == []


def test_missing_points_are_skipped_not_treated_as_zero() -> None:
    before = snap(3, 100.0, PlayerSnapshot("p1", "Puka Nacua", None, "t1"))
    after = snap(3, 145.0, player("p1", 6.4))
    assert diff_snapshots(before, after) == []


# ---------------------------------------------------------------------------
# Corrections
# ---------------------------------------------------------------------------


def test_a_negative_delta_is_flagged_as_a_correction() -> None:
    before = snap(3, 100.0, player("p1", 18.9))
    after = snap(3, 145.0, player("p1", 18.6))
    (event,) = diff_snapshots(before, after)
    assert event.correction is True
    assert event.delta == pytest.approx(-0.3)


def test_corrections_are_described_as_corrections() -> None:
    before = snap(3, 100.0, player("p1", 18.9))
    after = snap(3, 145.0, player("p1", 18.6))
    (event,) = diff_snapshots(before, after)
    assert describe(event) == "P. Nacua -0.30 (stat correction)"


# ---------------------------------------------------------------------------
# The undecomposable case
# ---------------------------------------------------------------------------


def test_two_scores_in_one_interval_produce_one_combined_event() -> None:
    """Yahoo shows one larger total; the numbers alone cannot separate them.

    This is a documented limitation, not a bug — see ``enrich_events`` for how
    both play descriptions still reach the UI.
    """
    before = snap(3, 100.0, player("p1", 6.0))
    after = snap(3, 145.0, player("p1", 18.4))
    (event,) = diff_snapshots(before, after)
    assert event.delta == pytest.approx(12.4)


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_re_ingesting_the_same_transition_is_idempotent() -> None:
    """A restart restores the previous snapshot and re-diffs the same pair."""
    before = snap(3, 100.0, player("p1", 12.5))
    after = snap(3, 145.0, player("p1", 18.9))

    feed = PlayFeed()
    assert len(feed.add(diff_snapshots(before, after))) == 1
    # Same transition, different wall-clock time on the later poll.
    again = snap(3, 999.0, player("p1", 18.9))
    assert feed.add(diff_snapshots(before, again)) == []
    assert len(feed) == 1


# ---------------------------------------------------------------------------
# Enrichment
# ---------------------------------------------------------------------------


def _espn_play(text: str, team: str, **kw) -> dict:
    return {
        "id": kw.pop("id", "x1"),
        "text": text,
        "team": team,
        "period": kw.pop("period", 3),
        "clock": kw.pop("clock", "8:12"),
        "credits": parse_scoring_play(text),
    }


REAL_PASS_TD = "Bijan Robinson 50 Yd pass from Michael Penix Jr. (Younghoe Koo Kick)"


def test_enrichment_attaches_a_real_play_description() -> None:
    before = snap(3, 100.0, player("p1", 6.0, name="Bijan Robinson", nfl_team="ATL", position="RB"))
    after = snap(3, 145.0, player("p1", 12.4, name="Bijan Robinson", nfl_team="ATL", position="RB"))
    events = diff_snapshots(before, after)

    (event,) = enrich_events(events, [_espn_play(REAL_PASS_TD, "ATL")], match_play_to_player)
    assert event.enriched
    assert event.best_play.text == REAL_PASS_TD
    assert event.best_play.role == "receiving_td"
    assert describe(event) == REAL_PASS_TD


def test_one_play_enriches_every_player_it_credits() -> None:
    """A passing TD credits receiver, passer and kicker — all three events."""
    args = {"nfl_team": "ATL", "matchup_id": "m1"}
    before = snap(
        3,
        100.0,
        player("p1", 0.0, name="Bijan Robinson", position="RB", **args),
        player("p2", 0.0, name="Michael Penix", position="QB", **args),
        player("p3", 0.0, name="Younghoe Koo", position="K", **args),
    )
    after = snap(
        3,
        145.0,
        player("p1", 6.0, name="Bijan Robinson", position="RB", **args),
        player("p2", 4.0, name="Michael Penix", position="QB", **args),
        player("p3", 1.0, name="Younghoe Koo", position="K", **args),
    )
    events = enrich_events(diff_snapshots(before, after), [_espn_play(REAL_PASS_TD, "ATL")], match_play_to_player)
    assert all(e.enriched for e in events)
    roles = {e.player_name: e.best_play.role for e in events}
    assert roles == {
        "Bijan Robinson": "receiving_td",
        "Michael Penix": "passing_td",
        "Younghoe Koo": "pat",
    }


def test_a_double_score_collects_both_plays_on_the_one_event() -> None:
    """The combined-delta event still shows both descriptions."""
    before = snap(3, 100.0, player("p1", 0.0, name="Bijan Robinson", nfl_team="ATL", position="RB"))
    after = snap(3, 145.0, player("p1", 12.0, name="Bijan Robinson", nfl_team="ATL", position="RB"))
    plays = [
        _espn_play(REAL_PASS_TD, "ATL", id="a", period=2, clock="10:00"),
        _espn_play("Bijan Robinson 3 Yd Rush (Younghoe Koo Kick)", "ATL", id="b", period=3, clock="1:00"),
    ]
    (event,) = enrich_events(diff_snapshots(before, after), plays, match_play_to_player)
    assert len(event.plays) == 2
    # The banner shows the most recent of the matched set.
    assert event.best_play.play_id == "b"


def test_a_play_by_another_team_is_not_attached() -> None:
    before = snap(3, 100.0, player("p1", 0.0, name="Bijan Robinson", nfl_team="TB", position="RB"))
    after = snap(3, 145.0, player("p1", 6.0, name="Bijan Robinson", nfl_team="TB", position="RB"))
    (event,) = enrich_events(diff_snapshots(before, after), [_espn_play(REAL_PASS_TD, "ATL")], match_play_to_player)
    assert not event.enriched


def test_corrections_are_never_enriched() -> None:
    """A stat revision did not happen on the field."""
    before = snap(3, 100.0, player("p1", 12.0, name="Bijan Robinson", nfl_team="ATL", position="RB"))
    after = snap(3, 145.0, player("p1", 6.0, name="Bijan Robinson", nfl_team="ATL", position="RB"))
    (event,) = enrich_events(diff_snapshots(before, after), [_espn_play(REAL_PASS_TD, "ATL")], match_play_to_player)
    assert event.correction
    assert not event.enriched


def test_enrichment_without_plays_returns_events_unchanged() -> None:
    before = snap(3, 100.0, player("p1", 6.0))
    after = snap(3, 145.0, player("p1", 12.4))
    events = diff_snapshots(before, after)
    assert enrich_events(events, [], match_play_to_player) == events


def test_unenriched_event_falls_back_to_the_point_delta() -> None:
    before = snap(3, 100.0, player("p1", 12.5))
    after = snap(3, 145.0, player("p1", 18.9))
    (event,) = diff_snapshots(before, after)
    assert describe(event) == "P. Nacua +6.40"


# ---------------------------------------------------------------------------
# Feed behaviour
# ---------------------------------------------------------------------------


def test_feed_returns_newest_first() -> None:
    feed = PlayFeed()
    for i in range(3):
        before = snap(3, 100.0 + i, player("p1", float(i)))
        after = snap(3, 101.0 + i, player("p1", float(i) + 5))
        feed.add(diff_snapshots(before, after))
    assert [e.new_points for e in feed.recent(3)] == [7.0, 6.0, 5.0]


def test_feed_evicts_old_events_and_can_still_record() -> None:
    """Eviction must release the dedupe id, or the feed silently goes deaf."""
    feed = PlayFeed(maxlen=2)
    for i in range(5):
        before = snap(3, 100.0 + i, player("p1", float(i)))
        after = snap(3, 101.0 + i, player("p1", float(i) + 5))
        assert len(feed.add(diff_snapshots(before, after))) == 1
    assert len(feed) == 2


def test_feed_filters_by_matchup_and_team() -> None:
    before = snap(
        3,
        100.0,
        player("p1", 0.0, matchup_id="m1", team_key="t1"),
        player("p2", 0.0, name="Sam Darnold", matchup_id="m2", team_key="t2"),
    )
    after = snap(
        3,
        145.0,
        player("p1", 6.0, matchup_id="m1", team_key="t1"),
        player("p2", 6.0, name="Sam Darnold", matchup_id="m2", team_key="t2"),
    )
    feed = PlayFeed()
    feed.add(diff_snapshots(before, after))
    assert [e.player_key for e in feed.recent(matchup_id="m1")] == ["p1"]
    assert [e.player_key for e in feed.recent(team_key="t2")] == ["p2"]


def test_banner_skips_corrections_by_default() -> None:
    feed = PlayFeed()
    feed.add(diff_snapshots(snap(3, 1.0, player("p1", 0.0)), snap(3, 2.0, player("p1", 6.0))))
    feed.add(diff_snapshots(snap(3, 3.0, player("p1", 6.0)), snap(3, 4.0, player("p1", 5.7))))
    assert feed.last_play().delta == pytest.approx(6.0)
    assert feed.last_play(include_corrections=True).correction is True


def test_a_new_week_clears_the_feed() -> None:
    feed = PlayFeed()
    feed.add(diff_snapshots(snap(3, 1.0, player("p1", 0.0)), snap(3, 2.0, player("p1", 6.0))))
    assert len(feed) == 1
    feed.add(diff_snapshots(snap(4, 3.0, player("p1", 0.0)), snap(4, 4.0, player("p1", 3.0))))
    assert len(feed) == 1
    assert feed.week == 4


def test_empty_feed_has_no_last_play() -> None:
    assert PlayFeed().last_play() is None


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def test_feed_round_trips_through_storage() -> None:
    before = snap(3, 100.0, player("p1", 0.0, name="Bijan Robinson", nfl_team="ATL", position="RB"))
    after = snap(3, 145.0, player("p1", 6.0, name="Bijan Robinson", nfl_team="ATL", position="RB"))
    feed = PlayFeed()
    feed.add(enrich_events(diff_snapshots(before, after), [_espn_play(REAL_PASS_TD, "ATL")], match_play_to_player))

    restored = loads(dumps(feed))
    assert len(restored) == 1
    event = restored.recent(1)[0]
    assert event.player_name == "Bijan Robinson"
    assert event.best_play.text == REAL_PASS_TD
    assert restored.week == 3


def test_restored_feed_still_deduplicates() -> None:
    before = snap(3, 100.0, player("p1", 12.5))
    after = snap(3, 145.0, player("p1", 18.9))
    feed = PlayFeed()
    events = diff_snapshots(before, after)
    feed.add(events)

    restored = loads(dumps(feed))
    assert restored.add(events) == []


def test_unreadable_storage_yields_an_empty_feed() -> None:
    assert len(loads(None)) == 0
    assert len(loads("")) == 0
    assert len(loads("{not json")) == 0
    assert len(loads(json.dumps({"week": 3, "events": [{"garbage": True}]}))) == 0


def test_stored_payload_is_json_serialisable() -> None:
    feed = PlayFeed()
    feed.add(diff_snapshots(snap(3, 1.0, player("p1", 0.0)), snap(3, 2.0, player("p1", 6.0))))
    json.loads(dumps(feed))


# ---------------------------------------------------------------------------
# Presentation helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Puka Nacua", "P. Nacua"),
        ("Michael Penix Jr.", "M. Penix Jr."),
        ("Amon-Ra St. Brown", "A. St. Brown"),
        ("Cher", "Cher"),
        ("", ""),
    ],
)
def test_abbreviate_name(raw: str, expected: str) -> None:
    assert abbreviate_name(raw) == expected


# ---------------------------------------------------------------------------
# Stat deltas — what Yahoo itself prints under each matchup
# ---------------------------------------------------------------------------


def test_an_event_carries_what_changed_not_only_how_much():
    before = snap(1, 100.0, player("p1", 13.67, name="Drake Maye",
                                   stats={"completions": 11.0, "passingYards": 71.0}))
    after = snap(1, 145.0, player("p1", 13.96, name="Drake Maye",
                                  stats={"completions": 12.0, "passingYards": 84.0}))

    (event,) = diff_snapshots(before, after)
    assert event.stat_delta == "1 Comp, 13 Pass Yds"
    assert describe(event) == "D. Maye 1 Comp, 13 Pass Yds"


def test_a_bare_point_delta_is_the_fallback_without_stats():
    """The HTML tier carries no stat lines; the banner still has to say something."""
    before = snap(1, 100.0, player("p1", 10.0))
    after = snap(1, 145.0, player("p1", 16.4))

    (event,) = diff_snapshots(before, after)
    assert event.stat_delta == ""
    assert describe(event) == "P. Nacua +6.40"


def test_a_real_play_description_still_wins():
    """ESPN enrichment says more than either, so it stays the top tier."""
    before = snap(1, 100.0, player("p1", 10.0, stats={"receptions": 2.0}))
    after = snap(1, 145.0, player("p1", 16.4, stats={"receptions": 3.0}))

    (event,) = diff_snapshots(before, after)
    enriched = replace(event, plays=(MatchedPlay("Nacua 24 Yd pass from Stafford", "receiver", 1.0),))
    assert describe(enriched) == "Nacua 24 Yd pass from Stafford"


def test_a_correction_is_never_dressed_up_as_a_play():
    before = snap(1, 100.0, player("p1", 5.4, stats={"rushingYards": 24.0}))
    after = snap(1, 145.0, player("p1", 5.2, stats={"rushingYards": 22.0}))

    (event,) = diff_snapshots(before, after)
    assert event.correction
    assert describe(event) == "P. Nacua -0.20 (stat correction)"


def test_stat_delta_survives_a_restart():
    """History is persisted through Store; the text must come back with it."""
    before = snap(1, 100.0, player("p1", 13.67, stats={"completions": 11.0}))
    after = snap(1, 145.0, player("p1", 13.96, stats={"completions": 12.0}))

    feed = PlayFeed()
    feed.add(diff_snapshots(before, after))
    restored = loads(dumps(feed))

    assert restored.last_play().stat_delta == "1 Comp"


def test_history_written_before_stat_deltas_existed_still_loads():
    legacy = (
        '{"week": 1, "events": [{"event_id": "w1:p1:0.00->6.40", "week": 1,'
        ' "timestamp": 1.0, "player_key": "p1", "player_name": "Puka Nacua",'
        ' "team_key": "t1", "matchup_id": "m1", "nfl_team": "LAR", "position": "WR",'
        ' "delta": 6.4, "old_points": 0.0, "new_points": 6.4, "starter": true,'
        ' "correction": false, "plays": []}]}'
    )
    restored = loads(legacy)
    assert restored.last_play().stat_delta == ""


# ---------------------------------------------------------------------------
# Yahoo's own play descriptions
# ---------------------------------------------------------------------------

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _relay():
    from yahoo_fantasy_football.yahoo_redzone import parse_relay_players, parse_relay_plays

    return (
        parse_relay_plays((FIXTURES_DIR / "yahoo_relay_plays_26_2026_w1.txt").read_text()),
        parse_relay_players((FIXTURES_DIR / "yahoo_relay_players_2026_w1.txt").read_text()),
    )


def _scoring_event(player_id: str, **kw):
    from yahoo_fantasy_football.plays import ScoringEvent

    return ScoringEvent(
        event_id=f"e:{player_id}",
        week=1,
        timestamp=100.0,
        player_key=f"802904.p.{player_id}",
        player_name="Whoever",
        team_key="802904.t.1",
        matchup_id="w1.m1",
        nfl_team="Sea",
        position="WR",
        delta=1.3,
        old_points=0.0,
        new_points=1.3,
        starter=True,
        correction=False,
        **kw,
    )


def test_a_play_is_matched_by_id_not_by_name() -> None:
    """The feed writes people as [40041]; so does the event's player key."""
    plays, names = _relay()
    match = match_relay_play(_scoring_event("40041"), plays, names)

    assert match is not None
    assert "Jaxon Smith-Njigba" in match.text
    assert match.confidence == 1.0


def test_the_newest_qualifying_play_wins() -> None:
    """Points cannot be split across plays, so the latest is the best guess."""
    plays, names = _relay()
    mine = [p for p in plays if "40041" in p.player_ids]
    assert len(mine) > 1, "fixture must exercise the choice"

    match = match_relay_play(_scoring_event("40041"), plays, names)
    assert match.play_id == f"{mine[-1].game_key}.{mine[-1].sequence}"


def test_a_player_who_did_nothing_gets_no_description() -> None:
    plays, names = _relay()
    assert match_relay_play(_scoring_event("99999999"), plays, names) is None


def test_the_description_wins_over_the_stat_line() -> None:
    """A real sentence beats "1 Rec, 13 Rec Yds" — that is the whole point."""
    from yahoo_fantasy_football.plays import describe

    plays, names = _relay()
    event = _scoring_event("40041", stat_delta="1 Rec, 13 Rec Yds")
    assert describe(event).endswith("1 Rec, 13 Rec Yds")

    described = replace(event, plays=(match_relay_play(event, plays, names),))
    assert "passed to Jaxon Smith-Njigba" in describe(described)


def test_a_revision_replaces_the_text_in_place() -> None:
    """Yahoo posts terse first, fuller a moment later."""
    from yahoo_fantasy_football.plays import MatchedPlay, PlayFeed

    feed = PlayFeed()
    event = _scoring_event("40041")
    feed.add([event])
    terse = MatchedPlay(text="Sam Darnold complete for 13 yards", role="", confidence=1.0)
    feed.revise(event.event_id, (terse,))
    assert feed.last_play().plays[-1].text == terse.text

    fuller = MatchedPlay(
        text="Sam Darnold passed to Jaxon Smith-Njigba for 13 yard gain", role="", confidence=1.0
    )
    feed.revise(event.event_id, (fuller,))

    assert len(feed) == 1, "a revision must not duplicate the event"
    assert feed.last_play().plays[-1].text == fuller.text


def test_revising_an_event_that_is_not_there_is_harmless() -> None:
    from yahoo_fantasy_football.plays import PlayFeed

    assert PlayFeed().revise("nope", ()) is None
