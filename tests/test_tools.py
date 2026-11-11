"""Tool contract tests: schema ↔ handler, register() wiring, every tool end to end on fakes, the hook."""

import json
import time

import pytest

import loshify as plugin
from loshify.tools import handlers as h
from loshify.tools import schemas
from core import spotify as sp
from core import store as st
from fakes import FakeSpotify, ProviderNotSignedIn, cat_track, reply

NOW = 1_800_000_000
CATALOG = [cat_track(f"Song {i}", f"Artist {i}", f"t{i:02d}") for i in range(30)]


class Env:
    def __init__(self, tmp_path, fake=None, settings=None):
        self.path = tmp_path / "data.db"
        self.fake = fake or FakeSpotify(CATALOG)
        self.settings = dict(settings or {})
        self.rt = h.Runtime(store_factory=lambda: st.open_store(self.path),
                            client_factory=lambda: sp.SpotifyClient(self.fake, sleep=lambda s: None),
                            get_setting=lambda k, d: self.settings.get(k, d), now=lambda: NOW)

    def call(self, name, args=None):
        raw = h.TOOL_HANDLERS[name](self.rt, args or {})
        assert isinstance(raw, str)
        return json.loads(raw)

    def db(self):
        return st.open_store(self.path)


def add_history(fake):
    fake.on("GET", "/v1/me/top/artists", reply(200, {"items": [{"name": "Artist 0"}]}))
    fake.on("GET", "/v1/me/top/tracks", reply(200, {"items": [CATALOG[0]]}))
    fake.on("GET", "/v1/me/tracks", reply(200, {"items": [{"added_at": f"20{15 + i}-01-01T00:00:00Z",
                                                          "item": CATALOG[i]} for i in range(6)], "next": None}))
    fake.on("GET", "/v1/me/player/recently-played", reply(200, {"items": []}))


# ── schemas ↔ manifest ↔ handlers ──────────────────────────────────────────────────────────────

def test_every_declared_tool_has_schema_and_handler():
    names = [s["name"] for s in schemas.ALL]
    assert names == list(plugin.TOOLS) == list(h.TOOL_HANDLERS)
    for s in schemas.ALL:
        assert s["parameters"]["type"] == "object" and len(s["description"]) < 1024
        for req in s["parameters"].get("required", []):
            assert req in s["parameters"]["properties"]


class Ctx:
    def __init__(self, config=None):
        self.tools, self.hooks, self.skills, self.config = {}, {}, [], dict(config or {})

    def register_tool(self, name, toolset, schema, handler, **kw):
        assert toolset == "loshify" and schema["name"] == name and kw.get("emoji")
        self.tools[name] = handler

    def register_hook(self, name, cb, **kw):
        self.hooks[name] = cb

    def register_skill(self, name, path, description=""):
        assert path.is_file() and ":" not in name
        self.skills.append((name, path))

    def get_config(self, key, default=None):
        return self.config.get(key, default)


def test_register_wires_tools_hook_skill_and_settings(monkeypatch, tmp_path):
    env = Env(tmp_path, settings={"inject_taste": False})
    ctx = Ctx(config={"playlist_size": 7})
    real_runtime = h.Runtime

    def runtime(**kw):  # the real get_setting (ctx.get_config), fake store + Spotify
        return real_runtime(store_factory=env.rt.store_factory, client_factory=env.rt.client_factory, now=env.rt.now,
                            **kw)
    monkeypatch.setattr(h, "Runtime", runtime)
    plugin.register(ctx)
    assert sorted(ctx.tools) == sorted(plugin.TOOLS) and list(ctx.hooks) == ["pre_llm_call"]
    assert ctx.skills and ctx.skills[0][0] == "loshify-curator"
    text = ctx.skills[0][1].read_text(encoding="utf-8")
    assert text.startswith("---\nname: loshify-curator\n") and "loshify_curate" in text
    # handler signature: (args, **context kwargs) → JSON string
    out = json.loads(ctx.tools["loshify_taste"]({"action": "stats"}, task_id="t", session_id="s"))
    assert out["ok"] and out["onboarded"] is False
    # settings flow through ctx.get_config
    add_history(env.fake)
    json.loads(ctx.tools["loshify_setup"]({}))
    made = json.loads(ctx.tools["loshify_curate"]({"template": "time_capsule"}))
    # pool: 6 saved, minus Song 0 (also a short-term top track = heard lately) → 5 < size 7
    assert made["ok"] and len(made["tracks"]) == 5 and made["short"] is True
    db = env.db()
    try:
        assert db.playlists()[0]["params"]["size"] == 7
    finally:
        db.close()


