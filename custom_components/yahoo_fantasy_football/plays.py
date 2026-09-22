"""Scoring-play engine — turn consecutive Yahoo polls into scoring events.

Yahoo's Fantasy API publishes point *totals*, never events. There is no
play-by-play, no event feed, and no "what just happened" endpoint. So the feed
is synthesized: poll the league, diff every rostered player's points against the
previous poll, and emit an event for each change.

That has one large advantage over reading real NFL play-by-play — the numbers
are exact to *this* league's scoring settings, and every point counts, not just
touchdowns. A three-yard reception in a PPR league is a real event here and is
invisible to any play-by-play source.

It also has three limits worth stating plainly:

* **Latency floor.** Yahoo's own live scoring trails the play by roughly
  30-60 s. Nothing downstream can be fresher than its source.
* **No decomposition.** If a player scores twice inside one poll interval,
  Yahoo shows one larger total and the two plays cannot be separated from the
  numbers alone. One event is emitted carrying the combined delta; ESPN
  enrichment can attach both descriptions to it (see ``enrich_events``).
* **Corrections look like plays.** Yahoo revises stats during and after games,
  which produces negative deltas. These are flagged, never presented as scores.
* **One play lands in pieces.** Yahoo's stat feed moves a category at a time,
  a poll apart: a 19-yard catch arrives as ``1 Rec`` (+1.00), then ``19 Rec
  Yds`` (+1.90), then a re-measured ``1 Rec Yds`` (+0.10) — three events for
  one play, each captioned with the same sentence once the text lands
  (observed live 2026-09-21, on every reception of the night). The feed folds
  a later piece into the row already showing its play, so one play is one row
  (see ``PlayFeed.add`` and ``PlayFeed.fold``).

This module is pure — no network, no Home Assistant, no clock of its own
(timestamps arrive on the snapshots). All of it is unit-testable.
"""

from __future__ import annotations

import json
import logging
from collections import deque
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

_LOGGER = logging.getLogger(__name__)

# Ignore changes smaller than this. Yahoo carries two decimal places and
# occasionally jitters the last one; a tenth of a point is below anything a
# scoring system actually awards.
MIN_DELTA = 0.05

# Per-week history depth. A busy Sunday in a 12-team league produces a few
# hundred events; this keeps the recent ones without unbounded growth.
DEFAULT_HISTORY = 200

# Lineup slots whose points do not count toward the fantasy team's score.
BENCH_SLOTS = frozenset({"BN", "IR", "IR+", "IR-R", "NA"})

# How long after an event is raised the text of its play can still be on the
# way. Measured at 15-20 s (2026-09-17); a minute is the margin for a slow
# night. An event still without a play of its own after this, and with none
# in the feed to wait for, was a piece of the play before it.
PLAY_TEXT_LAG_SECONDS = 60.0

# The longest a play's stats have been seen to keep landing after the first
# piece: a 3-yard rush credited as 2 yards, and the third a hundred seconds
# later (2026-09-21). A piece further than this from its play is a stat
# correction in all but name, and stays its own row.
SPLIT_TICK_SECONDS = 180.0


class Matcher(Protocol):
    """Signature of ``espn.match_play_to_player``.

    Passed in rather than imported so this module keeps zero dependency on the
    ESPN layer: enrichment is optional, and the engine must be usable (and
    testable) without it.
    """

    def __call__(
        self,
        credits: list[Any],
        *,
        player_name: str,
        player_team: str | None = ...,
        play_team: str | None = ...,
        position: str | None = ...,
        threshold: float = ...,
    ) -> Any: ...


@dataclass(frozen=True)
class PlayerSnapshot:
    """One rostered player at one poll."""

    player_key: str
    name: str
    points: float
    team_key: str
    """Fantasy team that rosters the player."""
    matchup_id: str | None = None
    nfl_team: str | None = None
    position: str | None = None
    selected_position: str | None = None
    """Lineup slot this week — ``BN`` for bench, ``WR``/``QB``/... for starters."""
    stats: dict[str, float] = field(default_factory=dict, compare=False)
    """Raw stat line, when the source carries one.

    Present from the GameChannel tier (:mod:`yahoo_redzone`), empty from the
    HTML tier. When two consecutive snapshots both have it, the event can say
    *what* happened rather than only how much it was worth.
    """

    @property
    def starter(self) -> bool:
        return (self.selected_position or "").upper() not in BENCH_SLOTS


