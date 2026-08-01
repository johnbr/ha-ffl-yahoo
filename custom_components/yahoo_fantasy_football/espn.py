"""ESPN NFL enrichment — turn real scoring plays into per-player credits.

Yahoo's Fantasy API exposes point *totals*, never events, so the scoring-play
feed is synthesized by diffing player points between polls (``plays.py``). That
gives an accurate value but no description. This module supplies the
description, by reading ESPN's public NFL API and attributing each scoring play
to the athletes involved.

Everything here is **optional enrichment**. A failure anywhere in this module
must degrade to the Yahoo-only event text, never break a refresh.

Two halves:

* Pure parsing (no network, no Home Assistant) — the bulk of the module and all
  of the risk. Unit-tested against real captured payloads in
  ``tests/fixtures/espn_scoring_plays.json``.
* A thin cached client over ESPN's unauthenticated endpoints.

**Why parse free text at all?** ESPN's ``scoringPlays[]`` entries carry a team
and a text string but **no athlete IDs** — the ids only exist on the core-API
play objects behind per-play ``$ref`` fetches, which is a fan-out we don't want
for an optional feature. So we parse, and gate every result on a confidence
score the caller can threshold.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

_LOGGER = logging.getLogger(__name__)

SITE_API = "https://site.api.espn.com/apis/site/v2/sports/football/nfl"

# ---------------------------------------------------------------------------
# Credit roles
# ---------------------------------------------------------------------------
# What a named athlete actually did on a scoring play. These map onto the stat
# categories every fantasy scoring system prices, so the caller can decide
# which credits are relevant to a given rostered player without re-reading the
# text.
ROLE_PASSING_TD = "passing_td"
ROLE_RECEIVING_TD = "receiving_td"
ROLE_RUSHING_TD = "rushing_td"
ROLE_FIELD_GOAL = "field_goal"
ROLE_PAT = "pat"
ROLE_PAT_MISSED = "pat_missed"
ROLE_TWO_POINT_PASS = "two_point_pass"
ROLE_TWO_POINT_RECEPTION = "two_point_reception"
ROLE_DEFENSIVE_TD = "defensive_td"
ROLE_RETURN_TD = "return_td"

# Roles that credit a team defense rather than (or as well as) the individual.
# Fantasy D/ST scores the return touchdown; leagues running IDP also credit the
# individual, so both are emitted and the caller picks.
TEAM_DEFENSE_ROLES = frozenset({ROLE_DEFENSIVE_TD})


@dataclass(frozen=True)
class ScoringCredit:
    """One athlete's involvement in one scoring play."""

    name: str
    """Athlete name exactly as ESPN printed it, e.g. ``Michael Penix Jr.``."""

    role: str
    """One of the ``ROLE_*`` constants."""

    yards: int | None = None
    """Play distance where meaningful — FG distance, TD length. ``None`` for PATs."""


@dataclass(frozen=True)
class MatchResult:
    """Outcome of testing one rostered player against one play's credits."""

    matched: bool
    confidence: float
    credit: ScoringCredit | None = None
    reason: str = ""


# ---------------------------------------------------------------------------
# Name normalization
# ---------------------------------------------------------------------------

# Generational suffixes. Yahoo and ESPN disagree constantly about whether to
# include these ("Michael Penix Jr." vs "Michael Penix"), so they are stripped
# from both sides before comparison rather than special-cased at match time.
_SUFFIXES = frozenset({"jr", "jr.", "sr", "sr.", "ii", "iii", "iv", "v"})

_PUNCT_RE = re.compile(r"[.,'\u2019]")
_NON_NAME_RE = re.compile(r"[^a-z0-9\s-]")
_WS_RE = re.compile(r"\s+")


