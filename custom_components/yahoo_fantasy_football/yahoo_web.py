"""Parse Yahoo's public fantasy **web** pages into the engine's shapes.

This is a second, independent source of league data, separate from the Fantasy
API in ``api.py``. It exists because Yahoo's web tier serves *public* leagues to
anonymous clients — no OAuth, no token, no account — and because it carries two
things the API does not expose at all:

* a **per-player projection** (the API has team projections only), and
* a **per-player stat line** (``1 Rush TD, 123 Pass Yds, 1 Pass TD``), which is
  strictly richer than the point-delta inference in :mod:`plays`.

Scope and non-goals
-------------------
Everything here is **pure**: HTML string in, dataclasses out. No network, no
clock, no Home Assistant imports — fetching and scheduling belong to the
coordinator, so this module stays trivially testable against saved fixtures.

Two cautions that belong in the code rather than only in the plan:

* Only leagues whose privacy setting is *public* render anonymously. A private
  league redirects to ``login.yahoo.com`` and there is no parseable page at all;
  callers must detect that (see :func:`is_login_redirect`) rather than feeding
  the login HTML in here and getting a confusing empty result.
* Everything was verified against a **completed** week. Whether in-progress rows
  carry the same shape is unverified until real games are played; the game-note
  field is therefore kept as raw text and classified defensively.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

# Column layout of the mirrored stat tables. Yahoo renders BOTH teams in one
# table: the left team's columns, three duplicate position columns (responsive
# variants of the same value), then the right team's columns mirrored.
#
#   [0] stats  [1] player  [2] proj  [3] points
#   [4] pos    [5] pos     [6] pos          <- identical, breakpoint variants
#   [7] points [8] proj    [9] player [10] stats
_LEFT = (0, 1, 2, 3)
_RIGHT = (10, 9, 8, 7)
_SLOT = 4
_EXPECTED_CELLS = 11

BENCH_SLOTS = frozenset({"BN", "IR", "IR+", "IR-R", "NA"})

_PLAYER_HREF = re.compile(r"/nfl/players/(\d+)")
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")

# Yahoo appends the player's NFL game to the same cell as their name, e.g.
# "Final W 26-7 @ Pit" or "Bye". Live wording is UNVERIFIED — see module docstring.
_GAME_FINAL = re.compile(r"\bFinal\b", re.I)
_GAME_BYE = re.compile(r"\bBye\b", re.I)
_GAME_LIVE = re.compile(r"\b(?:Q[1-5]|OT|Halftime|Half)\b", re.I)


@dataclass(frozen=True)
class WebPlayer:
    """One rostered player as the public matchup page reports them."""

    player_id: str
    """Yahoo's numeric player id, from the player-page link. Stable across weeks."""
    name: str
    slot: str
    """Lineup slot — ``BN`` for bench, ``QB``/``WR``/``W/R``/... for starters."""
    points: float | None
    projected: float | None
    stat_line: str = ""
    """Free text, e.g. ``1 Rush TD, 123 Pass Yds, 1 Pass TD``. Empty before kickoff."""
    game_note: str = ""
    """Raw NFL-game text as printed, e.g. ``Final W 26-7 @ Pit``."""
    team_side: int = 0
    """0 = left team, 1 = right team."""

    @property
    def starter(self) -> bool:
        return self.slot.upper() not in BENCH_SLOTS

    @property
    def game_state(self) -> str:
        """``pre`` / ``in`` / ``post`` / ``bye`` / ``unknown``.

        Deliberately conservative: anything unrecognised reads ``unknown``
        rather than being forced into a bucket, because the live wording has
        not been observed yet.
        """
        note = self.game_note
        if not note:
            return "unknown"
        if _GAME_BYE.search(note):
            return "bye"
        if _GAME_FINAL.search(note):
            return "post"
        if _GAME_LIVE.search(note):
            return "in"
        return "unknown"


@dataclass(frozen=True)
class WebTeam:
    """One fantasy team's side of a matchup."""

    team_id: str
    name: str
    points: float | None = None
    projected: float | None = None