@dataclass(frozen=True)
class LeagueSnapshot:
    """Every rostered player in the league at one poll."""

    week: int
    taken_at: float
    """Epoch seconds, supplied by the caller — this module has no clock."""
    players: dict[str, PlayerSnapshot] = field(default_factory=dict)


@dataclass(frozen=True)
class MatchedPlay:
    """An ESPN scoring play attributed to the player behind an event."""

    text: str
    role: str
    confidence: float
    period: int | None = None
    clock: str | None = None
    play_id: str | None = None


@dataclass(frozen=True)
class ScoringEvent:
    """A change in one player's fantasy points between two polls."""

    event_id: str
    week: int
    timestamp: float
    player_key: str
    player_name: str
    team_key: str
    matchup_id: str | None
    nfl_team: str | None
    position: str | None
    delta: float
    old_points: float
    new_points: float
    starter: bool
    correction: bool
    plays: tuple[MatchedPlay, ...] = ()
    stat_delta: str = ""
    """What changed, in Yahoo's own phrasing — ``1 Comp, 13 Pass Yds``.

    Empty when the source carries no stat line. This is the text Yahoo itself
    prints under each matchup on GameChannel, and it is preferred over a bare
    point delta by :func:`describe`.
    """
    play_floor: int | None = None
    """The newest play already in the game's feed when this event was raised.

    Only a play numbered above it can be this event's description. Yahoo's
    stat feed moves 15-20 s before the play's text reaches the play feed
    (measured live 2026-09-17), so at the moment an event is raised the play
    it came from is usually not there yet — and the newest play that mentions
    the player is their *previous* one. Without the floor that previous play
    was matched, pinned, and shown as the description of a play it was not.
    ``None`` when the feed's state was unknown, which falls back to newest-wins.
    """
    absorbed: tuple[str, ...] = ()
    """Ids of the later pieces of this play folded into this event.

    Kept so a restart that re-ingests the transition behind a piece does not
    raise it again as a row of its own: the feed treats these ids as seen.
    """

    @property
    def enriched(self) -> bool:
        return bool(self.plays)

    @property
    def best_play(self) -> MatchedPlay | None:
        """The play to show in a banner — most recent of the matched set."""
        return self.plays[-1] if self.plays else None


# ---------------------------------------------------------------------------
# Diffing
# ---------------------------------------------------------------------------


def _event_id(week: int, player_key: str, old: float, new: float) -> str:
    """Deterministic id for one transition.

    Deliberately excludes the timestamp so that re-ingesting the same pair of
    snapshots — which happens whenever Home Assistant restarts and restores the
    previous snapshot from storage — is idempotent rather than duplicating the
    whole feed.
    """
    return f"w{week}:{player_key}:{old:.2f}->{new:.2f}"


def _is_correction(delta: float, before: dict[str, float], after: dict[str, float]) -> bool:
    """Whether a point DROP is Yahoo revising a stat, or the player doing something bad.

    A negative delta is not automatically a correction, and treating it as one
    hid real plays: an interception, a lost fumble, a sack taken, a rush for a
    loss all cost points and all belong on the card. A quarterback throwing a
    pick is exactly the kind of thing someone watching a matchup wants to see.

    The tell is the direction of the STATS, not of the points. A real play ADDS
    something — interceptions 0 -> 1, and even a rush for -3 yards still adds an
    attempt — while a correction only ever walks numbers back, because it is
    unwinding something already counted.

    With no stat line to reason from (the HTML tier carries none) a drop stays a
    correction, which is the conservative read: better a missing play than a
    phantom one.
    """
    if delta >= 0:
        return False
    return not any(
        after.get(name, 0.0) > before.get(name, 0.0) for name in set(after) | set(before)
    )