def normalize_name(name: str) -> str:
    """Casefold a personal name to a comparable form.

    Drops punctuation (``Ka'imi`` → ``kaimi``, ``D.K.`` → ``dk``) and
    generational suffixes, then collapses whitespace. Hyphens are kept — they
    separate real name components rather than decorating them.
    """
    if not name:
        return ""
    text = _PUNCT_RE.sub("", name.lower())
    text = _NON_NAME_RE.sub(" ", text)
    parts = [p for p in _WS_RE.split(text) if p]
    while len(parts) > 1 and parts[-1] in _SUFFIXES:
        parts.pop()
    return " ".join(parts)


def _is_initial_form(a: str, b: str) -> bool:
    """True when one first name is an initial matching the other's first letter.

    ``normalize_name`` has already stripped the period, so ``"B."`` arrives as
    the single character ``"b"``. Requires exactly one side to be abbreviated —
    two spelled-out first names are compared in full, never by initial.
    """
    if not a or not b:
        return False
    if len(a) == 1 and len(b) == 1:
        return a == b
    if len(a) == 1:
        return a == b[0]
    if len(b) == 1:
        return b == a[0]
    return False


def name_parts(name: str) -> tuple[str, str]:
    """Return ``(first, last)`` from a normalized name. Last word wins as surname."""
    parts = normalize_name(name).split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return "", parts[0]
    return parts[0], parts[-1]


# ---------------------------------------------------------------------------
# Scoring-play text parsing
# ---------------------------------------------------------------------------

# A name as it appears inside play text: capitalised words, possibly with
# apostrophes, hyphens, internal periods (D.K.) and a trailing suffix.
_NAME = r"[A-Z][A-Za-z'\u2019.\-]*(?:\s+[A-Z][A-Za-z'\u2019.\-]*)*"

# "<Receiver> 25 Yd pass from <Passer>"
_PASS_RE = re.compile(rf"^(?P<receiver>{_NAME})\s+(?P<yards>\d+)\s+Yd\s+pass\s+from\s+(?P<passer>{_NAME})\s*$", re.I)
# "<Rusher> 4 Yd Rush" / "... Yd Run"
_RUSH_RE = re.compile(rf"^(?P<rusher>{_NAME})\s+(?P<yards>\d+)\s+Yd\s+(?:Rush|Run)\s*$", re.I)
# "<Kicker> 48 Yd Field Goal"
_FG_RE = re.compile(rf"^(?P<kicker>{_NAME})\s+(?P<yards>\d+)\s+Yd\s+Field\s+Goal\s*$", re.I)
# "<Defender> 32 Yd Interception Return" / "66 Yd Fumble Recovery" / "Fumble Return"
_DEF_RE = re.compile(
    rf"^(?P<player>{_NAME})\s+(?P<yards>\d+)\s+Yd\s+"
    r"(?:Interception\s+Return|Fumble\s+Recovery|Fumble\s+Return|Blocked\s+Punt\s+Return)\s*$",
    re.I,
)
# "<Returner> 90 Yd Punt Return" / "Kickoff Return"
_RETURN_RE = re.compile(
    rf"^(?P<player>{_NAME})\s+(?P<yards>\d+)\s+Yd\s+(?:Punt|Kickoff|Kick)\s+Return\s*$",
    re.I,
)
# ESPN ships a typo here — "Touchown" — so match the stem loosely.
# "Blocked Kick Recovered by Jordan Davis (PHI) Jordan Davis 61 Yd Touchown Return"
_BLOCKED_RE = re.compile(
    rf"Recovered\s+by\s+(?P<player>{_NAME})\s*\([A-Z]{{2,4}}\).*?(?P<yards>\d+)\s+Yd\s+Touch",
    re.I,
)

# Parentheticals.
_PAT_RE = re.compile(rf"^(?P<kicker>{_NAME})\s+Kick$", re.I)
_PAT_FAIL_RE = re.compile(rf"^(?P<kicker>{_NAME})\s+(?:PAT\s+Failed|Kick\s+Failed)$", re.I)
_TWO_PT_RE = re.compile(
    rf"^(?P<passer>{_NAME})\s+Pass\s+to\s+(?P<receiver>{_NAME})\s+for\s+Two-Point\s+Conversion$", re.I
)
_TWO_PT_RUN_RE = re.compile(rf"^(?P<rusher>{_NAME})\s+Run\s+for\s+Two-Point\s+Conversion$", re.I)
# A bare team abbreviation, e.g. "(PHI)". NOT a kicker — this is the trap in the
# blocked-kick format, where a naive "parenthetical means PAT" rule invents an
# athlete named "PHI".
_TEAM_ABBR_RE = re.compile(r"^[A-Z]{2,4}$")

