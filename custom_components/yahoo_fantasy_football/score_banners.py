"""The games card's scoring banner — TOUCHDOWN, FIELD GOAL, SAFETY.

Decided here rather than in the card so that every dashboard shows the same
thing. A card that spotted the score jump itself could only do so if it was
open when the jump arrived; one opened ten seconds later saw a score that was
already on the board and showed nothing. Kept here, a scoring play is a fact
with a time on it, and any card loaded inside the window shows its banner
until the same moment.

Pure, like :mod:`pending_plays`: the coordinator feeds it the games feed once
per poll and the sensor reads the live banners back.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .yahoo_redzone import team_abbr

# How long a banner stays up after the poll that saw the score.
BANNER_SECONDS = 30.0


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

    def observe(self, games: list[Any], now: float) -> None:
        """Take one poll's view of the slate.

        A game seen for the first time is the baseline, never a banner — a
        restart mid-game must not announce every score already on the board.
        Only a game in progress can raise one.
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
        for game_id in set(self._seen) - current:
            del self._seen[game_id]
        for game_id, banner in list(self._banners.items()):
            if game_id not in current or banner["until"] <= now:
                del self._banners[game_id]

    def active(self, now: float) -> dict[str, dict[str, Any]]:
        """``{game_id: banner}`` for every banner still up at ``now``."""
        return {gid: dict(b) for gid, b in self._banners.items() if b["until"] > now}