def diff_snapshots(
    previous: LeagueSnapshot | None,
    current: LeagueSnapshot,
    *,
    min_delta: float = MIN_DELTA,
) -> list[ScoringEvent]:
    """Emit one event per player whose points changed between two polls.

    Returns ``[]`` — never a burst of spurious events — for the cases that look
    like scoring but aren't:

    * **No previous snapshot.** The first poll after a restart establishes a
      baseline; every player would otherwise "score" their week-to-date total.
    * **Week rollover.** Points reset to zero, so diffing across a week boundary
      would emit a large negative correction for every player in the league.
    * **A player appearing mid-week.** A waiver pickup arrives carrying points
      already scored; there is no prior reading, so no event.
    * **A lineup change.** Moving a player between the bench and the starting
      lineup does not change that player's points.
    """
    if previous is None:
        return []
    if previous.week != current.week:
        _LOGGER.debug("Week rolled %s -> %s; suppressing diff", previous.week, current.week)
        return []

    events: list[ScoringEvent] = []
    for player_key, now in current.players.items():
        before = previous.players.get(player_key)
        if before is None:
            continue  # newly rostered — no baseline to diff against
        if now.points is None or before.points is None:
            continue

        delta = round(now.points - before.points, 2)
        if abs(delta) < min_delta:
            continue

        events.append(
            ScoringEvent(
                event_id=_event_id(current.week, player_key, before.points, now.points),
                week=current.week,
                timestamp=current.taken_at,
                player_key=player_key,
                player_name=now.name,
                team_key=now.team_key,
                matchup_id=now.matchup_id,
                nfl_team=now.nfl_team,
                position=now.position,
                delta=delta,
                old_points=round(before.points, 2),
                new_points=round(now.points, 2),
                starter=now.starter,
                correction=_is_correction(delta, before.stats, now.stats),
                stat_delta=_stat_delta(before.stats, now.stats),
            )
        )

    # Biggest scores first within a poll; corrections sort to the end.
    events.sort(key=lambda e: (e.correction, -abs(e.delta)))
    return events


# ---------------------------------------------------------------------------
# Enrichment
# ---------------------------------------------------------------------------


def enrich_events(
    events: list[ScoringEvent],
    espn_plays: list[dict[str, Any]],
    matcher: Matcher,
    *,
    threshold: float = 0.75,
) -> list[ScoringEvent]:
    """Attach matching ESPN scoring plays to each event.

    ``espn_plays`` should be the plays that appeared **since the previous poll**
    — the caller tracks which play ids it has already seen. Passing the whole
    game's plays would let a player's second-quarter touchdown attach itself to
    a fourth-quarter point change.

    A play is not consumed exclusively: one passing touchdown legitimately
    credits the receiver, the passer and the kicker, so it may attach to three
    different events. Conversely a player who scored twice in one interval
    collects both plays on the single combined-delta event, which is how that
    otherwise-undecomposable case still reads correctly in the UI.

    Corrections are never enriched — a negative delta is a stat revision, not
    something that happened on the field.
    """
    if not events or not espn_plays:
        return events

    enriched: list[ScoringEvent] = []
    for event in events:
        if event.correction:
            enriched.append(event)
            continue

        matches: list[MatchedPlay] = []
        for play in espn_plays:
            credits = play.get("credits") or []
            if not credits:
                continue
            result = matcher(
                credits,
                player_name=event.player_name,
                player_team=event.nfl_team,
                play_team=play.get("team"),
                position=event.position,
                threshold=threshold,
            )
            if getattr(result, "matched", False):
                credit = result.credit
                matches.append(
                    MatchedPlay(
                        text=play.get("text") or "",
                        role=getattr(credit, "role", "") or "",
                        confidence=result.confidence,
                        period=play.get("period"),
                        clock=play.get("clock"),
                        play_id=play.get("id"),
                    )
                )

        enriched.append(replace(event, plays=tuple(matches)) if matches else event)

    return enriched


