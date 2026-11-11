"""Static compliance gates (PLAN §6.4), run in pytest locally and in CI.

They read source as text; nothing is imported or executed.
"""

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", "node_modules", "tests"}


def shipped(*suffixes):
    for path in sorted(ROOT.rglob("*")):
        if path.is_file() and path.suffix in suffixes and not (set(path.relative_to(ROOT).parts) & SKIP_DIRS):
            yield path


PY = list(shipped(".py"))
JS = list(shipped(".js", ".mjs"))
CODE = PY + JS


def _hits(files, pattern):
    rx = re.compile(pattern)
    return [f"{p.relative_to(ROOT)}:{n}" for p in files
            for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1) if rx.search(line)]


def test_there_is_code_to_check():
    names = {p.relative_to(ROOT).as_posix() for p in CODE}
    assert {"__init__.py", "dashboard/plugin_api.py", "desktop/plugin.js", "core/spotify.py"} <= names


@pytest.mark.parametrize("label,pattern", [
    ("os.environ / getenv reads", r"os\.environ|os\.getenv|getenv\("),
    ("credential files", r"auth\.json|['\"/]\.env\b|dotenv"),
    ("token endpoint in code", r"accounts\.spotify\.com/api/token"),
    ("another plugin's code", r"plugins[./]spotify|spotify_client"),
    ("bearer handling", r"[Bb]earer |access_token|refresh_token"),
    ("shell / subprocess", r"\bsubprocess\b|os\.system|child_process"),
])
def test_no_forbidden_patterns_in_code(label, pattern):
    assert _hits(CODE, pattern) == [], label


def test_desktop_imports_only_the_sdk_and_jsx_runtime():
    for path in JS:
        src = path.read_text(encoding="utf-8")
        imports = set(re.findall(r"^import[^'\"]*['\"]([^'\"]+)['\"]", src, flags=re.M))
        assert imports <= {"@hermes/plugin-sdk", "react/jsx-runtime"}, (path, imports)
        assert not re.search(r"\bimport\(|\brequire\(|\beval\(|new Function\(", src), path


def test_no_app_dom_selectors():
    assert _hits(JS, r"data-(slot|testid|tour|sidebar)") == []
    assert _hits(JS, r"querySelector|MutationObserver|hermesDesktop") == []


def test_python_importlib_loads_only_this_plugins_own_files():
    # importlib is used to load ``core/`` next to the dashboard router; every spec path must be ours.
    for hit in _hits(PY, r"spec_from_file_location\("):
        path, line = hit.rsplit(":", 1)
        lines = (ROOT / path).read_text(encoding="utf-8").splitlines()
        call = " ".join(lines[int(line) - 1:int(line) + 2])  # the call may wrap onto the next lines
        assert "PLUGIN_DIR" in call, hit


def test_no_bytecode_or_binaries_tracked():
    try:
        out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    tracked = out.split()
    assert not [f for f in tracked if f.endswith((".pyc", ".pyo", ".so", ".dll", ".exe", ".wasm"))]
    for name in tracked:
        head = (ROOT / name).read_bytes()[:4] if (ROOT / name).is_file() else b""
        assert head not in (b"\x7fELF", b"\x00asm") and not head.startswith(b"MZ"), name


def test_readme_carries_the_rule_13_disclosures():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for needle in ("own Spotify developer app", "Premium", "stores its data on your computer", "api.spotify.com", "model provider",
                   "plugin-data/loshify/data.db", "no telemetry", "hermes auth add loshify:spotify",
                   "http://127.0.0.1:43828/callback", "only from playlists Loshify created"):
        assert needle.lower() in readme.lower(), needle
    assert "http://127.0.0.1/callback" not in readme  # the port-less URI Spotify refuses
    assert (ROOT / "LICENSE").read_text(encoding="utf-8").startswith("MIT License")


QUICK_START_MUST = ("developer.spotify.com/dashboard", "Loshify", "http://127.0.0.1:43828/callback", "Web API",
                    "this computer", "User Management", "Client ID", "hermes auth add loshify:spotify")


def test_readme_quick_start_mirrors_the_wizard_steps():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    quick = readme.split("## Quick start", 1)[1].split("\n## ", 1)[0]
    for needle in QUICK_START_MUST:
        assert needle in quick, needle
    steps = [line for line in quick.splitlines() if line[:3] in {f"{n}. " for n in range(1, 8)}]
    assert len(steps) == 9  # install, Desktop switch, then the seven wizard steps
    assert "hermes plugins install iHeavyShoes/loshify --enable" in quick
    assert "hermes plugins install loshify" in quick
    assert "--skill loshify:loshify-curator" in quick


def test_catalog_entry_mirrors_the_steps_and_matches_the_manifest():
    import yaml
    entry = yaml.safe_load((ROOT / "catalog" / "plugin-catalog" / "loshify.yaml").read_text(encoding="utf-8"))
    manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text(encoding="utf-8"))
    assert entry["name"] == manifest["name"] and entry["version"] == manifest["version"]
    assert entry["requires_hermes"] == manifest["requires_hermes"]
    assert entry["capabilities"]["provides_tools"] == manifest["provides_tools"]
    assert entry["capabilities"]["provides_hooks"] == manifest["provides_hooks"]
    for needle in QUICK_START_MUST:
        assert needle in entry["description"], needle


def test_chat_skill_walks_the_same_steps():
    skill = (ROOT / "skills" / "loshify-curator" / "SKILL.md").read_text(encoding="utf-8")
    for needle in ("guide.steps", "start_at", "one at a time", "fix", "Never ask for", "hermes auth add loshify:spotify"):
        assert needle in skill, needle