def test_register_imports_no_hermes_and_needs_only_tool_and_hook(monkeypatch):
    """Like the validator's probe: a context with only register_tool / register_hook must work."""
    class Bare:
        def __init__(self):
            self.n = 0

        def register_tool(self, *a, **k):
            self.n += 1

        def register_hook(self, *a, **k):
            self.n += 1
    bare = Bare()
    plugin.register(bare)
    assert bare.n == len(plugin.TOOLS) + len(plugin.HOOKS) == 8


# ── tools end to end ───────────────────────────────────────────────────────────────────────────

def test_setup_not_ready_returns_next_step(tmp_path):
    env = Env(tmp_path)
    env.fake.routes[("GET", "/v1/me")] = [ProviderNotSignedIn("no")]
    out = env.call("loshify_setup")
    assert out["ok"] and out["ready"] is False and out["step"] == "signin" and "hermes auth add" in out["next_step"]
    # the chat walks the same steps as the Desktop wizard, starting where this user is
    assert out["guide"]["start_at"] == "connect"
    assert [s["step"] for s in out["guide"]["steps"]] == ["dashboard", "app", "users", "client_id", "connect", "check"]
    assert out["redirect_uri"] == "http://127.0.0.1:43828/callback"
    assert out["signin_command"] == "hermes auth add loshify:spotify"
    assert "one at a time" in out["next"] and "Never ask for the Client ID in chat" in out["next"]


def test_setup_refused_names_the_fix_first(tmp_path):
    env = Env(tmp_path)
    env.fake.routes[("GET", "/v1/me")] = [reply(403, {"error": {"message": "user not registered"}})]
    out = env.call("loshify_setup")
    assert out["ok"] and out["problem"] == "not_listed" and "User Management" in out["fix"]
    assert out["guide"]["start_at"] == "check"


def test_full_loop_setup_portrait_capsule_dig_react_reactions_refresh(tmp_path):
    env = Env(tmp_path)
    add_history(env.fake)
    s = env.call("loshify_setup")
    assert s["ready"] and s["onboarded"] and "portrait_facts" in s and s["spotify_calls"] <= 20
    assert env.call("loshify_setup")["taste"]["onboarded"]  # second call: no re-seed
    assert env.call("loshify_taste", {"action": "set_portrait", "text": "A desert-blues heart."})["ok"]

    cap = env.call("loshify_curate", {"template": "time_capsule", "size": 5})
    assert cap["ok"] and cap["playlist"]["action"] == "created" and len(cap["tracks"]) == 5

    dig = env.call("loshify_curate", {"template": "deep_dig", "size": 5, "candidates": [
        {"title": f"Song {i}", "artist": f"Artist {i}", "reason": f"because {i}"} for i in range(10, 22)]})
    assert dig["ok"] and len(dig["tracks"]) == 5 and dig["tracks"][0]["reason"] == "because 10"
    pid = dig["playlist"]["id"]

    env.fake.play(CATALOG[10], pid)
    down = env.call("loshify_react", {"reaction": "down", "note": "too sleepy"})
    assert down["ok"] and down["actions"]["removal"]["status"] == "removed"
    assert CATALOG[10]["uri"] not in env.fake.playlists[pid]
    env.fake.play(CATALOG[11], pid)
    assert env.call("loshify_react", {"reaction": "love"})["reaction"] == "love"

    seen = env.call("loshify_reactions")
    assert seen["counts"] == {"love": 1, "okay": 0, "down": 1}
    assert next(r for r in seen["reactions"] if r["reaction"] == "down")["note"] == "too sleepy"
    assert env.call("loshify_reactions")["reactions"] == []  # marked seen

    taste = env.call("loshify_taste")["taste"]
    assert taste["portrait"] == "A desert-blues heart." and taste["recent_loves"][0]["title"] == "Song 11"

    again = env.call("loshify_curate", {"template": "deep_dig", "mode": "refresh", "size": 5, "candidates": [
        {"title": f"Song {i}", "artist": f"Artist {i}"} for i in range(10, 30)]})
    assert again["playlist"]["id"] == pid and again["playlist"]["action"] == "refreshed"
    assert CATALOG[10]["uri"] not in env.fake.playlists[pid]  # 👎 never served again

    undo = env.call("loshify_react", {"undo_id": down["id"]})
    assert undo["ok"] and undo["restored"]["status"] == "restored"


