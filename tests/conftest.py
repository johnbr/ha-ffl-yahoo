"""Import the integration without Home Assistant installed.

CI installs pytest and ruff, nothing else — no Home Assistant, no aiohttp. The
modules worth unit-testing (``espn``, ``plays``, ``demo``) are deliberately pure
and import none of it, but they live inside a package whose ``__init__`` does,
and Python runs that ``__init__`` before any submodule.

So the minimum surface ``__init__.py`` touches at import time is stubbed into
``sys.modules`` first. This is only sufficient for importing pure helpers —
anything exercising the coordinator, entity or config-flow machinery needs a
real Home Assistant plus ``pytest-homeassistant-custom-component``, and is out
of scope here.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _module(name: str, **attrs: object) -> ModuleType:
    """Create a stub module, wiring it to its parent package."""
    mod = ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    sys.modules[name] = mod
    if "." in name:
        parent_name, _, child = name.rpartition(".")
        parent = sys.modules.get(parent_name)
        if parent is not None:
            setattr(parent, child, mod)
    return mod


def _install_homeassistant_stubs() -> None:
    if "homeassistant" in sys.modules:
        return

    _module("homeassistant")
    _module("homeassistant.components")
    _module("homeassistant.components.http", StaticPathConfig=type("StaticPathConfig", (), {}))
    _module("homeassistant.config_entries", ConfigEntry=type("ConfigEntry", (), {}))
    _module(
        "homeassistant.const",
        EVENT_HOMEASSISTANT_STARTED="homeassistant_started",
        MATCH_ALL="*",
        Platform=SimpleNamespace(SENSOR="sensor"),
    )
    _module("homeassistant.core", HomeAssistant=type("HomeAssistant", (), {}), callback=lambda f: f)
    _module("homeassistant.helpers")
    # ``cv.config_entry_only_config_schema`` is CALLED at module scope in
    # __init__.py, so the stub has to be callable and return something.
    _module(
        "homeassistant.helpers.config_validation",
        config_entry_only_config_schema=lambda domain: {},
    )
    _module("homeassistant.loader", async_get_integration=None)


_install_homeassistant_stubs()

# ``custom_components`` on the path so the integration imports as a real package
# and its relative imports (``from .plays import ...``) resolve.
sys.path.insert(0, str(REPO_ROOT / "custom_components"))