# ---------------------------------------------------------------------------
# Presentation
# ---------------------------------------------------------------------------


def abbreviate_name(name: str) -> str:
    """``Puka Nacua`` → ``P. Nacua``. Single-word names pass through.

    A first name that is already initials — ``D.J. Reed``, ``C.J. Stroud`` —
    is kept whole: ``D. Reed`` is a different person's name, not a shorter
    version of this one.
    """
    parts = (name or "").split()
    if len(parts) < 2:
        return name or ""
    first = parts[0] if "." in parts[0] else f"{parts[0][0]}."
    return f"{first} {' '.join(parts[1:])}"


def _stat_delta(before: dict[str, float], after: dict[str, float]) -> str:
    """What changed between two stat lines, or ``""`` if the source has none.

    Imported lazily so this module keeps no import-time dependency on a
    particular source — the HTML tier has no stat lines at all.
    """
    if not after and not before:
        return ""
    from .yahoo_redzone import describe_delta

    return describe_delta(before or {}, after or {})


def match_relay_play(
    event: ScoringEvent,
    plays: list[Any],
    names: dict[str, str],
    pin: str | None = None,
) -> MatchedPlay | None:
    """The play in ``plays`` that this event's points most likely came from.

    Matching is by Yahoo's own player id, not by name: the play feed writes
    people as ``[42654]`` and the event's ``player_key`` ends in that same id,
    so this is exact where the ESPN path had to fuzzy-match strings.

    For a NEW event the newest qualifying play wins. Within one poll interval a
    player may appear in several plays and the points can only be attributed to
    one of them; the most recent is both the best guess and the one a reader
    watching live is asking about.

    ``pin`` is for re-reading an event that already matched. Yahoo revises a
    play's WORDING in place — terse first, fuller a moment later — it does not
    replace the play with a different one, so a revision must re-render that
    same play rather than run the newest-wins pick again. Without the pin, an
    event whose text was already correct is overwritten the moment its player
    makes another play: a 10-yard rush ends up captioned with the touchdown
    that came two minutes later, and two different events render one identical
    sentence. Observed live 2026-09-13 on roughly a quarter of enriched events.

    An unresolvable pin returns ``None`` so the caller keeps the text it has.
    That is deliberate: a play ageing out of the feed is not a reason to
    relabel the event with something newer and unrelated.

    The unpinned pick honours ``event.play_floor``: plays already in the feed
    when the event was raised cannot be the play that raised it. Returning
    ``None`` here is the normal case for a fresh event — its text has not
    landed yet — and the caller tries again on the next poll.

    ``yahoo_redzone`` is imported lazily to keep this module free of any
    particular source — the HTML tier has no play feed at all.
    """
    from .yahoo_redzone import humanize_play

    player_id = event.player_key.rsplit(".p.", 1)[-1]
    if not player_id:
        return None
    floor = event.play_floor
    for play in reversed(plays):  # newest first
        if pin is not None:
            if f"{play.game_key}.{play.sequence}" != pin:
                continue
        elif floor is not None and play.sequence <= floor:
            return None  # nothing further back can be it either
        elif player_id not in play.player_ids:
            continue
        text = humanize_play(play.text, names)
        if not text:
            return None
        try:
            period = int(play.period)
        except (TypeError, ValueError):
            period = None
        return MatchedPlay(
            text=text,
            role="",
            confidence=1.0,
            period=period,
            clock=play.clock,
            play_id=f"{play.game_key}.{play.sequence}",
        )
    return None


def play_above_floor(event: ScoringEvent, plays: list[Any]) -> bool:
    """Whether the feed holds a play, above the event's floor, that names its player.

    The question :meth:`PlayFeed.fold` needs answered before it acts: an
    event still unmatched after the text lag either has a play in the feed it
    cannot render yet (a name the dictionary lacks), or has no play of its
    own at all. Only the second is a piece of the play before it. An unknown
    floor answers yes — nothing can be ruled out from nothing.
    """
    if event.play_floor is None:
        return True
    player_id = event.player_key.rsplit(".p.", 1)[-1]
    return any(
        play.sequence > event.play_floor and player_id in play.player_ids for play in plays
    )