_PAREN_RE = re.compile(r"\(([^)]*)\)")


def _parse_parenthetical(body: str, credits: list[ScoringCredit]) -> None:
    """Append credits for one ``(...)`` group. Unknown shapes are ignored."""
    body = body.strip()
    if not body or _TEAM_ABBR_RE.match(body):
        return  # bare team abbreviation, or empty — no athlete here

    if m := _PAT_RE.match(body):
        credits.append(ScoringCredit(m["kicker"].strip(), ROLE_PAT))
    elif m := _PAT_FAIL_RE.match(body):
        credits.append(ScoringCredit(m["kicker"].strip(), ROLE_PAT_MISSED))
    elif m := _TWO_PT_RE.match(body):
        credits.append(ScoringCredit(m["passer"].strip(), ROLE_TWO_POINT_PASS))
        credits.append(ScoringCredit(m["receiver"].strip(), ROLE_TWO_POINT_RECEPTION))
    elif m := _TWO_PT_RUN_RE.match(body):
        credits.append(ScoringCredit(m["rusher"].strip(), ROLE_RUSHING_TD))
    # "Two-Point Pass Conversion Failed" and friends name nobody — nothing to do.


def parse_scoring_play(text: str) -> list[ScoringCredit]:
    """Parse one ESPN ``scoringPlays[].text`` into per-athlete credits.

    Returns an empty list for plays that name nobody (``Defensive Holding in
    Endzone for Safety``) or whose shape isn't recognised — an unparsed play
    simply gets no enrichment.

    A single play routinely credits three athletes: a passing touchdown names
    the receiver, the passer and the PAT kicker.
    """
    if not text:
        return []

    # ESPN emits trailing whitespace on a large fraction of field-goal texts.
    text = text.strip()

    credits: list[ScoringCredit] = []

    # Split the main clause from its parentheticals. The blocked-kick format
    # embeds "(PHI)" mid-sentence, so the main clause is everything outside any
    # parentheses rather than everything before the first one.
    parenthetical_bodies = _PAREN_RE.findall(text)
    main = _PAREN_RE.sub(" ", text)
    main = _WS_RE.sub(" ", main).strip()

    if m := _PASS_RE.match(main):
        yards = int(m["yards"])
        credits.append(ScoringCredit(m["receiver"].strip(), ROLE_RECEIVING_TD, yards))
        credits.append(ScoringCredit(m["passer"].strip(), ROLE_PASSING_TD, yards))
    elif m := _RUSH_RE.match(main):
        credits.append(ScoringCredit(m["rusher"].strip(), ROLE_RUSHING_TD, int(m["yards"])))
    elif m := _FG_RE.match(main):
        credits.append(ScoringCredit(m["kicker"].strip(), ROLE_FIELD_GOAL, int(m["yards"])))
    elif m := _DEF_RE.match(main):
        credits.append(ScoringCredit(m["player"].strip(), ROLE_DEFENSIVE_TD, int(m["yards"])))
    elif m := _RETURN_RE.match(main):
        credits.append(ScoringCredit(m["player"].strip(), ROLE_RETURN_TD, int(m["yards"])))
    elif m := _BLOCKED_RE.search(text):
        # Searched against the ORIGINAL text — this format needs its embedded
        # "(TEAM)" group for context, which stripping parentheses removes.
        credits.append(ScoringCredit(m["player"].strip(), ROLE_DEFENSIVE_TD, int(m["yards"])))

    for body in parenthetical_bodies:
        _parse_parenthetical(body, credits)

    return credits


