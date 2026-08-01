"""Demo league — a synthetic Sunday, for building and evaluating the cards.

Three jobs:

* **Unblock card work.** The regular season is months away and the real data
  path needs Yahoo credentials; the cards need something to render now.
* **Let a user see the cards before authorizing.** Yahoo requires every user to
  register their own API credentials (there is no unauthenticated tier — see
  ``PLAN.md``). Demo mode makes that a decision someone takes *after* seeing
  whether the integration is worth it.
* **Give reviewers and screenshots something real to look at.**

Design constraints that matter:

* It emits **exactly** the shapes ``plays.py`` consumes — ``LeagueSnapshot`` of
  ``PlayerSnapshot`` — so the cards are never built against a fiction that the
  live coordinator then fails to reproduce.
* It is **pure and deterministic**. No wall clock, no global RNG, no network.
  ``snapshot_at(league, elapsed)`` is a function of its arguments alone, so the
  same elapsed time always yields the same league state. Re-polling an unchanged
  instant produces no phantom events, which is what makes the engine's
  idempotency guarantee testable end to end.
* Scoring happens in **steps, not ramps**. Each player gets a script of discrete
  moments at league-build time; a snapshot sums the moments that have already
  happened. Real fantasy points arrive in lumps, and a smooth ramp would produce
  a stream of tiny deltas that looks nothing like a real feed.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from .plays import LeagueSnapshot, PlayerSnapshot

# A simulated game day, compressed. Twenty minutes is long enough to watch the
# feed evolve and short enough to sit through while working on a card.
DEMO_CYCLE_SECONDS = 20 * 60

# Kickoff offsets within the cycle, as fractions: the early slate, the late
# slate, and a night game. Staggering them means the board always shows a mix of
# "yet to play", "in progress" and "final", which is the layout worth designing
# against — a board where every game is live is the easy case.
_WINDOWS = ((0.05, 0.45), (0.35, 0.75), (0.62, 0.98))

_STARTING_SLOTS = ("QB", "RB", "RB", "WR", "WR", "WR", "TE", "K", "DEF")
_BENCH_SIZE = 3
_TEAM_COUNT = 10

# 10 teams x (9 starters + 3 bench) = 120 players, and the pool below must
# cover every position's demand with room to spare. ``build_league`` raises if
# it cannot fill a slot rather than quietly dealing a short roster — an
# incomplete roster is the kind of thing that looks fine on the board and only
# shows up once someone opens the popup.

# Points a scoring moment is worth, by position, with rough weights. Kickers
# score often and small; defenses lump.
_MOMENT_VALUES = {
    "QB": ((0.6, 8), (1.2, 6), (2.4, 4), (4.0, 3), (6.0, 2)),
    "RB": ((0.7, 8), (1.5, 5), (3.0, 3), (6.2, 2)),
    "WR": ((0.8, 8), (1.6, 5), (3.2, 3), (6.4, 2)),
    "TE": ((0.7, 7), (1.4, 4), (6.3, 1)),
    "K": ((3.0, 6), (4.0, 3), (5.0, 1), (1.0, 4)),
    "DEF": ((1.0, 5), (2.0, 4), (6.0, 1), (-1.0, 2)),
}

_MANAGERS = (
    "Alex",
    "Sam",
    "Jordan",
    "Casey",
    "Riley",
    "Morgan",
    "Avery",
    "Quinn",
    "Rowan",
    "Skyler",
)

_TEAM_NAMES = (
    "Gridiron Gremlins",
    "Couch Commandos",
    "Blitz Brigade",
    "Hail Mary Hooligans",
    "Sunday Scaries",
    "Pylon Pirates",
    "Turf Monsters",
    "Red Zone Rebels",
    "Audible Anarchy",
    "Fourth & Foolish",
)

# Real 2025 NFL players, so the demo board reads as plausible rather than as
# obvious filler. Public factual data; nothing here is generated.
_POOL: tuple[tuple[str, str, str], ...] = (
    ("Josh Allen", "QB", "BUF"),
    ("Lamar Jackson", "QB", "BAL"),
    ("Jalen Hurts", "QB", "PHI"),
    ("Patrick Mahomes", "QB", "KC"),
    ("Joe Burrow", "QB", "CIN"),
    ("Jayden Daniels", "QB", "WSH"),
    ("C.J. Stroud", "QB", "HOU"),
    ("Baker Mayfield", "QB", "TB"),
    ("Justin Herbert", "QB", "LAC"),
    ("Michael Penix Jr.", "QB", "ATL"),
    ("Bo Nix", "QB", "DEN"),
    ("Caleb Williams", "QB", "CHI"),
    ("Saquon Barkley", "RB", "PHI"),
    ("Bijan Robinson", "RB", "ATL"),
    ("Jahmyr Gibbs", "RB", "DET"),
    ("Derrick Henry", "RB", "BAL"),
    ("Christian McCaffrey", "RB", "SF"),
    ("Ashton Jeanty", "RB", "LV"),
    ("De'Von Achane", "RB", "MIA"),
    ("Josh Jacobs", "RB", "GB"),
    ("Bucky Irving", "RB", "TB"),
    ("Kyren Williams", "RB", "LAR"),
    ("Chase Brown", "RB", "CIN"),
    ("James Cook", "RB", "BUF"),
    ("Breece Hall", "RB", "NYJ"),
    ("Kenneth Walker III", "RB", "SEA"),
    ("Omarion Hampton", "RB", "LAC"),
    ("Alvin Kamara", "RB", "NO"),
    ("Chuba Hubbard", "RB", "CAR"),
    ("Tony Pollard", "RB", "TEN"),
    ("Ja'Marr Chase", "WR", "CIN"),
    ("Justin Jefferson", "WR", "MIN"),
    ("CeeDee Lamb", "WR", "DAL"),
    ("Puka Nacua", "WR", "LAR"),
    ("Malik Nabers", "WR", "NYG"),
    ("Nico Collins", "WR", "HOU"),
    ("Brian Thomas Jr.", "WR", "JAX"),
    ("Amon-Ra St. Brown", "WR", "DET"),
    ("A.J. Brown", "WR", "PHI"),
    ("Drake London", "WR", "ATL"),
    ("Tee Higgins", "WR", "CIN"),
    ("Ladd McConkey", "WR", "LAC"),
    ("Garrett Wilson", "WR", "NYJ"),
    ("Terry McLaurin", "WR", "WSH"),
    ("Davante Adams", "WR", "LAR"),
    ("Mike Evans", "WR", "TB"),
    ("Marvin Harrison Jr.", "WR", "ARI"),
    ("DK Metcalf", "WR", "PIT"),
    ("Rashee Rice", "WR", "KC"),
    ("Jaxon Smith-Njigba", "WR", "SEA"),
    ("Emeka Egbuka", "WR", "TB"),
    ("Courtland Sutton", "WR", "DEN"),
    ("Jameson Williams", "WR", "DET"),
    ("Zay Flowers", "WR", "BAL"),
    ("Jaylen Waddle", "WR", "MIA"),
    ("Chris Olave", "WR", "NO"),
    ("Rome Odunze", "WR", "CHI"),
    ("Xavier Worthy", "WR", "KC"),
    ("Jerry Jeudy", "WR", "CLE"),
    ("Deebo Samuel", "WR", "WSH"),
    ("Rhamondre Stevenson", "RB", "NE"),
    ("Travis Etienne Jr.", "RB", "JAX"),
    ("Isiah Pacheco", "RB", "KC"),
    ("D'Andre Swift", "RB", "CHI"),
    ("Javonte Williams", "RB", "DAL"),
    ("Rachaad White", "RB", "TB"),
    ("Tyrone Tracy Jr.", "RB", "NYG"),
    ("Jaylen Warren", "RB", "PIT"),
    ("Zach Charbonnet", "RB", "SEA"),
    ("Brian Robinson Jr.", "RB", "SF"),
    ("Najee Harris", "RB", "LAC"),
    ("Cam Skattebo", "RB", "NYG"),
    ("Khalil Shakir", "WR", "BUF"),
    ("Jauan Jennings", "WR", "SF"),
    ("Keon Coleman", "WR", "BUF"),
    ("Jakobi Meyers", "WR", "LV"),
    ("Darnell Mooney", "WR", "ATL"),
    ("Cooper Kupp", "WR", "SEA"),
    ("Stefon Diggs", "WR", "NE"),
    ("Calvin Ridley", "WR", "TEN"),
    ("Michael Pittman Jr.", "WR", "IND"),
    ("Ricky Pearsall", "WR", "SF"),
    ("Josh Downs", "WR", "IND"),
    ("Wan'Dale Robinson", "WR", "NYG"),
    ("Tetairoa McMillan", "WR", "CAR"),
    ("Matthew Golden", "WR", "GB"),
    ("Travis Hunter", "WR", "JAX"),
    ("Jayden Reed", "WR", "GB"),
    ("Chris Godwin", "WR", "TB"),
    ("Adam Thielen", "WR", "MIN"),
    ("Hunter Henry", "TE", "NE"),
    ("Jake Ferguson", "TE", "DAL"),
    ("Evan Engram", "TE", "DEN"),
    ("Dallas Goedert", "TE", "PHI"),
    ("Isaiah Likely", "TE", "BAL"),
    ("Cade Otton", "TE", "TB"),
    ("Brock Bowers", "TE", "LV"),
    ("Trey McBride", "TE", "ARI"),
    ("George Kittle", "TE", "SF"),
    ("Sam LaPorta", "TE", "DET"),
    ("T.J. Hockenson", "TE", "MIN"),
    ("Travis Kelce", "TE", "KC"),
    ("Mark Andrews", "TE", "BAL"),
    ("David Njoku", "TE", "CLE"),
    ("Tucker Kraft", "TE", "GB"),
    ("Dalton Kincaid", "TE", "BUF"),
    ("Colston Loveland", "TE", "CHI"),
    ("Tyler Warren", "TE", "IND"),
    ("Brandon Aubrey", "K", "DAL"),
    ("Cameron Dicker", "K", "LAC"),
    ("Chris Boswell", "K", "PIT"),
    ("Ka'imi Fairbairn", "K", "HOU"),
    ("Jake Bates", "K", "DET"),
    ("Younghoe Koo", "K", "ATL"),
    ("Wil Lutz", "K", "DEN"),
    ("Chase McLaughlin", "K", "TB"),
    ("Jason Myers", "K", "SEA"),
    ("Evan McPherson", "K", "CIN"),
    ("Cam Little", "K", "JAX"),
    ("Matt Gay", "K", "WSH"),
    ("Eagles", "DEF", "PHI"),
    ("Ravens", "DEF", "BAL"),
    ("Broncos", "DEF", "DEN"),
    ("Vikings", "DEF", "MIN"),
    ("Texans", "DEF", "HOU"),
    ("Steelers", "DEF", "PIT"),
    ("Lions", "DEF", "DET"),
    ("Bills", "DEF", "BUF"),
    ("Packers", "DEF", "GB"),
    ("Chargers", "DEF", "LAC"),
    ("Seahawks", "DEF", "SEA"),
    ("Colts", "DEF", "IND"),
)


@dataclass(frozen=True)
class DemoPlayer:
    """One rostered player plus the script of when they score."""

    player_key: str
    name: str
    position: str
    nfl_team: str
    team_key: str
    matchup_id: str
    slot: str
    projected: float
    window: int
    moments: tuple[tuple[float, float], ...]
    """``(elapsed_seconds, points)`` pairs, ascending. Negative points are corrections."""


@dataclass(frozen=True)
class DemoTeam:
    team_key: str
    name: str
    manager: str
    matchup_id: str


@dataclass(frozen=True)
class DemoLeague:
    week: int
    teams: tuple[DemoTeam, ...]
    players: tuple[DemoPlayer, ...]
    matchups: tuple[tuple[str, str], ...] = field(default_factory=tuple)


def _weighted_value(rng: random.Random, position: str) -> float:
    table = _MOMENT_VALUES.get(position, _MOMENT_VALUES["WR"])
    values = [v for v, _ in table]
    weights = [w for _, w in table]
    return rng.choices(values, weights=weights, k=1)[0]


def _script(rng: random.Random, position: str, window: int) -> tuple[tuple[float, float], ...]:
    """Discrete scoring moments for one player, inside their game window."""
    start_frac, end_frac = _WINDOWS[window]
    start = start_frac * DEMO_CYCLE_SECONDS
    end = end_frac * DEMO_CYCLE_SECONDS

    # A handful of players have a quiet day; that is realistic and gives the
    # roster popup something other than a wall of scorers.
    count = rng.choices((0, 1, 2, 3, 4, 5), weights=(2, 4, 6, 6, 4, 2), k=1)[0]
    moments = sorted(rng.uniform(start, end) for _ in range(count))
    return tuple((round(t, 2), round(_weighted_value(rng, position), 2)) for t in moments)


def build_league(*, seed: int = 20260801, week: int = 1) -> DemoLeague:
    """Deal a deterministic league. The same seed always yields the same league."""
    rng = random.Random(seed)

    pool = list(_POOL)
    rng.shuffle(pool)
    by_position: dict[str, list[tuple[str, str, str]]] = {}
    for entry in pool:
        by_position.setdefault(entry[1], []).append(entry)

    # Kickoff window is a property of the NFL team, not the player — two
    # Chargers are in the same game. Assigning it per player puts teammates in
    # different game states, which reads as broken the moment a roster popup
    # shows one of them "Final" and the other "yet to play".
    nfl_teams = sorted({entry[2] for entry in _POOL})
    window_by_nfl_team = {team: rng.randrange(len(_WINDOWS)) for team in nfl_teams}

    teams: list[DemoTeam] = []
    players: list[DemoPlayer] = []
    matchups: list[tuple[str, str]] = []

    for index in range(_TEAM_COUNT):
        matchup_id = f"m{index // 2 + 1}"
        team_key = f"demo.l.1.t.{index + 1}"
        teams.append(DemoTeam(team_key, _TEAM_NAMES[index], _MANAGERS[index], matchup_id))
        if index % 2 == 0:
            matchups.append((team_key, f"demo.l.1.t.{index + 2}"))

        slots = list(_STARTING_SLOTS) + ["BN"] * _BENCH_SIZE
        for slot_index, slot in enumerate(slots):
            # Bench players are drawn from the deep positions, since a real
            # bench is mostly RB/WR depth rather than a spare kicker.
            if slot == "BN":
                # Draw the bench from whichever skill position has the most
                # players left. A fixed random choice exhausts a position by
                # variance long before the pool is actually empty, and a real
                # bench is RB/WR depth anyway — which is what "most remaining"
                # naturally produces, since those are the deepest positions.
                position = max(("RB", "WR", "TE"), key=lambda p: len(by_position.get(p, ())))
            else:
                position = slot
            available = by_position.get(position)
            if not available:
                raise ValueError(
                    f"demo player pool exhausted at {position} while filling team {index + 1}; "
                    "add more players to _POOL or reduce _TEAM_COUNT/_BENCH_SIZE"
                )
            name, _, nfl_team = available.pop()
            window = window_by_nfl_team[nfl_team]
            players.append(
                DemoPlayer(
                    player_key=f"demo.p.{index + 1}.{slot_index + 1}",
                    name=name,
                    position=position,
                    nfl_team=nfl_team,
                    team_key=team_key,
                    matchup_id=matchup_id,
                    slot=slot,
                    projected=round(rng.uniform(4.0, 22.0), 1),
                    window=window,
                    moments=_script(rng, position, window),
                )
            )

    return DemoLeague(week=week, teams=tuple(teams), players=tuple(players), matchups=tuple(matchups))


def points_at(player: DemoPlayer, elapsed: float) -> float:
    """Points scored so far. Sums the moments that have already happened."""
    total = sum((value for when, value in player.moments if when <= elapsed), 0.0)
    # float() guards the empty case: sum of nothing is int 0, and max(0, 0.0)
    # returns the int, so an unplayed player would carry a different type from
    # every other player on the board.
    return round(float(max(total, 0.0)), 2)


def game_state(player: DemoPlayer, elapsed: float) -> str:
    """``pre`` / ``in`` / ``post`` for the player's NFL game."""
    start_frac, end_frac = _WINDOWS[player.window]
    if elapsed < start_frac * DEMO_CYCLE_SECONDS:
        return "pre"
    if elapsed < end_frac * DEMO_CYCLE_SECONDS:
        return "in"
    return "post"


