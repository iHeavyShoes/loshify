"""Desktop routes for the click-through flow and Tune in (offline: FakeSpotify + a fake host LLM)."""

import pytest

pytest.importorskip("fastapi")
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from fakes import FakeSpotify, cat_track, reply  # noqa: E402
from test_dashboard_api import _KeptStore, api, get, post  # noqa: E402

sp = api._core("spotify")
st = api._core("store")
LIB = [cat_track(f"Old Song {i}", f"Lib Artist {i}", f"l{i:02d}") for i in range(20)]
NEW = [cat_track(f"Fresh Song {i}", f"New Artist {i}", f"n{i:02d}") for i in range(60)]


def history(fake):
    fake.on("GET", "/v1/me/top/artists", reply(200, {"items": [{"name": f"Lib Artist {i}"} for i in range(5)]}))
    fake.on("GET", "/v1/me/top/tracks", reply(200, {"items": LIB[:6]}))
    fake.on("GET", "/v1/me/tracks", reply(200, {"items": [{"added_at": "2020-03-01T00:00:00Z", "item": t} for t in LIB],
                                                "next": None}))
    fake.on("GET", "/v1/me/player/recently-played", reply(200, {"items": []}))
    return fake


def fake_llm(purpose, instructions, data):
    if purpose == "portrait":
        return {"portrait": "Right now you keep coming back to Lib Artist 0, with a soft spot for slow songs."}
    if purpose in ("calibration", "tune_in"):
        base = 0 if purpose == "calibration" else 40
        return {"picks": [{"title": f"Fresh Song {base + i}", "artist": f"New Artist {base + i}",
                           "kind": ["familiar", "near", "stretch"][i % 3], "reason": "why"} for i in range(8)]}
    if purpose == "rotation":
        return {"slots": {s["name"]: [{"title": f"Fresh Song {10 + j}", "artist": f"New Artist {10 + j}",
                                       "reason": "new"} for j in range(25)] for s in data["slots"]}}
    return None


@pytest.fixture
def env(monkeypatch):
    fake = history(FakeSpotify(LIB + NEW))
    db = st.open_store(":memory:")
    settings = {}
    monkeypatch.setattr(api, "client_factory", lambda: sp.SpotifyClient(fake, sleep=lambda s: None))
    monkeypatch.setattr(api, "store_factory", lambda: _KeptStore(db))
    monkeypatch.setattr(api, "raw_settings", lambda: settings)
    monkeypatch.setattr(api, "_bind_profile_scope", lambda: None)
    monkeypatch.setattr(api, "llm_factory", lambda: fake_llm)
    app = FastAPI()
    app.include_router(api.router, prefix="/api/plugins/loshify")
    yield TestClient(app), fake, db, settings
    db.close()


def test_click_through_routes_portrait_calibration_build(env):
    http, fake, db, _ = env
    met = post(http, "/flow/meet")
    assert met["ok"] and met["data"]["portrait_by"] == "llm" and met["data"]["calibration"]["total"] == 5
    assert "total" in met["data"]["timing"]
    v = post(http, "/flow/verdict", {"verdict": "sounds_like_me"})
    assert v["ok"] and v["data"]["verdict"] == "sounds_like_me"
    assert post(http, "/flow/verdict", {"verdict": "maybe"})["ok"] is False
    assert post(http, "/flow/verdict", {"verdict": "not_quite", "anchors": [3]})["ok"] is False
    fake.play(NEW[0])
    fake.calls.clear()
    r = post(http, "/onboarding/react", {"n": 1, "reaction": "down"})
    assert r["ok"] and r["data"]["actions"] == {"recorded_only": True}
    assert not any(c.path.startswith("/v1/me/player") for c in fake.calls)  # item 3, through the route
    built = post(http, "/flow/build", {"preset": "then_now_next", "note": "no christmas music"})
    assert built["ok"] and built["data"]["ok"] and len(built["data"]["playlists"]) == 3
    assert post(http, "/flow/build", {"preset": 7})["ok"] is False
    assert post(http, "/flow/build", {})["ok"] is False


def test_flow_routes_survive_a_missing_or_broken_llm(env, monkeypatch):
    http, _, _, _ = env
    monkeypatch.setattr(api, "llm_factory", lambda: (_ for _ in ()).throw(ImportError("old hermes")))
    met = post(http, "/flow/meet")
    assert met["ok"] and met["data"]["portrait_by"] == "fallback" and met["data"]["portrait"]


