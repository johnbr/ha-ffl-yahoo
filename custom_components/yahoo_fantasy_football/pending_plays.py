"""A play's result, read off the games feed before Yahoo's text arrives.

Yahoo moves a game's down, distance and ball spot in the games feed 2-30 s
before the play's sentence reaches that game's play feed (measured live
2026-09-17). Until it does, the games card shows the NEW down and distance
under the OLD play: "3rd & 4" beside the 2nd-down run that set it up.

The two snapshots either side of the snap already say most of what happened.
The spot moved seven yards and the down reset: "Gain of 7, 1st down". The
other club has the ball after a third down: "Turnover". The score went up by
six: a touchdown. That is shown, marked provisional, until Yahoo's own
sentence lands and replaces it.

What it cannot say, and does not pretend to: who carried the ball, whether it
was a run or a pass, or that a gain was a penalty — a false start reads
"Loss of 5". It is a stopgap of a few seconds, not a play description.

Pure, like :mod:`league_state`: the coordinator feeds it one observation per
game per poll and reads the answer back.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, NamedTuple

from .league_state import down_and_distance
from .yahoo_redzone import team_abbr

# How long after a games-feed change a further change is still the SAME play.
#
# The games feed updates its fields piecemeal, in either order — on 2026-10-01
# the down and distance moved first and the spot followed ~12 s later, on every
# play watched; ``down_and_distance`` records the opposite — so one snap can
# arrive over two polls. Two real snaps are rarely this close in the games feed,
# and the cost when they are is one provisional line netting the two together.
REFINE_SECONDS = 18.0

# Play-feed rows that never move the ball: ``24`` a timeout or the two-minute
# warning, ``25`` the end of a period (read off the captured feeds). They land
# between snaps, so they are never a games-feed change's text.
NO_BALL_TYPES = frozenset({"24", "25"})

# How long the play feed is re-checked for a pending play's text. Past the
# measured lag with a margin, the same allowance the scoring-play matcher gives
# (``plays.PLAY_TEXT_LAG_SECONDS``). The provisional line itself does not
# expire: it is still the truest thing to say about the last snap.
CHASE_FOR_SECONDS = 60.0


@dataclass(frozen=True)
class Snap:
    """One game's state as the games feed reports it at one poll."""

    team: str
    """Club with the ball. ``"0"`` when nobody has it."""
    down: int
    distance: int
    to_goal: int
    """Yards to the opponent's goal line; 0 between plays and after a score."""
    away_score: int | None
    home_score: int | None

    @property
    def situation(self) -> tuple[str, int, int, int]:
        """The fields a play-feed row records as the situation it ran FROM."""
        return (self.team, self.down, self.distance, self.to_goal)

    def has_spot(self, away: str, home: str) -> bool:
        """A real ball on the field, held by one of the two clubs playing."""
        return 0 < self.to_goal < 100 and self.team in {away, home}


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _score(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def snap_of(game: Any) -> Snap:
    """A :class:`Snap` from a games-feed row (``yahoo_redzone.GameState``)."""
    return Snap(
        team=str(getattr(game, "team_with_ball", "") or ""),
        down=_int(getattr(game, "down", 0)),
        distance=_int(getattr(game, "distance", 0)),
        to_goal=_int(getattr(game, "yards_to_goal", 0)),
        away_score=_score(getattr(game, "away_score", None)),
        home_score=_score(getattr(game, "home_score", None)),
    )


class Landed(NamedTuple):
    """The play whose text the card is showing: which, and what it ran from."""

    sequence: int
    situation: tuple[str, int, int, int]
    """As :func:`situation_of` reads it."""
    period: str = ""
    clock: str = ""
    """Game clock at the snap, ``"14:11"``."""
    play_type: str = ""


def game_time(period: Any, clock: Any) -> tuple[int, int] | None:
    """``(period, seconds elapsed in it)`` — orderable, unlike a clock that runs down."""
    try:
        minutes, seconds = str(clock).split(":")
        return int(period), 15 * 60 - (int(minutes) * 60 + int(seconds))
    except (TypeError, ValueError):
        return None


def situation_of(play: Any) -> tuple[str, int, int, int]:
    """The situation a play-feed row (``yahoo_redzone.RelayPlay``) ran from."""
    return (
        str(getattr(play, "team_with_ball", "") or ""),
        _int(getattr(play, "down", 0)),
        _int(getattr(play, "distance", 0)),
        _int(getattr(play, "yards_to_goal", 0)),
    )


def _scored(before: Snap, after: Snap, away: str, home: str) -> tuple[str, int] | None:
    """``(club, points)`` if exactly one side's score went up."""
    if None in (before.away_score, before.home_score, after.away_score, after.home_score):
        return None
    away_gain = after.away_score - before.away_score
    home_gain = after.home_score - before.home_score
    if away_gain > 0 and home_gain <= 0:
        return away, away_gain
    if home_gain > 0 and away_gain <= 0:
        return home, home_gain
    return None


def describe_change(before: Snap, after: Snap, away: str, home: str) -> str:
    """What one games-feed change says happened, or ``""`` if nothing useful.

    ``before`` is the situation the play was run from, ``after`` where it
    left the game.
    """
    scored = _scored(before, after, away, home)
    if scored is not None:
        club, points = scored
        abbr = team_abbr(club)
        if points >= 6:  # a touchdown, with the try sometimes already added
            return f"{abbr} touchdown"
        if points == 3:
            return f"{abbr} field goal"
        if points == 2:
            # The defence scores a safety; the offence scores a conversion.
            return f"{abbr} safety" if club != before.team else f"{abbr} two-point conversion"
        if points == 1:
            return f"{abbr} extra point"
        return f"{abbr} +{points}"

    # No spot either side is a kickoff, a try, a break in play: nothing a
    # yardage or a down can be read from.
    if not (before.has_spot(away, home) and after.has_spot(away, home)):
        return ""

    if after.team != before.team:
        abbr = team_abbr(after.team)
        if before.down == 1 and before.distance == 10:
            # A kickoff looks just like this from in here — the kicking club
            # "has" the ball 65 yards out — so nothing more is claimed.
            return f"Change of possession, {abbr} ball"
        if before.down == 4:
            # How far the ball went for the club that gave it up: a punt
            # flips the field, a stop on downs or a missed kick barely moves it.
            moved = before.to_goal + after.to_goal - 100
            what = "Punt" if moved >= 20 else "Change of possession"
            return f"{what}, {abbr} ball"
        if before.down:
            return f"Turnover, {abbr} ball"
        return f"Change of possession, {abbr} ball"

    # Yardage from the distance where the down says how to read it, and from
    # the spot only where it has to: the spot lags the down by a poll, so a
    # five-yard run first reads as 2nd & 5 with the ball where it was snapped.
    spot_gain = before.to_goal - after.to_goal
    if after.down == 1 and (before.down != 1 or after.distance == before.distance):
        if spot_gain >= before.distance:
            return f"{_yards(spot_gain)}, 1st down"
        if before.down != 1:
            return "1st down"  # the spot has not caught up yet
        return ""  # 1st & 10 again a yard over: a re-spot, not a play
    if after.down == before.down + 1:
        return _yards(before.distance - after.distance)
    if after.down == before.down and after.distance != before.distance:
        return _yards(before.distance - after.distance)  # a penalty, replaying the down
    return ""


def _yards(gained: int) -> str:
    if gained > 0:
        return f"Gain of {gained}"
    if gained < 0:
        return f"Loss of {-gained}"
    return "No gain"


def _ran_from(play: Landed, origin: Snap, origin_at: tuple[int, int] | None) -> bool:
    """Whether ``play`` is the one snapped from ``origin``.

    By the situation the row records, or failing that by the clock: a play
    snapped no earlier than the moment the games feed first showed ``origin``
    was run from it. The clock is needed because the row's situation is not
    always the games feed's — the first play after a turnover was recorded
    live (2026-10-01) as run from the OTHER club's last spot.
    """
    if play.situation == origin.situation:
        return True
    snapped = game_time(play.period, play.clock)
    return snapped is not None and origin_at is not None and snapped >= origin_at


@dataclass
class _Game:
    snap: Snap
    """The games feed as last seen."""
    changed_at: float
    landed: int | None = None
    """Sequence of the newest play whose text is on the card."""
    seen_at: tuple[int, int] | None = None
    """Game time when ``snap`` first appeared — see :func:`game_time`."""
    origin: Snap | None = None
    """Where the newest snap was run from — ``None`` once its text is in."""
    origin_at: tuple[int, int] | None = None
    earlier: Snap | None = None
    """Where the snap before that was run from, while its text is out too."""
    unclaimed: bool = False
    """A play landed while none was awaited: the next change is its result."""
    text: str = ""
    since: float = 0.0


@dataclass
class PendingPlays:
    """Every live game's provisional play, kept across polls."""

    _games: dict[str, _Game] = field(default_factory=dict)

    def observe(
        self,
        plays_id: str,
        game: Any,
        newest: Landed | None,
        now: float,
    ) -> None:
        """Take one poll's view of one game.

        ``newest`` is the play whose text the card is now showing, or
        ``None`` if there is none.
        Called again between polls with the same ``game`` when only the play
        feed has been re-read, which is just the text-landing half of this.
        """
        if getattr(game, "state", "") != "in":
            self._games.pop(plays_id, None)
            return
        away, home = str(game.away), str(game.home)
        snap = snap_of(game)
        seen_at = game_time(getattr(game, "period", ""), getattr(game, "clock", ""))
        state = self._games.get(plays_id)
        if state is None:
            # A first look has nothing to compare against; it is the baseline.
            landed = newest.sequence if newest else None
            self._games[plays_id] = _Game(snap, float("-inf"), landed=landed, seen_at=seen_at)
            return

        fresh = newest is not None and (state.landed is None or newest.sequence > state.landed)
        moved = fresh and newest.play_type not in NO_BALL_TYPES
        if fresh and state.origin is None:
            state.unclaimed = state.unclaimed or moved
        elif fresh:
            if state.earlier is not None and newest.situation == state.earlier.situation:
                # The play BEFORE the awaited one landed; this one still waits.
                state.earlier = None
            else:
                # Anything else that lands is taken as the awaited play. A
                # wrong guess here shows Yahoo's real text a play early; the
                # opposite guess would hide it behind a provisional line.
                state.origin = state.earlier = None
                state.text = ""
        if newest is not None:
            state.landed = newest.sequence

        if snap == state.snap:
            return
        if state.origin is not None and snap == state.origin:
            # Back where the awaited play started: it was undone — a flag, a
            # review. Its text, when it comes, says what really happened.
            state.text = ""
            state.snap, state.changed_at, state.seen_at = snap, now, seen_at
            return
        if now - state.changed_at >= REFINE_SECONDS:
            # A new snap. A refinement — the rest of the last change — keeps
            # the origin that change set, or its absence: a play whose text is
            # already in, or that had nothing to say, gains nothing from it.
            state.earlier = state.origin
            state.origin, state.origin_at = state.snap, state.seen_at
            state.since = now
            # Its text is already in when a play lands in the same poll —
            # every time this was watched live (2026-10-01), including a
            # kickoff whose return showed up as a second change 25 s after the
            # catch — or landed earlier with nothing awaiting it (a kickoff's
            # text, 3 s ahead of the games feed), or when the play on the card
            # ran from here.
            if (
                moved
                or state.unclaimed
                or (newest is not None and _ran_from(newest, state.origin, state.origin_at))
            ):
                state.origin = None
        # Spent by any change, a refinement included: a play that landed
        # between a change and the rest of it belonged to that change.
        state.unclaimed = False
        state.text = describe_change(state.origin, snap, away, home) if state.origin else ""
        state.snap, state.changed_at, state.seen_at = snap, now, seen_at

    def forget_others(self, live: set[str]) -> None:
        """Drop every game not in ``live`` — finished, or gone from the feed."""
        for plays_id in set(self._games) - live:
            del self._games[plays_id]

    def text(self, plays_id: str) -> str:
        """The provisional line for a game, or ``""`` when its text is in."""
        state = self._games.get(plays_id)
        return state.text if state is not None else ""

    def texts(self) -> dict[str, str]:
        """``{plays_id: provisional line}`` for every game awaiting text."""
        return {pid: s.text for pid, s in self._games.items() if s.text}

    def row(self, plays_id: str, game: Any) -> dict[str, Any] | None:
        """The awaited snap as a row of the games card's expanded play list.

        Shaped like ``coordinator.async_game_plays`` rows, with the down and
        distance it was run from like every other row there, and flagged so
        the card can set it apart. No clock: the games feed only says what
        the clock read at each poll, not at the snap.
        """
        state = self._games.get(plays_id)
        if state is None or not state.text or state.origin is None:
            return None
        origin = state.origin
        situation = ""
        if origin.has_spot(str(game.away), str(game.home)):
            situation = down_and_distance(origin.down, origin.distance, origin.to_goal)
        return {
            "play_id": "",
            "text": state.text,
            "short_text": state.text,
            "situation": situation,
            "period": str(getattr(game, "period", "") or ""),
            "clock": "",
            "provisional": True,
        }

    def awaiting(self, now: float) -> list[str]:
        """Games whose play feed is worth re-reading before the next poll."""
        return [pid for pid, s in self._games.items() if s.text and now - s.since < CHASE_FOR_SECONDS]
