"""The games card's scoring banner — TOUCHDOWN, FIELD GOAL, SAFETY.

Decided here rather than in the card so that every dashboard shows the same
thing. A card that spotted the score jump itself could only do so if it was
open when the jump arrived; one opened ten seconds later saw a score that was
already on the board and showed nothing. Kept here, a scoring play is a fact
with a time on it, and any card loaded while it is up shows the same banner.

It stays up until the play AFTER the scoring play lands in the play feed — the
extra point after a touchdown, the kickoff after a field goal — so it sits
over the scoring play's own sentence for as long as that is the last play on
the card, however long Yahoo takes to write it.

Pure, like :mod:`pending_plays`: the coordinator feeds it the games feed once
per poll and the play feeds as they are read, and the sensor reads the live
banners back.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from .yahoo_redzone import team_abbr

# The longest a banner stays up, for when the play after it never shows: the
# play feed is down, or the score ended the game and there is no next play.
BANNER_SECONDS = 180.0

# The shortest. A score that reached the games feed late can arrive with its
# play AND the one after already written; the banner still gets this long.
MIN_BANNER_SECONDS = 15.0

# Play-feed rows that are not the next play: ``24`` is a timeout or the
# two-minute warning, which can fall between a touchdown and its try. The end
# of a period (``25``) does count — after a field goal at the half, nothing
# else is coming for a quarter of an hour.
NOT_A_PLAY = frozenset({"24"})


def _score(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class _Seen:
    away_score: int | None
    home_score: int | None
    team: str
    """Club with the ball. ``"0"`` or empty when nobody has it."""


def _seen(game: Any) -> _Seen:
    return _Seen(
        _score(getattr(game, "away_score", None)),
        _score(getattr(game, "home_score", None)),
        str(getattr(game, "team_with_ball", "") or ""),
    )


def scoring_kind(before: _Seen, after: _Seen, away: str, home: str) -> tuple[str, str] | None:
    """``(kind, club)`` if one games-feed change was a scoring play.

    The feed has no play type, so the size of the jump is the evidence: six
    to eight is a touchdown (the try sometimes lands in the same poll), three
    a field goal, and two a safety when it went to the club WITHOUT the ball.
    Two to the offence is a two-point try and one an extra point — the tail of
    a touchdown already bannered, not a scoring play of its own. More than
    eight is a gap in the polls rather than one play, and is not guessed at.
    """
    best: tuple[int, str, str] | None = None
    for club, was, now in (
        (away, before.away_score, after.away_score),
        (home, before.home_score, after.home_score),
    ):
        if was is None or now is None or now <= was:
            continue
        gain = now - was
        kind = ""
        if 6 <= gain <= 8:
            kind = "TOUCHDOWN"
        elif gain == 3:
            kind = "FIELD GOAL"
        elif gain == 2 and before.team in {away, home} and club != before.team:
            kind = "SAFETY"
        if kind and (best is None or gain > best[0]):
            best = (gain, kind, club)
    return (best[1], best[2]) if best else None


@dataclass
class ScoreBanners:
    """Every game's last-seen score, and the banners still up."""

    _seen: dict[str, _Seen] = field(default_factory=dict)
    _banners: dict[str, dict[str, Any]] = field(default_factory=dict)
    _floors: dict[str, int | None] = field(default_factory=dict)
    """Per banner, the newest play-feed row from BEFORE the score."""

    def observe(self, games: list[Any], now: float, floors: dict[str, int] | None = None) -> None:
        """Take one poll's view of the slate.

        A game seen for the first time is the baseline, never a banner — a
        restart mid-game must not announce every score already on the board.
        Only a game in progress can raise one.

        ``floors`` maps a game id to the newest row its play feed had as of
        the last poll: the scoring play is the first row above it, and the
        banner comes down on the second.
        """
        current: set[str] = set()
        for game in games:
            game_id = str(getattr(game, "game_id", "") or "")
            if not game_id:
                continue
            current.add(game_id)
            seen = _seen(game)
            before = self._seen.get(game_id)
            self._seen[game_id] = seen
            if before is None or getattr(game, "state", "") != "in":
                continue
            scored = scoring_kind(before, seen, str(game.away), str(game.home))
            if scored is not None:
                kind, club = scored
                self._banners[game_id] = {
                    "kind": kind,
                    "abbr": team_abbr(club),
                    "at": now,
                    "until": now + BANNER_SECONDS,
                }
                self._floors[game_id] = (floors or {}).get(game_id)
        for game_id in set(self._seen) - current:
            del self._seen[game_id]
        for game_id, banner in list(self._banners.items()):
            if game_id not in current or banner["until"] <= now:
                self._drop(game_id)

    def plays(self, game_id: str, rows: Iterable[tuple[int, str]], now: float) -> bool:
        """Read a game's play feed — ``(sequence, play type)`` per row.

        Takes the banner down once a play has landed after the scoring play.
        A banner raised before the feed was ever read has no floor; it takes
        the newest row at its first read as the scoring play. Returns whether
        a banner came down.
        """
        banner = self._banners.get(game_id)
        if banner is None:
            return False
        landed = sorted(seq for seq, kind in rows if kind not in NOT_A_PLAY)
        floor = self._floors.get(game_id)
        if floor is None:
            if landed:
                self._floors[game_id] = landed[-1] - 1
            return False
        if sum(seq > floor for seq in landed) < 2 or now - banner["at"] < MIN_BANNER_SECONDS:
            return False
        self._drop(game_id)
        return True

    def _drop(self, game_id: str) -> None:
        del self._banners[game_id]
        self._floors.pop(game_id, None)

    def active(self, now: float) -> dict[str, dict[str, Any]]:
        """``{game_id: banner}`` for every banner still up at ``now``."""
        return {gid: dict(b) for gid, b in self._banners.items() if b["until"] > now}