def describe(event: ScoringEvent) -> str:
    """One-line banner text for an event.

    Three tiers, best first:

    1. A real play description, when ESPN enrichment matched one — "Nacua
       24 Yd pass from Stafford".
    2. The stat delta from Yahoo's own live feed — "D. Maye 1 Comp, 13 Pass
       Yds". This is the phrasing Yahoo prints under each matchup itself.
    3. A bare point delta, which is all the HTML tier can support.

    The point value is rendered separately by the card, so it is not repeated
    here except in the last-resort form.
    """
    who = abbreviate_name(event.player_name)
    if event.correction:
        return f"{who} {event.delta:+.2f} (stat correction)"
    play = event.best_play
    if play is not None and play.text:
        return play.text
    if event.stat_delta:
        return f"{who} {event.stat_delta}"
    return f"{who} {event.delta:+.2f}"


def short_parts(event: ScoringEvent) -> tuple[str, str]:
    """``("T. Etienne Jr.", "1 rec, 1 yd")`` — who, and what they did.

    The two halves of :func:`short_describe`, kept apart so the card can put
    the line break BETWEEN them when a row is too narrow for both: "A. St.
    Brown" over "1 rec, 23 yd, 1 TD" reads, "A. St. Brown 1 rec," over
    "23 yd, 1 TD" does not.
    """
    who = abbreviate_name(event.player_name)
    if event.correction:
        return who, f"{event.delta:+.2f} (stat correction)"
    if event.stat_delta:
        from .yahoo_redzone import shorten_stat_delta

        return who, shorten_stat_delta(event.stat_delta)
    return who, f"{event.delta:+.2f}"


def short_describe(event: ScoringEvent) -> str:
    """``T. Etienne Jr. 1 rec, 1 yd`` — the fantasy-side view of an event.

    Deliberately SKIPS the play description that :func:`describe` prefers. On a
    matchup row the question is what this manager's player just did, and the
    full sentence answers a different one — "Tyler Shough passed to Travis
    Etienne Jr. to the right for 1 yard gain" leads with a quarterback who may
    be on nobody's roster, and costs a line wrap to say what "1 rec, 1 yd" says
    in a corner.

    The long form is not lost: it is what the expanded play list shows, and
    what an NFL-games view would want, where the play itself is the subject.
    """
    return " ".join(short_parts(event))


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------