@dataclass(frozen=True)
class WebMatchup:
    """Both sides of one head-to-head matchup, with full rosters."""

    week: int
    teams: tuple[WebTeam, WebTeam]
    players: list[WebPlayer] = field(default_factory=list)

    def roster(self, side: int) -> list[WebPlayer]:
        return [p for p in self.players if p.team_side == side]


# ---------------------------------------------------------------------------
# URLs
# ---------------------------------------------------------------------------

_BASE = "https://football.fantasysports.yahoo.com"


def _prefix(season: int | None) -> str:
    """Archived seasons live under ``/<year>/f1/``; the current one under ``/f1/``."""
    return f"/{season}/f1" if season else "/f1"


def scoreboard_url(league_id: str | int, week: int, season: int | None = None) -> str:
    """League-wide scoreboard — every matchup, scores only."""
    return f"{_BASE}{_prefix(season)}/{league_id}?week={week}"


def matchup_url(
    league_id: str | int, week: int, matchup_id: int = 1, season: int | None = None
) -> str:
    """One matchup, with both full rosters."""
    return f"{_BASE}{_prefix(season)}/{league_id}/matchup?week={week}&mid1={matchup_id}"


def is_login_redirect(url: str | None, body: str | None = None) -> bool:
    """True when Yahoo bounced us to sign-in — i.e. the league is **private**.

    Checked on the *final* URL after redirects. A private league is not a
    transient failure and must not be retried as one; it means this data source
    cannot serve that league at all.
    """
    if url and "login.yahoo.com" in url:
        return True
    return bool(body and "login.yahoo.com" in body[:2000])


# ---------------------------------------------------------------------------
# HTML extraction
# ---------------------------------------------------------------------------


@dataclass
class _Cell:
    text: str = ""
    player_id: str | None = None
    player_name: str | None = None


class _TableParser(HTMLParser):
    """Collect rows of cells from the table with a given ``id``.

    Uses the stdlib parser rather than regexes so attribute order, self-closing
    tags and stray markup do not matter. The only structural assumptions are the
    table's ``id`` and that rows are ``<tr>`` of ``<td>``/``<th>`` — not the
    hashed ``_yb_*`` CSS classes, which change on every Yahoo frontend build.
    """

    def __init__(self, table_id: str) -> None:
        super().__init__(convert_charrefs=True)
        self._want = table_id
        self._depth = 0
        self._in_table = False
        self._in_cell = False
        self._skip = 0
        self.rows: list[list[_Cell]] = []
        self._row: list[_Cell] = []
        self._cell = _Cell()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag == "table":
            self._depth += 1
            if a.get("id") == self._want:
                self._in_table = True
                self._table_depth = self._depth
            return
        if not self._in_table:
            return
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th"):
            self._in_cell = True
            self._cell = _Cell()
        elif tag == "a" and self._in_cell:
            href = a.get("href") or ""
            m = _PLAYER_HREF.search(href)
            if m and self._cell.player_id is None:
                self._cell.player_id = m.group(1)
                # ``title`` is the clean name; the link text can carry markup.
                self._cell.player_name = (a.get("title") or "").strip() or None
        elif tag == "script" or tag == "style":
            self._skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "table":
            if self._in_table and self._depth == getattr(self, "_table_depth", -1):
                self._in_table = False
            self._depth -= 1
            return
        if not self._in_table:
            return
        if tag == "tr":
            if self._row:
                self.rows.append(self._row)
            self._row = []
        elif tag in ("td", "th"):
            self._cell.text = " ".join(self._cell.text.split())
            self._row.append(self._cell)
            self._in_cell = False
        elif tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if self._in_table and self._in_cell and not self._skip:
            self._cell.text += data


def _table_rows(html: str, table_id: str) -> list[list[_Cell]]:
    parser = _TableParser(table_id)
    parser.feed(html)
    return parser.rows


def _number(text: str) -> float | None:
    """First number in a cell, or None. ``-`` and ``--`` are legitimately empty."""
    m = _NUMBER.search(text or "")
    return float(m.group()) if m else None


