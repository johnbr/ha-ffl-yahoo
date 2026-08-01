"""Repository-shape tests.

These run without Home Assistant installed, so they cover the metadata that
HACS and hassfest validate rather than integration behaviour. The version-drift
check is the load-bearing one: release-please bumps ``manifest.json`` and
``.release-please-manifest.json`` together, and a hand edit to either one
silently breaks the card's ``?v=`` cache-buster.
"""

from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOMAIN = "yahoo_fantasy_football"
COMPONENT_DIR = REPO_ROOT / "custom_components" / DOMAIN

# Keys HACS requires in a custom integration manifest.
REQUIRED_MANIFEST_KEYS = (
    "domain",
    "name",
    "codeowners",
    "documentation",
    "issue_tracker",
    "version",
)


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_single_integration_in_repo() -> None:
    """HACS allows exactly one integration per repository."""
    integrations = sorted(p.name for p in (REPO_ROOT / "custom_components").iterdir() if p.is_dir())
    assert integrations == [DOMAIN]


def test_manifest_has_required_keys() -> None:
    manifest = _load(COMPONENT_DIR / "manifest.json")
    missing = [key for key in REQUIRED_MANIFEST_KEYS if not manifest.get(key)]
    assert not missing, f"manifest.json missing: {missing}"


def test_manifest_domain_matches_directory() -> None:
    assert _load(COMPONENT_DIR / "manifest.json")["domain"] == COMPONENT_DIR.name


def test_manifest_version_matches_release_please() -> None:
    manifest_version = _load(COMPONENT_DIR / "manifest.json")["version"]
    tracked_version = _load(REPO_ROOT / ".release-please-manifest.json")["."]
    assert manifest_version == tracked_version


def test_card_version_matches_manifest() -> None:
    """The card's banner/cache-buster version must track the manifest."""
    manifest_version = _load(COMPONENT_DIR / "manifest.json")["version"]
    card = (COMPONENT_DIR / "yahoo-fantasy-football-cards.js").read_text(encoding="utf-8")
    assert f'"{manifest_version}"; // x-release-please-version' in card


def test_hacs_json_has_name() -> None:
    assert _load(REPO_ROOT / "hacs.json").get("name")


def test_brand_icon_present() -> None:
    """HACS validates a brand icon for integrations."""
    assert (COMPONENT_DIR / "brand" / "icon.png").is_file()


def test_config_flow_declared_and_translated() -> None:
    """``config_flow: true`` requires a flow module and matching strings."""
    manifest = _load(COMPONENT_DIR / "manifest.json")
    assert manifest.get("config_flow") is True
    assert (COMPONENT_DIR / "config_flow.py").is_file()

    strings = _load(COMPONENT_DIR / "strings.json")
    assert "user" in strings["config"]["step"]
    # translations/en.json must stay in sync with strings.json.
    assert _load(COMPONENT_DIR / "translations" / "en.json") == strings
