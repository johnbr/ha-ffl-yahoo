"""Constants for the Yahoo Fantasy Football integration."""

from __future__ import annotations

DOMAIN = "yahoo_fantasy_football"
PLATFORMS = ["sensor"]

# Config entry keys.
CONF_LEAGUE_KEY = "league_key"
CONF_NAME = "name"

# Public-web source. A Yahoo *API* league key looks like ``461.l.476807``; the
# public web pages are addressed by the bare numeric league id plus, for an
# archived season, the year. Kept separate from CONF_LEAGUE_KEY so both sources
# can coexist on one entry once OAuth lands.
CONF_LEAGUE_ID = "league_id"
CONF_SEASON = "season"
CONF_TEAM_ID = "team_id"
"""Which team is 'mine' — drives the my-matchup card. Optional."""

DEFAULT_NAME = "Yahoo Fantasy Football"

# The bundled card ships inside this package directory and is copied to
# ``<config>/www/community/<DOMAIN>/`` at setup so HACS's ``/hacsfiles/``
# mapping serves it. See ``__init__.py``.
CARD_FILENAME = "yahoo-fantasy-football-cards.js"

# Poll cadences, reassigned on every refresh once the coordinator lands.
# Yahoo's own live scoring lags the play by roughly 30-60s; that is a floor
# nothing here can beat, but the poll interval adds to it, so it is worth
# keeping small. Yahoo does not document its rate limits and throttles per app
# id, so these stay conservative — what makes this affordable is that a live
# poll costs ~8 KB, not ~186 KB: the 178 KB league seed is cached behind its
# own TTL (see ``redzone_client.SEED_TTL_SECONDS``).
#
# 10s is measured, not guessed. Sampling the relay during a live slate
# (2026-09-13, eight concurrent games) showed the games feed changing every
# ~7s on average — 17 data changes in 119s, with ``source_sequence_number``
# advancing 18 over the same window, two independent signals agreeing. So the
# source is the faster side and polling is what adds the delay: at 20s the
# average wait was ~10s, at 10s it is ~5s.
#
# Going below ~7s would buy little and cost proportionally: the feed simply
# has nothing new to say more often than that.
SCAN_INTERVAL_LIVE_SECONDS = 10
SCAN_INTERVAL_NEAR_GAME_SECONDS = 300
SCAN_INTERVAL_IDLE_SECONDS = 1800

# Bus event fired for each detected scoring play.
EVENT_SCORING_PLAY = f"{DOMAIN}_scoring_play"