def test_curate_before_onboarding_points_to_setup(tmp_path):
    out = Env(tmp_path).call("loshify_curate", {"template": "time_capsule"})
    assert out["ok"] is False and out["setup"] == "onboard"


def test_curate_play_reports_missing_device(tmp_path):
    env = Env(tmp_path)
    add_history(env.fake)
    env.call("loshify_setup")
    env.fake.routes[("PUT", "/v1/me/player/play")] = [reply(404, {"error": {"message": "no device"}})]
    out = env.call("loshify_curate", {"template": "time_capsule", "play": True})
    assert out["ok"] and out["playing"] is False and "device" in out["play_error"]


def test_react_with_nothing_playing_and_bad_input(tmp_path):
    env = Env(tmp_path)
    assert env.call("loshify_react", {"reaction": "love"})["ok"] is False
    assert "reaction must be" in env.call("loshify_react", {"reaction": "meh"})["error"]
    assert env.call("loshify_taste", {"action": "nope"})["ok"] is False
    assert env.call("loshify_reactions", {"since": "x"})["ok"] is False
    assert env.call("loshify_curate", {"template": "deep_dig", "candidates": "x"})["ok"] is False


def test_taste_corrections(tmp_path):
    env = Env(tmp_path)
    db = env.db()
    db.add_reaction(CATALOG[1]["uri"], "down", artists=["Artist 1"], ts=NOW - 5)
    db.add_blocked(CATALOG[2]["uri"], "Song 2", "Artist 2", NOW - 5)
    db.put_artist("artist 3", "Artist 3", 1, 0, 0, 0, NOW)
    db.close()
    assert env.call("loshify_taste", {"action": "unblock_track", "track_uri": CATALOG[1]["uri"]})["unblocked"]
    assert env.call("loshify_taste", {"action": "unblock_track", "track_uri": CATALOG[2]["uri"]})["unblocked"]
    assert env.call("loshify_taste", {"action": "forget_artist", "artist": "artist 3"})["forgotten"]
    db = env.db()
    assert db.blocked_uris() == set() and db.artist_rows() == []
    db.close()


def test_spotify_signin_error_inside_a_tool_is_a_setup_hint(tmp_path):
    env = Env(tmp_path)
    add_history(env.fake)
    env.call("loshify_setup")
    env.fake.routes[("GET", "/v1/me/tracks")] = [ProviderNotSignedIn("revoked")]
    out = env.call("loshify_curate", {"template": "time_capsule"})
    assert out == {**out, "ok": False, "setup": "signin"} and "hermes auth add" in out["next_step"]


def test_unexpected_error_is_contained(tmp_path):
    env = Env(tmp_path)
    env.rt.store_factory = lambda: (_ for _ in ()).throw(RuntimeError("disk on fire /secret/path"))
    out = env.call("loshify_taste")
    assert out == {"ok": False, "error": "Loshify failed unexpectedly (RuntimeError)."}


# ── hook ───────────────────────────────────────────────────────────────────────────────────────

def onboarded(env):
    db = env.db()
    db.put_artist("tinariwen", "Tinariwen", 3, 0, 0, 5, NOW)
    db.set_meta("onboarded_at", NOW - 86400)
    db.close()


def test_hook_injects_brief_only_on_music_turns(tmp_path):
    env = Env(tmp_path)
    assert h.taste_hook(env.rt, "make me a playlist") is None  # not onboarded
    onboarded(env)
    out = h.taste_hook(env.rt, "תכין לי פלייליסט", session_id="s", conversation_history=[])
    assert out and "Tinariwen" in out["context"] and len(out["context"]) <= 700
    assert h.taste_hook(env.rt, "what's the weather tomorrow?") is None


def test_hook_injects_on_unseen_reactions_and_respects_setting(tmp_path):
    env = Env(tmp_path)
    onboarded(env)
    db = env.db()
    db.add_reaction(CATALOG[0]["uri"], "love", artists=["Artist 0"], ts=NOW)
    db.close()
    assert "new reaction" in h.taste_hook(env.rt, "hi")["context"]
    env.settings["inject_taste"] = "false"
    assert h.taste_hook(env.rt, "play some music") is None


def test_hook_never_raises_and_is_fast(tmp_path):
    env = Env(tmp_path)
    onboarded(env)
    start = time.perf_counter()
    for _ in range(50):
        h.taste_hook(env.rt, "the weather")  # the common case: no music, no unseen reactions
    assert (time.perf_counter() - start) / 50 < 0.005
    env.rt.store_factory = lambda: (_ for _ in ()).throw(OSError("locked"))
    assert h.taste_hook(env.rt, "music") is None