def snapshot_at(league: DemoLeague, elapsed: float) -> LeagueSnapshot:
    """The league at one instant, in the shape ``plays.diff_snapshots`` expects."""
    elapsed = max(0.0, float(elapsed))
    return LeagueSnapshot(
        week=league.week,
        taken_at=elapsed,
        players={
            p.player_key: PlayerSnapshot(
                player_key=p.player_key,
                name=p.name,
                points=points_at(p, elapsed),
                team_key=p.team_key,
                matchup_id=p.matchup_id,
                nfl_team=p.nfl_team,
                position=p.position,
                selected_position=p.slot,
            )
            for p in league.players
        },
    )


def team_totals(league: DemoLeague, elapsed: float) -> dict[str, dict[str, float]]:
    """Live and projected points per fantasy team.

    Only starters count toward the score, which is the whole reason the roster
    popup distinguishes them. ``projected`` blends what has already been scored
    with the remaining projection, so it converges on the live total as the day
    ends — the same behaviour Yahoo's own projected total has.
    """
    totals: dict[str, dict[str, float]] = {t.team_key: {"points": 0.0, "projected": 0.0} for t in league.teams}
    for player in league.players:
        if player.slot == "BN":
            continue
        scored = points_at(player, elapsed)
        entry = totals[player.team_key]
        entry["points"] += scored
        entry["projected"] += scored if game_state(player, elapsed) == "post" else max(scored, player.projected)
    return {k: {"points": round(v["points"], 2), "projected": round(v["projected"], 2)} for k, v in totals.items()}


def elapsed_for(now: float, *, cycle: float = DEMO_CYCLE_SECONDS) -> float:
    """Map a wall-clock timestamp into the demo's looping game day.

    Takes the timestamp as an argument rather than reading a clock, so callers
    stay testable and this module stays pure.
    """
    if cycle <= 0:
        return 0.0
    return float(now) % cycle