# ---------------------------------------------------------------------------
# Matching credits to rostered players
# ---------------------------------------------------------------------------

# Confidence floor for treating a credit as belonging to a rostered player.
# Surname-only agreement sits below this deliberately: NFL rosters carry
# repeated surnames (two Smiths on one team is routine), and a wrong
# attribution is worse than no enrichment at all.
DEFAULT_CONFIDENCE_THRESHOLD = 0.75


def match_play_to_player(
    credits: list[ScoringCredit],
    *,
    player_name: str,
    player_team: str | None = None,
    play_team: str | None = None,
    position: str | None = None,
    threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
) -> MatchResult:
    """Decide whether any credit on a play belongs to ``player_name``.

    ``player_team``/``play_team`` are NFL team abbreviations. When both are
    known and disagree, the match is rejected outright — the same surname on the
    other sideline is the single most likely false positive.

    A ``position`` of ``DEF``/``D/ST`` matches team-defense credits by team
    rather than by name, since the fantasy scorer is the unit, not the defender
    ESPN named.
    """
    if not credits or not player_name:
        return MatchResult(False, 0.0, reason="no credits or no player")

    same_team = None
    if player_team and play_team:
        same_team = player_team.upper() == play_team.upper()

    # Team defense: the individual defender ESPN names is irrelevant.
    if position and position.upper().replace("/", "").replace("-", "") in {"DEF", "DST"}:
        if same_team:
            for credit in credits:
                if credit.role in TEAM_DEFENSE_ROLES:
                    return MatchResult(True, 0.9, credit, "team defense scored on this play")
        return MatchResult(False, 0.0, reason="no defensive credit for this team")

    if same_team is False:
        return MatchResult(False, 0.0, reason="player is not on the scoring team")

    target_norm = normalize_name(player_name)
    target_first, target_last = name_parts(player_name)

    best = MatchResult(False, 0.0, reason="no name overlap")
    for credit in credits:
        cred_norm = normalize_name(credit.name)
        cred_first, cred_last = name_parts(credit.name)

        if cred_norm == target_norm:
            confidence = 1.0 if same_team else 0.9
            reason = "exact name match"
        elif cred_last != target_last:
            continue
        elif _is_initial_form(cred_first, target_first):
            # "B. Robinson" vs "Bijan Robinson" — one side is genuinely
            # abbreviated, so the initial is all the evidence available.
            confidence = 0.8 if same_team else 0.5
            reason = "surname and first initial match"
        else:
            # Surnames agree but both first names are spelled out. If they
            # differ these are DIFFERENT PEOPLE, however similar they look:
            # "Bijan Robinson" (ATL) and "Brian Robinson" (a real contemporary)
            # share a surname and a first initial and are not the same player.
            # Comparing first initials here would manufacture exactly the false
            # positive the threshold exists to prevent.
            confidence = 0.6 if same_team else 0.3
            reason = "surname only"

        if confidence > best.confidence:
            best = MatchResult(confidence >= threshold, confidence, credit, reason)

    return best


# ---------------------------------------------------------------------------
# Scoreboard normalization
# ---------------------------------------------------------------------------