def _split_player_cell(cell: _Cell) -> tuple[str, str]:
    """Return ``(name, game_note)`` from a player cell.

    The cell holds the name, an optional "Video Forecast" promo link, and the
    player's NFL game. The name comes from the link's ``title`` attribute, so
    the note is whatever remains once the name and the promo text are removed.
    """
    name = cell.player_name or ""
    note = cell.text
    if name and note.startswith(name):
        note = note[len(name) :]
    note = note.replace("Video Forecast", " ")
    return name, " ".join(note.split())


_DEFENSE = re.compile(r"^(?P<name>.+?\s-\s+DEF)\b", re.I)


def _defense_id(text: str) -> str | None:
    """Synthesise a stable id for a team defense.

    Team defenses are the one roster slot Yahoo renders with **no** player link
    and no ``data-ys-playerid``, so there is no upstream id to read. The slug of
    the printed name is stable week to week, which is all the engine needs to
    diff a player against itself across polls.
    """
    m = _DEFENSE.match(text.strip())
    if not m:
        return None
    slug = re.sub(r"[^a-z0-9]+", "-", m.group("name").lower()).strip("-")
    return f"def-{slug}"


def _split_defense_cell(text: str) -> tuple[str, str]:
    m = _DEFENSE.match(text.strip())
    if not m:
        return " ".join(text.split()), ""
    name = " ".join(m.group("name").split())
    return name, " ".join(text.strip()[m.end() :].split())


def _players_from_rows(rows: list[list[_Cell]]) -> list[WebPlayer]:
    out: list[WebPlayer] = []
    for row in rows:
        if len(row) < _EXPECTED_CELLS:
            continue  # header, spacer, or a layout row — not a roster row
        slot = row[_SLOT].text.strip()
        if not slot or slot.lower() == "pos":
            continue  # header row
        for side, (stats_i, player_i, proj_i, pts_i) in enumerate((_LEFT, _RIGHT)):
            cell = row[player_i]
            name, note = _split_player_cell(cell)
            player_id = cell.player_id or _defense_id(cell.text)
            if not player_id:
                continue  # genuinely empty side — uneven rosters
            if not name:
                # Team defenses carry no player link, so the name has to come
                # from the cell text with the trailing game note stripped.
                name, note = _split_defense_cell(cell.text)
            out.append(
                WebPlayer(
                    player_id=player_id,
                    name=name,
                    slot=slot,
                    points=_number(row[pts_i].text),
                    projected=_number(row[proj_i].text),
                    stat_line=row[stats_i].text,
                    game_note=note,
                    team_side=side,
                )
            )
    return out


def _totals_row(rows: list[list[_Cell]]) -> tuple[float | None, float | None, float | None, float | None]:
    """``(left_proj, left_pts, right_pts, right_proj)`` from the Total row."""
    for row in reversed(rows):
        if len(row) < _EXPECTED_CELLS:
            continue
        if "total" in row[_SLOT].text.strip().lower():
            return (
                _number(row[_LEFT[2]].text),
                _number(row[_LEFT[3]].text),
                _number(row[_RIGHT[3]].text),
                _number(row[_RIGHT[2]].text),
            )
    return (None, None, None, None)


_TEAM_LINK = re.compile(
    r'<a[^>]+href="[^"]*?/(?P<league>\d+)/(?P<team>\d+)"[^>]*>(?P<label>.*?)</a>',
    re.S,
)
_TAGS = re.compile(r"<[^>]+>")


def _team_names(html: str) -> list[tuple[str, str]]:
    """``[(team_id, name)]`` in page order, de-duplicated."""
    seen: list[tuple[str, str]] = []
    for m in _TEAM_LINK.finditer(html):
        label = " ".join(_TAGS.sub(" ", m.group("label")).split())
        pair = (m.group("team"), label)
        if label and pair not in seen:
            seen.append(pair)
    return seen


# ---------------------------------------------------------------------------
# Public parsers
# ---------------------------------------------------------------------------


