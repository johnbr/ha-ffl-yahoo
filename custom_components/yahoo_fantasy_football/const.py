"""Constants for the Yahoo Fantasy Football integration."""

from __future__ import annotations

DOMAIN = "yahoo_fantasy_football"
PLATFORMS = ["sensor"]

# Config entry keys.
CONF_LEAGUE_KEY = "league_key"
CONF_NAME = "name"

DEFAULT_NAME = "Yahoo Fantasy Football"

# The bundled card ships inside this package directory and is copied to
# ``<config>/www/community/<DOMAIN>/`` at setup so HACS's ``/hacsfiles/``
# mapping serves it. See ``__init__.py``.
CARD_FILENAME = "yahoo-fantasy-football-cards.js"

# Poll cadences, reassigned on every refresh once the coordinator lands.
# Yahoo's own live scoring lags the play by roughly 30-60s, so polling faster
# than LIVE buys nothing but rate-limit risk. Yahoo does not document its
# limits and throttles per app id, so these stay deliberately conservative.
SCAN_INTERVAL_LIVE_SECONDS = 45
SCAN_INTERVAL_NEAR_GAME_SECONDS = 300
SCAN_INTERVAL_IDLE_SECONDS = 1800

# Bus event fired for each detected scoring play.
EVENT_SCORING_PLAY = f"{DOMAIN}_scoring_play"