def normalize_scoreboard(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Reduce a scoreboard response to the per-game facts the coordinator needs.

    Drops the ~95% of the payload that is news, video, odds and standings.
    ``last_play`` is only present while a game is live.
    """
    games: list[dict[str, Any]] = []
    for event in payload.get("events") or []:
        competitions = event.get("competitions") or []
        if not competitions:
            continue
        comp = competitions[0]
        status = (event.get("status") or {}).get("type") or {}

        teams: dict[str, Any] = {}
        for competitor in comp.get("competitors") or []:
            side = competitor.get("homeAway")
            team = competitor.get("team") or {}
            if side:
                teams[side] = {
                    "abbr": team.get("abbreviation"),
                    "name": team.get("displayName"),
                    "logo": team.get("logo"),
                    "score": competitor.get("score"),
                }

        situation = comp.get("situation") or {}
        last_play = (situation.get("lastPlay") or {}).get("text")

        games.append(
            {
                "event_id": event.get("id"),
                "state": status.get("state"),  # pre | in | post
                "completed": bool(status.get("completed")),
                "detail": status.get("shortDetail"),
                "start": event.get("date"),
                "home": teams.get("home"),
                "away": teams.get("away"),
                "last_play": last_play,
            }
        )
    return games


def normalize_scoring_plays(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Reduce a summary response to its scoring plays, with credits parsed."""
    plays: list[dict[str, Any]] = []
    for play in payload.get("scoringPlays") or []:
        text = (play.get("text") or "").strip()
        clock = play.get("clock") or {}
        period = play.get("period") or {}
        plays.append(
            {
                "id": play.get("id"),
                "text": text,
                "team": (play.get("team") or {}).get("abbreviation"),
                "scoring_type": (play.get("scoringType") or {}).get("name"),
                "type": (play.get("type") or {}).get("text"),
                "period": period.get("number"),
                "clock": clock.get("displayValue"),
                "away_score": play.get("awayScore"),
                "home_score": play.get("homeScore"),
                "credits": parse_scoring_play(text),
            }
        )
    return plays


# ---------------------------------------------------------------------------
# Cached client
# ---------------------------------------------------------------------------

# A live game's summary changes constantly; a finished one never does. Cache
# accordingly so a Sunday slate costs one scoreboard call plus one summary per
# in-progress game per cycle.
SCOREBOARD_TTL = 30
SUMMARY_LIVE_TTL = 45
SUMMARY_FINAL_TTL = 6 * 3600
STALE_FALLBACK = 15 * 60
REQUEST_TIMEOUT = 20


@dataclass
class _CacheEntry:
    fetched_at: float
    payload: dict[str, Any]
    ttl: float = 0.0


@dataclass
class EspnClient:
    """Minimal cached client for ESPN's public NFL endpoints.

    No API key, no auth. Every method returns ``None`` rather than raising when
    ESPN is unreachable and no usable cache exists — enrichment is optional and
    must never fail a coordinator refresh.
    """

    session: Any
    """An ``aiohttp.ClientSession``; in HA use ``async_get_clientsession(hass)``."""

    _cache: dict[str, _CacheEntry] = field(default_factory=dict, repr=False)

    async def _get(self, url: str, ttl: float) -> dict[str, Any] | None:
        now = time.time()
        entry = self._cache.get(url)
        if entry is not None and now - entry.fetched_at < ttl:
            return entry.payload

        try:
            import aiohttp

            async with self.session.get(
                url,
                headers={"User-Agent": "Home Assistant", "Accept": "application/json"},
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
            ) as resp:
                if resp.status != 200:
                    raise OSError(f"HTTP {resp.status}")
                payload = await resp.json()
        # Enrichment must never raise into a coordinator refresh.
        except Exception as err:
            if entry is not None and now - entry.fetched_at < STALE_FALLBACK:
                _LOGGER.debug("ESPN fetch failed (%s), serving cached %s", err, url)
                return entry.payload
            _LOGGER.debug("ESPN fetch failed (%s) with no usable cache: %s", err, url)
            return None

        self._cache[url] = _CacheEntry(now, payload, ttl)
        return payload

    async def async_scoreboard(self, date: str | None = None) -> list[dict[str, Any]]:
        """Games for one ``YYYYMMDD`` date (today if omitted). ``[]`` on failure."""
        url = f"{SITE_API}/scoreboard" + (f"?dates={date}" if date else "")
        payload = await self._get(url, SCOREBOARD_TTL)
        return normalize_scoreboard(payload) if payload else []

    async def async_scoring_plays(self, event_id: str, *, live: bool = True) -> list[dict[str, Any]]:
        """Scoring plays for one game, credits already parsed. ``[]`` on failure."""
        url = f"{SITE_API}/summary?event={event_id}"
        payload = await self._get(url, SUMMARY_LIVE_TTL if live else SUMMARY_FINAL_TTL)
        return normalize_scoring_plays(payload) if payload else []