def parse_matchup(html: str, week: int) -> WebMatchup:
    """Parse a ``/matchup`` page into both rosters and both team totals.

    Raises ``ValueError`` if no roster table is found, which is what a login
    page, an error page, or a Yahoo redesign all look like from here — callers
    should treat it as "this source is unavailable", never as "zero points".
    """
    starters = _table_rows(html, "statTable1")
    bench = _table_rows(html, "statTable2")
    if not starters and not bench:
        raise ValueError("no roster table found — login page, error page, or layout change")

    players = _players_from_rows(starters) + _players_from_rows(bench)
    left_proj, left_pts, right_pts, right_proj = _totals_row(starters)

    names = _team_names(html)
    left_name = names[0][1] if len(names) > 0 else "Team 1"
    right_name = names[1][1] if len(names) > 1 else "Team 2"
    left_id = names[0][0] if len(names) > 0 else "1"
    right_id = names[1][0] if len(names) > 1 else "2"

    return WebMatchup(
        week=week,
        teams=(
            WebTeam(left_id, left_name, left_pts, left_proj),
            WebTeam(right_id, right_name, right_pts, right_proj),
        ),
        players=players,
    )


_MATCHUPS_START = "matchups-body"
_MATCHUPS_END = re.compile(r'(?:id|class)="[^"]*\btransactions\b')
_SCORE = re.compile(r">\s*(-?\d{1,3}\.\d{2})\s*<")


def _matchups_container(html: str) -> str:
    """Slice out just the week's matchup list.

    Bounding this matters more than it looks. The scoreboard page also renders a
    **playoff bracket** (``yfa-matchup`` blocks, carrying weeks 16/17 scores) and
    a recent-transactions module that links team names — both would contribute
    plausible-looking names and decimals to an unbounded scan, and the bracket's
    scores are for a completely different week.
    """
    start = html.find(_MATCHUPS_START)
    if start < 0:
        return ""
    end = _MATCHUPS_END.search(html, start)
    return html[start : end.start() if end else len(html)]


def parse_scoreboard(html: str, week: int) -> list[tuple[WebTeam, WebTeam]]:
    """Parse the league page into ``[(home, away), ...]`` for the week.

    Each matchup contributes two team links and four numbers, in the order
    ``home points, home projected, away points, away projected``.

    Thin by design — the scoreboard carries no rosters, so a card fetches the
    matchup page when a row is expanded.
    """
    seg = _matchups_container(html)
    if not seg:
        return []
    names = _team_names(seg)
    nums = [float(x) for x in _SCORE.findall(seg)]

    out: list[tuple[WebTeam, WebTeam]] = []
    for i in range(min(len(names) // 2, len(nums) // 4)):
        (home_id, home_name), (away_id, away_name) = names[2 * i], names[2 * i + 1]
        hp, hproj, ap, aproj = nums[4 * i : 4 * i + 4]
        out.append(
            (
                WebTeam(home_id, home_name, hp, hproj),
                WebTeam(away_id, away_name, ap, aproj),
            )
        )
    return out


# ---------------------------------------------------------------------------
# Bridge into the scoring engine
# ---------------------------------------------------------------------------


def to_snapshot(
    matchups: list[WebMatchup],
    week: int,
    taken_at: float,
    league_id: str | int = "0",
):
    """Fold parsed matchups into a :class:`plays.LeagueSnapshot`.

    Imported lazily so this module stays importable on its own. ``taken_at`` is
    supplied by the caller — nothing here reads a clock.
    """
    from .plays import LeagueSnapshot, PlayerSnapshot

    players: dict[str, PlayerSnapshot] = {}
    for index, matchup in enumerate(matchups):
        matchup_id = f"w{week}.m{index + 1}"
        for player in matchup.players:
            team = matchup.teams[player.team_side]
            key = f"{league_id}.p.{player.player_id}"
            players[key] = PlayerSnapshot(
                player_key=key,
                name=player.name,
                points=float(player.points or 0.0),
                team_key=f"{league_id}.t.{team.team_id}",
                matchup_id=matchup_id,
                nfl_team=None,  # the matchup page prints the opponent, not the team
                position=player.slot,
                selected_position=player.slot,
            )
    return LeagueSnapshot(week=week, taken_at=taken_at, players=players)