class PlayFeed:
    """Bounded, de-duplicated, per-week scoring history.

    Persisted through ``homeassistant.helpers.storage.Store`` so a restart in
    the middle of a Sunday does not lose the afternoon.
    """

    def __init__(self, maxlen: int = DEFAULT_HISTORY) -> None:
        self._events: deque[ScoringEvent] = deque(maxlen=maxlen)
        self._seen: set[str] = set()
        self._week: int | None = None
        self._version = 0

    def __len__(self) -> int:
        return len(self._events)

    @property
    def week(self) -> int | None:
        return self._week

    @property
    def version(self) -> int:
        """Counts every change to the history, so a caller knows when to persist it."""
        return self._version

    def add(self, events: list[ScoringEvent]) -> list[ScoringEvent]:
        """Append new events, dropping ones already recorded. Returns what stuck.

        A new week clears the feed: history is per-week, and carrying last
        week's plays into this week's banner would be worse than losing them.

        An event that is a later piece of a play already on the feed — the
        same player, matched to the same play — is folded into that row
        rather than appended, and the row as it now reads is what is
        returned in its place: the running total under the id the caller
        already knows, not a second event for the same play.
        """
        added: list[ScoringEvent] = []
        for event in events:
            if self._week is not None and event.week != self._week:
                self.clear()
            self._week = event.week

            if event.event_id in self._seen:
                continue
            host = self._row_showing(event)
            if host is not None:
                added.append(self._absorb(host, event))
                continue
            if len(self._events) == self._events.maxlen and self._events:
                # deque eviction must also release the id, or a long game
                # eventually refuses to record anything.
                self._seen.discard(self._events[0].event_id)
            self._events.append(event)
            self._seen.add(event.event_id)
            self._version += 1
            added.append(event)
        return added

    def revise(self, event_id: str, plays: tuple[MatchedPlay, ...]) -> ScoringEvent | None:
        """Replace a stored event's attached plays, keeping its place in history.

        Yahoo publishes a terse description first — "Dak Prescott complete for
        29 yards" — and fills in the detail a moment later. Re-matching a
        recent event against the current feed is how the better wording reaches
        a card that is already showing the worse one.

        A revision that lands the event on a play another of the player's
        rows already shows makes the two one row, whichever was matched
        first: the older keeps its place and its id, the younger is folded in.
        """
        for index, event in enumerate(self._events):
            if event.event_id == event_id:
                updated = replace(event, plays=tuple(plays))
                self._events[index] = updated
                self._version += 1
                other = self._row_showing(updated)
                if other is None:
                    return updated
                if self._events.index(other) < index:
                    host, piece = other, updated
                else:
                    host, piece = updated, other
                merged = self._absorb(host, piece)
                del self._events[self._events.index(piece)]
                return merged
        return None

    def fold(self, event_id: str) -> ScoringEvent | None:
        """Fold an event that never got a play of its own into its player's last play.

        The other way a piece arrives: the play's text had already landed
        when the piece did, so the floor (rightly) refused to match the piece
        to it, and the piece sat as "1 Rush Yds +0.10" — a run that never
        happened — until, minutes later, newest-wins pinned it to whatever
        the player did next. The caller decides WHEN this is safe (the text
        lag has passed and the feed holds nothing above the floor that names
        the player, see :func:`play_above_floor`); this decides WHETHER: the
        player's nearest older row must be showing a play, and be within
        :data:`SPLIT_TICK_SECONDS` of the piece. Otherwise nothing is known
        and the piece stays. Returns the row as it now reads, or ``None``.
        """
        index = next((i for i, e in enumerate(self._events) if e.event_id == event_id), None)
        if index is None:
            return None
        piece = self._events[index]
        if piece.correction or piece.plays:
            return None
        for host in reversed(list(self._events)[:index]):
            if host.player_key != piece.player_key or host.correction:
                continue
            if not host.plays or piece.timestamp - host.timestamp > SPLIT_TICK_SECONDS:
                return None
            merged = self._absorb(host, piece)
            del self._events[index]
            return merged
        return None

    def _row_showing(self, event: ScoringEvent) -> ScoringEvent | None:
        """Another of the player's rows already matched to this event's play."""
        if event.correction or not event.plays or not event.plays[-1].play_id:
            return None
        play_id = event.plays[-1].play_id
        for other in reversed(self._events):
            if (
                other.event_id != event.event_id
                and other.player_key == event.player_key
                and not other.correction
                and other.plays
                and other.plays[-1].play_id == play_id
            ):
                return other
        return None

    def _absorb(self, host: ScoringEvent, piece: ScoringEvent) -> ScoringEvent:
        """Fold ``piece`` into ``host`` in place; the piece's id counts as seen."""
        from .yahoo_redzone import merge_stat_deltas

        merged = replace(
            host,
            delta=round(host.delta + piece.delta, 2),
            new_points=piece.new_points if piece.timestamp >= host.timestamp else host.new_points,
            stat_delta=merge_stat_deltas(host.stat_delta, piece.stat_delta),
            absorbed=(*host.absorbed, piece.event_id, *piece.absorbed),
        )
        self._events[self._events.index(host)] = merged
        self._seen.add(piece.event_id)
        self._version += 1
        return merged

    def start_week(self, week: int | None) -> bool:
        """Make ``week`` the feed's week. Returns whether an older week was cleared.

        The coordinator calls this once per poll. It has to move ``week`` as
        well as clearing, because :meth:`add` is the only other thing that
        does, and ``add`` never runs until an event exists. Clearing without
        moving the week meant the *next* poll saw last week's number again and
        cleared again — every poll of the first game of week 2 reset the
        baseline, so no diff ever produced an event (2026-09-17, Det at Buf).
        """
        if week is None or week == self._week:
            return False
        rolled = self._week is not None
        if rolled:
            self.clear()
        self._week = week
        return rolled

    def clear(self) -> None:
        self._events.clear()
        self._seen.clear()
        self._version += 1

    def recent(
        self,
        limit: int = 10,
        *,
        matchup_id: str | None = None,
        team_key: str | None = None,
        include_corrections: bool = False,
        starters_only: bool = False,
        nfl_teams: frozenset[str] | None = None,
    ) -> list[ScoringEvent]:
        """Most recent events first, newest at index 0.

        ``starters_only`` drops bench and IR players. Their points are real but
        they do not count toward the fantasy score, so showing them beside a
        matchup total reads as a scoring change that never happened.

        ``nfl_teams`` restricts the result to players on those clubs — the
        callers pass the clubs whose games are in progress, so a play cannot
        outlive the game it happened in. ``None`` means no restriction, which
        is what an unavailable games feed must fall back to.
        """
        out: list[ScoringEvent] = []
        for event in reversed(self._events):
            if not include_corrections and event.correction:
                continue
            if starters_only and not event.starter:
                continue
            if nfl_teams is not None and (event.nfl_team or "") not in nfl_teams:
                continue
            if matchup_id is not None and event.matchup_id != matchup_id:
                continue
            if team_key is not None and event.team_key != team_key:
                continue
            out.append(event)
            if len(out) >= limit:
                break
        return out

    def last_play(self, **kwargs: Any) -> ScoringEvent | None:
        """The single event a banner should show, or ``None``."""
        found = self.recent(1, **kwargs)
        return found[0] if found else None

    # -- persistence --------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "week": self._week,
            "events": [_event_to_dict(e) for e in self._events],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None, maxlen: int = DEFAULT_HISTORY) -> PlayFeed:
        feed = cls(maxlen=maxlen)
        if not data:
            return feed
        feed._week = data.get("week")
        for raw in data.get("events") or []:
            try:
                event = _event_from_dict(raw)
            except (TypeError, ValueError, KeyError) as err:
                _LOGGER.debug("Discarding unreadable stored event (%s): %s", err, raw)
                continue
            feed._events.append(event)
            feed._seen.add(event.event_id)
            feed._seen.update(event.absorbed)
        return feed


