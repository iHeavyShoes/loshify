"""Load the plugin the way Hermes does: as a package (``loshify``) from its directory.

The Day-1 tests import ``core.*`` as top-level modules. Those names are aliased to the package's own
modules (``sys.modules["core.store"] is loshify.core.store``) so there is one copy of every class,
whichever name a test uses. Without that, an exception raised by ``core.spotify`` would not be caught
as ``loshify.core.spotify.SpotifyError`` by the tool handlers.
"""

import importlib
import importlib.util
import sys
import types
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
PACKAGE = "loshify"

if str(PLUGIN_DIR / "tests") not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR / "tests"))

if PACKAGE not in sys.modules:
    spec = importlib.util.spec_from_file_location(PACKAGE, PLUGIN_DIR / "__init__.py",
                                                  submodule_search_locations=[str(PLUGIN_DIR)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE] = module
    spec.loader.exec_module(module)

sys.modules["core"] = importlib.import_module(f"{PACKAGE}.core")
for _path in sorted((PLUGIN_DIR / "core").glob("*.py")):
    if _path.stem != "__init__":
        sys.modules[f"core.{_path.stem}"] = importlib.import_module(f"{PACKAGE}.core.{_path.stem}")


@pytest.fixture(autouse=True)
def _hermetic_client_id(monkeypatch):
    """Client ID present, whatever interpreter runs the suite.

    ``core.setup.client_id_present`` (the default of ``Runtime.has_client_id``, ``setup.probe`` and the
    dashboard's ``client_id_present``) asks Hermes's ``agent.secret_scope``. Inside a Hermes venv that
    would read the real profile, so every test gets a stub reader instead. Tests about a missing or
    unknown Client ID pass ``has_client_id`` / patch the seam / replace these modules themselves.
    """
    scope = types.ModuleType("agent.secret_scope")
    scope.get_secret = lambda name, default=None: (
        "0123456789abcdef0123456789abcdef" if name == "LOSHIFY_SPOTIFY_CLIENT_ID" else default)
    try:  # inside Hermes keep the real ``agent`` package (plugin_llm, …); only the reader is replaced
        pkg = importlib.import_module("agent")
    except Exception:
        pkg = types.ModuleType("agent")
        pkg.__path__ = []
        monkeypatch.setitem(sys.modules, "agent", pkg)
    monkeypatch.setattr(pkg, "secret_scope", scope, raising=False)
    monkeypatch.setitem(sys.modules, "agent.secret_scope", scope)
