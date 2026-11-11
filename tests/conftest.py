"""Load the plugin the way Hermes does: as a package (``loshify``) from its directory.

The Day-1 tests import ``core.*`` as top-level modules. Those names are aliased to the package's own
modules (``sys.modules["core.store"] is loshify.core.store``) so there is one copy of every class,
whichever name a test uses. Without that, an exception raised by ``core.spotify`` would not be caught
as ``loshify.core.spotify.SpotifyError`` by the tool handlers.
"""

import importlib
import importlib.util
import sys
from pathlib import Path

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
