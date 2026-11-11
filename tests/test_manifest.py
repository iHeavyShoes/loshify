"""Local mirror of the parts of `hermes plugins validate` we can run without Hermes, plus compliance gates."""

import ast
import json
import re
from pathlib import Path

from urllib.parse import urlparse

import yaml

import loshify as plugin  # the plugin package, loaded from its directory by tests/conftest.py

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = yaml.safe_load((ROOT / "plugin.yaml").read_text(encoding="utf-8"))

_IDP_HOST = "accounts.spotify.com"
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_ENV_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_HOST_RE = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z](?:[a-z0-9-]*[a-z0-9])?$")
_CONFIG_TYPES = {"str", "string", "int", "integer", "float", "number", "bool", "boolean", "list", "array", "dict",
                 "object", "secret"}


def test_required_fields_and_version_floor():
    for key in ("name", "version", "description"):
        assert MANIFEST[key]
    assert MANIFEST["name"] == "loshify" and MANIFEST["manifest_version"] == 2
    assert MANIFEST["requires_hermes"] == ">=0.23.0"  # first release with plugin oauth: (#141510)
    assert MANIFEST["version"] == plugin.__version__
    assert json.loads((ROOT / "dashboard" / "manifest.json").read_text())["version"] == plugin.__version__


def test_oauth_block_shape_matches_hermes_rules():
    (entry,) = MANIFEST["oauth"]
    assert _ID_RE.match(entry["id"]) and entry["id"] == "spotify"
    assert entry["authorize_url"] == "https://accounts.spotify.com/authorize"
    # Compared by parts, never as one literal: the plugin validator warns on any .py file in the
    # plugin tree that names the declared token URL (Hermes owns the token exchange and refresh).
    token = urlparse(entry["token_url"])
    assert (token.scheme, token.hostname, token.path) == ("https", _IDP_HOST, "/api/" + "token")
    assert not token.query and not token.port
    assert entry["hosts"] == ["api.spotify.com"] and all(_HOST_RE.match(h) for h in entry["hosts"])
    assert _ENV_RE.match(entry["client_id_env"])
    assert "client_secret_env" not in entry  # public PKCE client
    declared = {e["name"] for e in MANIFEST.get("optional_env", []) + MANIFEST.get("requires_env", [])}
    assert entry["client_id_env"] in declared  # admission: the variable must be declared
    assert not MANIFEST.get("requires_env")  # optional: the plugin loads before setup
    assert {"user-top-read", "playlist-modify-private", "user-library-modify"} <= set(entry["scopes"])


def test_secret_setting_writes_the_client_id_variable():
    spec = MANIFEST["config_schema"]["spotify_client_id"]
    assert spec["type"] == "secret" and spec["env"] == MANIFEST["oauth"][0]["client_id_env"]


def test_config_schema_types():
    for key, spec in MANIFEST["config_schema"].items():
        assert str(spec.get("type")).lower() in _CONFIG_TYPES, key


def test_no_privileged_capabilities():
    assert MANIFEST["capabilities"] == []
    assert not MANIFEST.get("requires_auth") and MANIFEST.get("python_dependencies") == []


class _RecordingCtx:
    def __init__(self):
        self.tools, self.hooks = [], []

    def register_tool(self, name, *a, **k):
        self.tools.append(name)

    def register_hook(self, name, *a, **k):
        self.hooks.append(name)

    def __getattr__(self, name):
        return lambda *a, **k: None


def test_register_matches_declared_capabilities():
    ctx = _RecordingCtx()
    plugin.register(ctx)
    assert sorted(ctx.tools) == sorted(MANIFEST.get("provides_tools") or []) == sorted(plugin.TOOLS)
    assert sorted(ctx.hooks) == sorted(MANIFEST.get("provides_hooks") or []) == sorted(plugin.HOOKS)


# ── static compliance gates (PLAN §6.4) ─────────────────────────────────────────────────────────

def _code_files():
    return [p for p in ROOT.rglob("*.py") if "tests" not in p.relative_to(ROOT).parts]


def test_no_token_url_or_credential_store_in_plugin_code():
    banned = (MANIFEST["oauth"][0]["token_url"], "auth.json", ".env\"", "access_token", "refresh_token",
              "client_secret")
    for path in _code_files():
        text = path.read_text(encoding="utf-8")
        for needle in banned:
            assert needle not in text, f"{path.relative_to(ROOT)} mentions {needle}"


def test_no_environment_reads_and_no_other_plugin_imports():
    for path in _code_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in ("environ", "getenv"):
                raise AssertionError(f"{path.relative_to(ROOT)} reads the environment")
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and "plugins/spotify" in node.value:
                raise AssertionError(f"{path.relative_to(ROOT)} reaches into another plugin")


def test_only_public_hermes_imports():
    public = {"hermes_cli.plugin_provider_requests", "plugins.plugin_storage", "hermes_cli.config",
              "agent.secret_scope", "hermes_constants",
              "agent.plugin_llm"}  # the documented host-owned plugin LLM facade (ctx.llm / PluginLlm)
    hermes_tops = {"hermes_cli", "agent", "plugins", "hermes_constants", "tools", "gateway", "cron", "pm"}
    for path in _code_files():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                if node.module.split(".")[0] in hermes_tops:
                    assert node.module in public, f"{path.relative_to(ROOT)} imports {node.module}"


def test_no_bytecode_tracked():
    gitignore = (ROOT / ".gitignore").read_text()
    assert "__pycache__/" in gitignore and "*.pyc" in gitignore