def _event_to_dict(event: ScoringEvent) -> dict[str, Any]:
    data = event.__dict__.copy()
    data["plays"] = [p.__dict__.copy() for p in event.plays]
    return data


def _event_from_dict(raw: dict[str, Any]) -> ScoringEvent:
    from .yahoo_redzone import strip_tackler

    data = dict(raw)
    # Stored text predates whatever the humaniser does today; a play whose
    # text was nothing but the tackler is dropped, and the event falls back
    # to its stat line the way a fresh one would.
    plays = []
    for p in data.get("plays") or []:
        play = MatchedPlay(**p)
        text = strip_tackler(play.text)
        if text:
            plays.append(play if text == play.text else replace(play, text=text))
    data["plays"] = tuple(plays)
    data["absorbed"] = tuple(data.get("absorbed") or ())
    return ScoringEvent(**data)


def dumps(feed: PlayFeed) -> str:
    """JSON for a Store payload."""
    return json.dumps(feed.to_dict())


def loads(payload: str | None, maxlen: int = DEFAULT_HISTORY) -> PlayFeed:
    """Restore from a Store payload; unreadable input yields an empty feed."""
    if not payload:
        return PlayFeed(maxlen=maxlen)
    try:
        return PlayFeed.from_dict(json.loads(payload), maxlen=maxlen)
    except (ValueError, TypeError) as err:
        _LOGGER.debug("Discarding unreadable stored feed: %s", err)
        return PlayFeed(maxlen=maxlen)