def test_tune_in_routes(env):
    http, _, db, settings = env
    settings["tune_in_days"] = 99  # clamped to 14
    assert get(http, "/tune-in")["data"]["eligible"] is False
    post(http, "/flow/meet")
    post(http, "/flow/verdict", {"verdict": "sounds_like_me"})
    post(http, "/flow/build", {"preset": "then_now_next"})
    status = get(http, "/tune-in")["data"]
    assert status["interval_days"] == 14 and status["available"] is False and status["next_in_days"] == 14
    assert "Next tune-in available in 14 days" == status["line"]
    early = post(http, "/tune-in/start")
    assert early["ok"] and early["data"]["ok"] is False
    # Pretend the first run ended 15 days ago.
    import json
    ob_state = json.loads(db.get_meta("onboarding"))
    ob_state["finished"] -= 15 * 86400
    db.set_meta("onboarding", json.dumps(ob_state))
    assert get(http, "/tune-in")["data"]["available"] is True
    started = post(http, "/tune-in/start")
    assert started["data"]["ok"] and started["data"]["round"]["total"] == 5
    assert get(http, "/tune-in")["data"]["round"]["total"] == 5
    react = post(http, "/tune-in/react", {"n": 1, "reaction": "❤️"})
    assert react["ok"] and react["data"]["reaction"] == "love"
    assert post(http, "/tune-in/react", {"reaction": "love"})["ok"] is False
    done = post(http, "/tune-in/finish")
    assert done["ok"] and done["data"]["tune_in"]["available"] is False and done["data"]["tune_in"]["next_in_days"] == 14


def test_demo_mode_tune_in_now_and_real_counts_on_the_card(env):
    http, _, db, settings = env
    assert get(http, "/demo")["data"] == {"on": False}  # off by default: nothing extra on the card
    settings["demo_mode"] = True
    assert get(http, "/tune-in")["data"]["eligible"] is False  # still needs the first run
    assert get(http, "/demo")["data"] == {"on": True}  # no taste, no refresh yet: nothing made up
    post(http, "/flow/meet")
    post(http, "/flow/verdict", {"verdict": "sounds_like_me"})
    post(http, "/flow/build", {"preset": "then_now_next"})
    status = get(http, "/tune-in")["data"]
    assert status["available"] is True and status["demo"] is True  # no 7-14-day wait
    started = post(http, "/tune-in/start")
    assert started["data"]["ok"] and started["data"]["round"]["total"] == 5
    assert post(http, "/tune-in/react", {"n": 1, "reaction": "love"})["ok"]
    done = post(http, "/tune-in/finish")
    assert done["data"]["tune_in"]["available"] is True  # and again right after a round
    shown = get(http, "/demo")["data"]
    assert "archive" not in shown  # the first edition archives nothing: no counts yet
    assert shown["taste"]["loves"] == 1 and shown["taste"]["line"].endswith("· 1 ❤️ · 0 👎")  # the ❤️ from the round
    # the real counts of the last refresh that curated an Archive
    import json
    from core import rotations as rot
    state = json.loads(db.get_meta(rot.STATE_KEY))
    state["last_archive"] = {"added": 14, "expired": 9, "evicted": 0, "disliked": 0, "promoted": 1,
                             "keepers": 3, "kept": 52, "ts": 1}
    db.set_meta(rot.STATE_KEY, json.dumps(state))
    archive = get(http, "/demo")["data"]["archive"]
    assert archive["added"] == 14 and archive["line"] == "Archive: 14 added, 9 aged out, 3 keepers (52 tracks)"
    settings["demo_mode"] = "off"
    assert get(http, "/demo")["data"] == {"on": False}
    assert get(http, "/tune-in")["data"]["available"] is False


def test_default_llm_sends_each_purpose_schema_through_the_facade():
    """The real adapter over a provider that only takes json_schema (Claude subscription)."""
    from types import SimpleNamespace

    from test_llm_json import FakeFacade

    fake = FakeFacade({"schema"}, answer={"picks": []})
    call = api._schema_llm(fake, lambda t: SimpleNamespace(text=t))
    for purpose in ("portrait", "calibration", "tune_in", "rotation", "rotation_spec"):
        assert call(purpose, "x", {"a": 1}) == {"picks": []}
        mode, kw = fake.calls[-1]
        assert mode == "schema" and kw["json_schema"] == api._core("writer").SCHEMAS[purpose]
        assert kw["timeout"] == api.LLM_TIMEOUT_S and kw["max_tokens"] == api.LLM_MAX_TOKENS
    assert call.asker.mode == "schema" and len(fake.calls) == 5
