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
                correction=delta < 0,
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
    """``Puka Nacua`` → ``P. Nacua``. Single-word names pass through."""
    parts = (name or "").split()
    if len(parts) < 2:
        return name or ""
    return f"{parts[0][0]}. {' '.join(parts[1:])}"


def describe(event: ScoringEvent) -> str:
    """One-line banner text for an event.

    Prefers the real play description when enrichment found one, since
    "Nacua 24 Yd pass from Stafford" says more than "+6.4". The point delta is
    rendered separately by the card, so it is not repeated here.
    """
    if event.correction:
        return f"{abbreviate_name(event.player_name)} {event.delta:+.2f} (stat correction)"
    play = event.best_play
    if play is not None and play.text:
        return play.text
    return f"{abbreviate_name(event.player_name)} {event.delta:+.2f}"


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

    def __len__(self) -> int:
        return len(self._events)

    @property
    def week(self) -> int | None:
        return self._week

    def add(self, events: list[ScoringEvent]) -> list[ScoringEvent]:
        """Append new events, dropping ones already recorded. Returns what stuck.

        A new week clears the feed: history is per-week, and carrying last
        week's plays into this week's banner would be worse than losing them.
        """
        added: list[ScoringEvent] = []
        for event in events:
            if self._week is not None and event.week != self._week:
                self.clear()
            self._week = event.week

            if event.event_id in self._seen:
                continue
            if len(self._events) == self._events.maxlen and self._events:
                # deque eviction must also release the id, or a long game
                # eventually refuses to record anything.
                self._seen.discard(self._events[0].event_id)
            self._events.append(event)
            self._seen.add(event.event_id)
            added.append(event)
        return added

    def clear(self) -> None:
        self._events.clear()
        self._seen.clear()

    def recent(
        self,
        limit: int = 10,
        *,
        matchup_id: str | None = None,
        team_key: str | None = None,
        include_corrections: bool = False,
    ) -> list[ScoringEvent]:
        """Most recent events first, newest at index 0."""
        out: list[ScoringEvent] = []
        for event in reversed(self._events):
            if not include_corrections and event.correction:
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
        return feed


def _event_to_dict(event: ScoringEvent) -> dict[str, Any]:
    data = event.__dict__.copy()
    data["plays"] = [p.__dict__.copy() for p in event.plays]
    return data


def _event_from_dict(raw: dict[str, Any]) -> ScoringEvent:
    data = dict(raw)
    data["plays"] = tuple(MatchedPlay(**p) for p in data.get("plays") or [])
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
