"""Desktop backend (dashboard/plugin_api.py): routes over the shared core, offline against FakeSpotify."""

import importlib.util
import sys
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from fakes import FakeSpotify, cat_track, reply  # noqa: E402

PLUGIN_DIR = Path(__file__).resolve().parents[1]
T1 = cat_track("Track One", "Band", "d01")
T2 = cat_track("Track Two", "Band", "d02")
T3 = cat_track("Track Three", "Other", "d03")


def _load_api():
    """Load plugin_api.py standalone, exactly as Hermes mounts it (no package context)."""
    name = "_loshify_plugin_api_under_test"
    spec = importlib.util.spec_from_file_location(name, PLUGIN_DIR / "dashboard" / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


api = _load_api()
sp = api._core("spotify")
st = api._core("store")


class _KeptStore:
    """One in-memory store across requests (the route closes it after each call)."""

    def __init__(self, store):
        self._s = store

    def __getattr__(self, item):
        return getattr(self._s, item)

    def close(self):
        pass


@pytest.fixture
def env(monkeypatch):
    fake = FakeSpotify([T1, T2, T3])
    db = st.open_store(":memory:")
    settings = {}
    monkeypatch.setattr(api, "client_factory", lambda: sp.SpotifyClient(fake, sleep=lambda s: None))
    monkeypatch.setattr(api, "store_factory", lambda: _KeptStore(db))
    monkeypatch.setattr(api, "raw_settings", lambda: settings)
    monkeypatch.setattr(api, "_bind_profile_scope", lambda: None)
    api.forget_snapshots()
    app = FastAPI()
    app.include_router(api.router, prefix="/api/plugins/loshify")
    yield TestClient(app), fake, db, settings
    db.close()


def managed(fake, db, uris, name="Loshify · Deep Dig"):
    c = sp.SpotifyClient(fake, sleep=lambda s: None)
    pid = c.create_playlist(name)["id"]
    c.replace_items(pid, uris)
    db.add_playlist(pid, name, "deep_dig")
    fake.calls.clear()
    return pid


def get(client, path, **params):
    return client.get("/api/plugins/loshify" + path, params=params).json()


def post(client, path, body=None, method="post"):
    return getattr(client, method)("/api/plugins/loshify" + path, json=body).json()


# ── standalone import / genericity ─────────────────────────────────────────────────────────────

def test_backend_imports_standalone_and_is_generic():
    src = (PLUGIN_DIR / "dashboard" / "plugin_api.py").read_text(encoding="utf-8")
    for banned in ("feedback.jsonl", "plugins.spotify", "profiles/",
                   "access_token", "Authorization"):
        assert banned not in src, banned
    assert not any("\u0590" <= ch <= "\u05ff" for ch in src)
    assert sp.__name__.startswith("_loshify_core_")  # private alias, never shadows another plugin's ``core``


# ── setup ──────────────────────────────────────────────────────────────────────────────────────

def test_setup_done_and_onboarded_flag(env):
    client, _, db, _ = env
    out = get(client, "/setup")
    assert out["ready"] is True and out["step"] == "done" and out["onboarded"] is False
    assert out["redirect_uri"] == "http://127.0.0.1:43828/callback"
    assert out["signin_command"] == "hermes auth add loshify:spotify"
    assert out["app_name"] == "Loshify" and out["api"] == "Web API"
    assert "127.0.0.1 means 'this computer'" in out["why"]
    assert set(out["fixes"]) >= {"redirect", "not_listed", "port_busy", "no_premium"}
    db.set_meta("onboarded_at", 1_800_000_000)
    assert get(client, "/setup")["onboarded"] is True


GOOD_ID = "0123456789abcdef0123456789abcdef"


def test_setup_client_id_saves_into_the_plugin_setting_and_re_probes(env, monkeypatch):
    client, fake, _, _ = env
    saved = []
    monkeypatch.setattr(api, "save_client_id", saved.append)
    fake.routes[("GET", "/v1/me")] = [sp.SpotifyError("not signed in", setup=sp.SETUP_SIGNIN)]
    out = post(client, "/setup/client-id", {"client_id": f"  {GOOD_ID}\n"})
    assert out["ok"] and out["saved"] and saved == [GOOD_ID]
    assert out["setup"]["step"] == "signin"  # the wizard moves on to "Connect"
    assert GOOD_ID not in str(out)  # the value is never echoed back


@pytest.mark.parametrize("body,error", [
    ({}, "Paste the Client ID first."),
    ({"client_id": "   "}, "Paste the Client ID first."),
    ({"client_id": "my-client-secret"}, "That doesn't look like a Client ID"),
    (None, "Paste the Client ID first."),
])
def test_setup_client_id_refuses_bad_pastes_without_saving(env, monkeypatch, body, error):
    client, _, _, _ = env
    saved = []
    monkeypatch.setattr(api, "save_client_id", saved.append)
    out = post(client, "/setup/client-id", body)
    assert out["ok"] is False and out["error"].startswith(error) and saved == []


def test_setup_client_id_locked_profile_says_where_to_paste_instead(env, monkeypatch):
    client, _, _, _ = env

    def locked(value):
        raise ValueError("Cannot set LOSHIFY_SPOTIFY_CLIENT_ID: it is managed by your administrator")

    monkeypatch.setattr(api, "save_client_id", locked)
    out = post(client, "/setup/client-id", {"client_id": GOOD_ID})
    assert out["ok"] is False and "managed by your administrator" in out["error"]
    assert "Settings → Plugins → Loshify" in out["error"]


def test_setup_client_id_unexpected_failure_leaks_no_detail(env, monkeypatch):
    client, _, _, _ = env

    def boom(value):
        raise RuntimeError(f"disk full while writing {value}")

    monkeypatch.setattr(api, "save_client_id", boom)
    out = post(client, "/setup/client-id", {"client_id": GOOD_ID})
    assert out["ok"] is False and "RuntimeError" in out["error"] and GOOD_ID not in out["error"]


def test_default_saver_uses_hermes_own_credential_writer():
    src = (PLUGIN_DIR / "dashboard" / "plugin_api.py").read_text(encoding="utf-8")
    assert "from hermes_cli.config import save_env_value_secure" in src
    assert ".env" not in src.replace("Hermes's own writer", "")  # never writes the file itself


def test_setup_reports_the_users_next_step(env, monkeypatch):
    client, fake, _, _ = env
    fake.routes[("GET", "/v1/me")] = [sp.SpotifyError("no client id", setup=sp.SETUP_CLIENT_ID)]
    out = get(client, "/setup")
    assert out["ready"] is False and out["step"] == "client_id" and "Client ID" in out["next_step"]
    fake.routes[("GET", "/v1/me")] = [reply(403, {"error": {"status": 403, "message": "user not registered"}})]
    out = get(client, "/setup")
    assert out["step"] == "premium_or_users" and out["status"] == 403


def test_setup_never_raises_even_without_a_store(env, monkeypatch):
    client, _, _, _ = env
    monkeypatch.setattr(api, "store_factory", lambda: (_ for _ in ()).throw(OSError("no db")))
    out = get(client, "/setup")
    assert out["ready"] is True and out["onboarded"] is False


# ── player / snapshot ──────────────────────────────────────────────────────────────────────────

def test_player_snapshot_marks_managed_context(env):
    client, fake, db, settings = env
    pid = managed(fake, db, [T1["uri"], T2["uri"]])
    fake.play(T1, pid)
    settings.update({"poll_interval_open": "2", "poll_interval_closed": 0})
    out = get(client, "/player")
    assert out["ok"] and out["track"]["track_uri"] == T1["uri"] and out["track"]["managed"] is True
    assert out["data"]["item"]["name"] == "Track One"
    assert out["settings"] == {"poll_interval_open": 2, "poll_interval_closed": 1}
    fake.add_foreign_playlist("userplaylist0000000009", [T2["uri"]])
    fake.play(T2, "userplaylist0000000009")
    assert get(client, "/player")["track"]["managed"] is False


def test_player_nothing_playing_returns_last_snapshot(env):
    client, fake, _, _ = env
    fake.play(T1)
    get(client, "/player")
    fake.player = None
    out = get(client, "/player")
    assert out["ok"] and out["track"] is None and out["last"]["track_uri"] == T1["uri"]


def test_player_setup_error_carries_the_step(env):
    client, fake, _, _ = env
    fake.routes[("GET", "/v1/me/player")] = [sp.SpotifyError("not signed in", setup=sp.SETUP_SIGNIN)]
    out = get(client, "/player")
    assert out["ok"] is False and out["setup"] == "signin"


# ── reactions: managed-only removal + undo ─────────────────────────────────────────────────────

def test_down_on_loshify_playlist_removes_and_undo_puts_it_back(env):
    client, fake, db, _ = env
    pid = managed(fake, db, [T1["uri"], T2["uri"], T1["uri"], T3["uri"]])
    fake.play(T1, pid)
    get(client, "/player")
    out = post(client, "/feedback", {"track_uri": T1["uri"], "reaction": "down", "note": "too slow"})
    assert out["ok"], out
    data = out["data"]
    assert data["undoable"] is True and data["actions"]["skip"]["status"] == "skipped"
    assert data["actions"]["removal"]["status"] == "removed"
    assert data["actions"]["removal"]["playlist_name"] == "Loshify · Deep Dig"
    assert fake.playlists[pid] == [T2["uri"], T3["uri"]]
    row = db.get_reaction(data["id"])
    assert row["reaction"] == "down" and row["source"] == "desktop" and row["note"] == "too slow"

    back = post(client, "/undo", {"event_id": data["id"]})
    assert back["ok"] and back["data"]["restored"]["status"] == "restored"
    assert fake.playlists[pid] == [T1["uri"], T2["uri"], T1["uri"], T3["uri"]]
    assert db.get_reaction(data["id"]) is None
    assert post(client, "/undo", {"event_id": data["id"]})["ok"] is False  # once only


def test_down_on_users_own_playlist_never_touches_it(env):
    client, fake, db, _ = env
    fake.add_foreign_playlist("userplaylist0000000001", [T1["uri"], T2["uri"]])
    fake.play(T1, "userplaylist0000000001")
    get(client, "/player")
    fake.calls.clear()
    out = post(client, "/feedback", {"track_uri": T1["uri"], "reaction": "👎"})
    assert out["ok"] and out["data"]["actions"]["removal"]["status"] == "not_managed"
    assert fake.playlists["userplaylist0000000001"] == [T1["uri"], T2["uri"]]
    assert not any(c.path.startswith("/v1/playlists/") for c in fake.calls)


def test_down_settings_off_records_only(env):
    client, fake, db, settings = env
    pid = managed(fake, db, [T1["uri"]])
    fake.play(T1, pid)
    get(client, "/player")
    settings.update({"dislike_skips": "false", "dislike_removes_from_playlist": False})
    fake.calls.clear()
    out = post(client, "/feedback", {"track_uri": T1["uri"], "reaction": "down"})
    assert out["ok"] and out["data"]["actions"]["skip"]["status"] == "off"
    assert fake.playlists[pid] == [T1["uri"]]
    assert not any(c.method in ("POST", "DELETE") for c in fake.calls)


def test_reaction_needs_a_track_the_card_showed(env):
    client, fake, _, _ = env
    out = post(client, "/feedback", {"track_uri": T1["uri"], "reaction": "love"})
    assert out["ok"] is False and "not shown" in out["error"]
    fake.play(T1)
    get(client, "/player")
    assert post(client, "/feedback", {"track_uri": "spotify:track:bad", "reaction": "love"})["ok"] is False
    assert post(client, "/feedback", {"track_uri": T1["uri"], "reaction": "meh"})["ok"] is False
    assert post(client, "/feedback", {"track_uri": T1["uri"], "reaction": "love", "note": "x" * 501})["ok"] is False


def test_snapshot_fields_come_from_the_server_not_the_client(env):
    client, fake, db, _ = env
    fake.play(T1)
    get(client, "/player")
    out = post(client, "/feedback", {"track_uri": T1["uri"], "reaction": "love", "title": "INJECTED",
                                     "artists": ["Someone Else"]})
    row = db.get_reaction(out["data"]["id"])
    assert row["title"] == "Track One" and "Someone Else" not in str(row)


def test_late_note_amends_the_original_reaction(env):
    client, fake, db, _ = env
    fake.play(T1)
    get(client, "/player")
    first = post(client, "/feedback", {"track_uri": T1["uri"], "reaction": "okay"})["data"]["id"]
    out = post(client, "/feedback", {"track_uri": T1["uri"], "reaction": "okay", "note": "the bass", "amends": first})
    assert out["ok"] and out["data"]["id"] == first
    assert db.get_reaction(first)["note"] == "the bass"
    assert db.count_reactions() == 1
    assert post(client, "/feedback", {"track_uri": T1["uri"], "reaction": "okay", "amends": first})["ok"] is False


# ── remote ─────────────────────────────────────────────────────────────────────────────────────

def test_seek_clamps_and_guards_against_a_changed_track(env):
    client, fake, _, _ = env
    fake.play(T1)
    fake.player["item"]["duration_ms"] = 200_000
    fake.on("PUT", "/v1/me/player/seek", reply(204))
    out = post(client, "/seek", {"position_ms": 999_999, "track_uri": T1["uri"]})
    assert out["ok"] and out["data"]["position_ms"] == 200_000
    assert [c.query["position_ms"] for c in fake.calls if c.path == "/v1/me/player/seek"] == ["200000"]
    out = post(client, "/seek", {"position_ms": 1000, "track_uri": T2["uri"]})
    assert out["ok"] is False and out["status"] == "track_changed"
    assert post(client, "/seek", {"position_ms": "abc"})["ok"] is False


def test_playlists_lists_loshify_ones_first(env):
    client, fake, db, _ = env
    pid = managed(fake, db, [T1["uri"]])
    fake.on("GET", "/v1/me/playlists", reply(200, {"items": [
        {"id": "userplaylist0000000001", "name": "Mine", "uri": "spotify:playlist:userplaylist0000000001"},
        {"id": pid, "name": "Loshify · Deep Dig", "uri": f"spotify:playlist:{pid}"}]}))
    items = get(client, "/playlists")["data"]["items"]
    assert [i["loshify"] for i in items] == [True, False]


def test_search_caps_the_limit_at_ten(env):
    client, fake, _, _ = env
    get(client, "/search", q="track", limit=50)
    assert [c.query["limit"] for c in fake.calls if c.path == "/v1/search"] == ["10"]
    assert get(client, "/search", q="t")["data"]["tracks"]["items"] == []


def test_devices_falls_back_to_the_player_device(env):
    client, fake, _, _ = env
    fake.on("GET", "/v1/me/devices", reply(410, {"error": {"status": 410, "message": "gone"}}))
    fake.play(T1)
    fake.player["device"] = {"id": "dev1", "name": "Laptop", "type": "Computer"}
    out = get(client, "/devices")
    assert out["ok"] and out["data"]["devices"][0]["id"] == "dev1"


def test_unexpected_errors_never_leak_details(env, monkeypatch):
    client, _, _, _ = env

    def boom():
        raise RuntimeError("secret-ish detail")

    monkeypatch.setattr(api, "client_factory", boom)
    out = get(client, "/player")
    assert out["ok"] is False and "secret-ish" not in out["error"] and "RuntimeError" in out["error"]


def test_settings_parsing_is_defensive(monkeypatch):
    monkeypatch.setattr(api, "raw_settings", lambda: {"poll_interval_open": "x", "dislike_skips": "no",
                                                      "love_saves_to_liked": 1})
    s = api.plugin_settings()
    assert s["poll_interval_open"] == 3 and s["dislike_skips"] is False and s["love_saves_to_liked"] is True
    monkeypatch.setattr(api, "raw_settings", lambda: (_ for _ in ()).throw(RuntimeError()))
    assert api.plugin_settings() == api.SETTING_DEFAULTS


# ── rotation (read-only) ────────────────────────────────────────────────────────────────────────

def test_rotation_route_is_read_only_and_reports_presets_then_the_spec(env):
    http, fake, db, _ = env
    res = http.get("/api/plugins/loshify/rotation").json()
    assert res["ok"] and res["data"]["configured"] is False and res["data"]["max_slots"] == 6
    assert {p["id"] for p in res["data"]["presets"]} >= {"rhythm_quiet_mixed", "day_parts", "discovery_dial"}
    rot = api._core("rotations")
    rot.set_spec(db, rot.from_preset("rhythm_quiet_mixed"))
    res = http.get("/api/plugins/loshify/rotation").json()["data"]
    assert res["configured"] and res["spec"]["name"] == "Rhythmic / Quiet / Mixed" and res["due"] is True
    assert [p["slot"] for p in res["playlists"]] == ["Rhythmic", "Quiet", "Mixed"]
    assert fake.calls == []  # no Spotify traffic
    for method in ("post", "put", "delete"):
        assert getattr(http, method)("/api/plugins/loshify/rotation").status_code == 405


# ── onboarding: calibration reactions, the note, skip ──────────────────────────────────────────

def _calibration_round(db):
    ob = api._core("onboarding")
    db.set_meta("onboarded_at", 1)
    ob.start(db)
    picks = [{"title": "Track One", "artist": "Band", "kind": "familiar"},
             {"title": "Track Three", "artist": "Other", "kind": "stretch"}]
    ob.propose(sp.SpotifyClient(FakeSpotify([T1, T2, T3]), sleep=lambda s: None), db, picks)
    return ob


def test_onboarding_route_reads_progress_without_spotify(env):
    http, fake, db, _ = env
    _calibration_round(db)
    res = get(http, "/onboarding")
    assert res["ok"] and res["data"]["active"] and res["data"]["next_step"] == "portrait"
    assert [t["title"] for t in res["data"]["calibration"]["tracks"]] == ["Track One", "Track Three"]
    assert res["data"]["note_max"] == 300 and fake.calls == []


def test_calibration_react_goes_through_reactions_and_never_removes(env):
    http, fake, db, settings = env
    _calibration_round(db)
    settings["dislike_removes_from_playlist"] = True  # forced off for calibration: there is no playlist
    out = post(http, "/onboarding/react", {"n": 1, "reaction": "down"})
    assert out["ok"] and out["data"]["reaction"] == "down" and out["data"]["reacted"] == 1
    row = db.get_reaction(out["data"]["id"])
    assert row["source"] == "desktop" and row["track_uri"] == T1["uri"]
    assert not any(c.method == "DELETE" for c in fake.calls)
    again = post(http, "/onboarding/react", {"n": 1, "reaction": "love"})
    assert again["data"]["id"] == out["data"]["id"] and db.get_reaction(out["data"]["id"])["reaction"] == "love"
    done = post(http, "/onboarding/react", {"track_uri": T3["uri"], "reaction": "👌"})
    assert done["ok"] and done["data"]["complete"] is True
    assert post(http, "/onboarding/react", {"n": 9, "reaction": "love"})["ok"] is False
    assert post(http, "/onboarding/react", {"n": 1, "reaction": "meh"})["ok"] is False
    assert post(http, "/onboarding/react", {"reaction": "love"})["ok"] is False


def test_note_route_validates_saves_and_clears(env):
    http, _, db, _ = env
    _calibration_round(db)
    assert post(http, "/note", {"text": "x" * 301})["ok"] is False
    assert post(http, "/note", {"text": 5})["ok"] is False
    saved = post(http, "/note", {"text": "  more  dub,\n less EDM "})
    assert saved["ok"] and saved["data"]["note"] == "more dub, less EDM"
    assert get(http, "/onboarding")["data"]["note"] == "more dub, less EDM"
    assert post(http, "/note", {"text": ""})["data"]["note"] == ""


def test_onboarding_skip_route(env):
    http, _, db, _ = env
    _calibration_round(db)
    out = post(http, "/onboarding/skip", {"step": "portrait"})
    assert out["ok"] and out["data"]["next_step"] == "anchors"
    assert post(http, "/onboarding/skip", {"step": "nope"})["ok"] is False


def test_setup_without_a_client_id_is_step_client_id_even_when_hermes_says_not_signed_in(env, monkeypatch):
    # t_089118df: fresh profile, no Client ID. Real Hermes raises ProviderNotSignedIn for GET /me before it
    # checks the Client ID; /setup must still answer client_id (wizard step 4), never signin (1–4 "done").
    from fakes import ProviderNotSignedIn
    client, fake, _, _ = env
    monkeypatch.setattr(api, "client_id_present", lambda: False)
    fake.routes[("GET", "/v1/me")] = [ProviderNotSignedIn("no grant")]
    out = get(client, "/setup")
    assert out["ready"] is False and out["step"] == "client_id" and "Client ID" in out["next_step"]
    monkeypatch.setattr(api, "client_id_present", lambda: True)
    fake.routes[("GET", "/v1/me")] = [ProviderNotSignedIn("no grant")]
    assert get(client, "/setup")["step"] == "signin"


def test_setup_client_id_save_moves_on_to_signin_even_if_the_scope_lags(env, monkeypatch):
    from fakes import ProviderNotSignedIn
    client, fake, _, _ = env
    monkeypatch.setattr(api, "client_id_present", lambda: False)  # the bound scope predates the save
    monkeypatch.setattr(api, "save_client_id", lambda value: None)
    fake.routes[("GET", "/v1/me")] = [ProviderNotSignedIn("no grant")]
    out = post(client, "/setup/client-id", {"client_id": GOOD_ID})
    assert out["ok"] is True and out["setup"]["step"] == "signin"
