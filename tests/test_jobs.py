"""Finding 3 (long Desktop steps run as background jobs) and finding 2 (Start over), offline."""

import threading

import pytest

pytest.importorskip("fastapi")

from test_dashboard_api import api, get, post  # noqa: E402
from test_dashboard_flow import env  # noqa: E402,F401  (fixture)

jobs = api._core("jobs")
ob = api._core("onboarding")

ID = "job-0000-aaaa"


# -- core.jobs ------------------------------------------------------------------------------------------

def test_job_runs_reports_progress_and_result():
    reg = jobs.Jobs()
    seen = []

    def fn(progress):
        progress("Writing your playlists… 1/3")
        seen.append(reg.get(ID)["progress"])
        return {"ok": True, "data": {"ok": True, "playlists": []}}

    view = reg.start(ID, "build", fn, run_inline=True)
    assert seen == ["Writing your playlists… 1/3"]
    assert view["state"] == jobs.DONE and view["result"]["data"]["ok"] is True and view["message"] is None


def test_failure_carries_only_a_plain_reason_never_exception_text():
    reg = jobs.Jobs()
    told = reg.start("job-told-0001", "build", lambda p: {"ok": True, "data": {"ok": False, "error": "Pick a card"}},
                     run_inline=True)
    assert told["state"] == jobs.FAILED and told["message"] == "Pick a card" and told["result"] is None

    def boom(progress):
        raise RuntimeError("secret internals /home/x")

    crashed = reg.start("job-boom-0001", "build", boom, run_inline=True)
    assert crashed["state"] == jobs.FAILED and crashed["message"] is None


def test_same_id_is_idempotent_and_one_running_job_per_kind():
    reg = jobs.Jobs()
    gate = threading.Event()
    calls = []

    def slow(progress):
        calls.append(1)
        gate.wait(5)
        return {"ok": True, "data": {"ok": True}}

    first = reg.start(ID, "build", slow)
    again = reg.start(ID, "build", slow)
    other = reg.start("job-other-0001", "build", slow)  # a second Build while one runs → the running one
    assert first["state"] == again["state"] == jobs.RUNNING
    assert other["id"] == ID
    assert reg.start("job-meet-00001", "meet", lambda p: {"ok": True, "data": {}}, run_inline=True)["state"] == jobs.DONE
    gate.set()
    for _ in range(100):
        if reg.get(ID)["state"] != jobs.RUNNING:
            break
        threading.Event().wait(0.02)
    assert reg.get(ID)["state"] == jobs.DONE and len(calls) == 1


def test_a_hung_job_reads_as_failed_and_ids_are_validated():
    now = [1000.0]
    reg = jobs.Jobs(clock=lambda: now[0])
    gate = threading.Event()
    reg.start(ID, "build", lambda p: gate.wait(5) or {"ok": True, "data": {}})
    now[0] += jobs.STALE_S + 1
    assert reg.get(ID)["state"] == jobs.FAILED
    assert reg.running("build")["id"] == ID
    assert reg.start("retry-job-0001", "build", lambda p: {})["id"] == ID
    gate.set()
    for bad in ("", "short", "has space here", "x" * 65, None, 7):
        assert not jobs.valid_id(bad)
        with pytest.raises(ValueError):
            reg.start(bad, "build", lambda p: {})


# -- routes ---------------------------------------------------------------------------------------------

@pytest.fixture
def inline(monkeypatch):
    monkeypatch.setattr(api, "JOB_RUN_INLINE", True)
    monkeypatch.setattr(api, "_JOBS", None)


def test_build_with_a_job_answers_at_once_and_the_poll_has_the_links(env, inline):  # noqa: F811
    http, fake, db, _ = env
    post(http, "/flow/meet")
    started = post(http, "/flow/build", {"preset": "day_parts", "job": ID})
    assert started["ok"] and started["data"]["job"]["id"] == ID
    polled = get(http, "/flow/job", id=ID)
    assert polled["ok"] and polled["data"]["state"] == "done"
    links = polled["data"]["result"]["data"]["playlists"]
    assert [l["name"] for l in links] == ["Loshify · Morning", "Loshify · Focus", "Loshify · Night"]
    assert all(l["url"] and l["tracks"] > 0 for l in links)


def test_the_finished_job_is_remembered_after_a_restart(env, inline, monkeypatch):  # noqa: F811
    http, fake, db, _ = env
    post(http, "/flow/meet")
    post(http, "/flow/build", {"preset": "day_parts", "job": ID})
    monkeypatch.setattr(api, "_JOBS", None)  # a new dashboard process
    polled = get(http, "/flow/job", id=ID)["data"]
    assert polled["state"] == "done" and len(polled["result"]["data"]["playlists"]) == 3
    assert get(http, "/flow/job", id="never-started-1")["data"]["state"] == "unknown"
    assert get(http, "/flow/job", id="bad id")["ok"] is False


def test_a_real_background_build_and_meet(env, monkeypatch):  # noqa: F811
    """No inline flag: the work runs on a thread with the request's context, the POST answers 'running'."""
    http, fake, db, _ = env
    monkeypatch.setattr(api, "_JOBS", None)
    met = post(http, "/flow/meet", {"job": "meet-thread-01"})
    assert met["ok"] and met["data"]["job"]["state"] in ("running", "done")
    for _ in range(250):
        view = get(http, "/flow/job", id="meet-thread-01")["data"]
        if view["state"] != "running":
            break
        threading.Event().wait(0.02)
    assert view["state"] == "done", view
    assert view["result"]["data"]["portrait_by"] == "llm"
    post(http, "/flow/build", {"preset": "day_parts", "job": "build-thread-1"})
    for _ in range(250):
        view = get(http, "/flow/job", id="build-thread-1")["data"]
        if view["state"] != "running":
            break
        threading.Event().wait(0.02)
    assert view["state"] == "done" and len(view["result"]["data"]["playlists"]) == 3


def test_without_a_job_the_routes_stay_synchronous(env):  # noqa: F811
    http, fake, db, _ = env
    assert post(http, "/flow/meet")["data"]["portrait_by"] == "llm"
    assert post(http, "/flow/build", {"preset": "day_parts"})["data"]["playlists"]


def test_a_build_that_fails_server_side_is_a_failed_job_with_a_plain_line(env, inline):  # noqa: F811
    http, fake, db, _ = env
    post(http, "/flow/meet")
    post(http, "/flow/build", {"job": ID})  # no preset, no words
    view = get(http, "/flow/job", id=ID)["data"]
    assert view["state"] == "failed" and view.get("result") is None
    assert view["message"] is None or "Traceback" not in view["message"]


def test_progress_lines_name_each_playlist_step():
    flow = api._core("flow")
    said = []
    flow._say(said.append, "slot", i=2, n=3)
    assert said == ["Writing your playlists… 2/3"]


# -- Start over (finding 2) ----------------------------------------------------------------------------

def test_start_over_keeps_reactions_anchors_and_note(env):  # noqa: F811
    http, fake, db, _ = env
    met = post(http, "/flow/meet")["data"]
    first = met["calibration"]["tracks"][0]
    assert post(http, "/onboarding/react", {"n": first["n"], "reaction": "love"})["ok"]
    post(http, "/note", {"text": "no christmas music"})
    post(http, "/flow/build", {"preset": "day_parts"})
    before = db.conn.execute("SELECT COUNT(*) FROM reactions").fetchone()[0]
    assert before >= 1 and get(http, "/onboarding")["data"].get("calibration")

    out = post(http, "/onboarding/restart")
    assert out["ok"]
    after = get(http, "/onboarding")["data"]
    assert not after.get("calibration") and after["note"] == "no christmas music"
    assert db.conn.execute("SELECT COUNT(*) FROM reactions").fetchone()[0] == before
    again = post(http, "/flow/meet")["data"]  # "Get to know me" runs again from the top
    assert again["calibration"]["total"] == 5 and again["calibration"]["reacted"] == 0


# -- Finding 4: the playlist name reaches Spotify and the database as real UTF-8 ------------------------

def test_playlist_name_middle_dot_is_utf8_on_the_wire_and_in_sqlite():
    httpx = pytest.importorskip("httpx")
    name = "Loshify · Morning"
    req = httpx.Client().build_request("POST", "https://api.spotify.com/v1/me/playlists", json={"name": name})
    assert "\u00b7".encode("utf-8") in req.content  # b'\xc2\xb7', not an escaped or cp1252 byte
    db = api._core("store").open_store(":memory:")
    try:
        db.add_playlist("p1", name, "rotation", {}, ts=1)
        assert db.conn.execute("SELECT hex(name) FROM playlists").fetchone()[0] == name.encode("utf-8").hex().upper()
    finally:
        db.close()


def test_tune_in_job_and_legacy_route(env, inline, monkeypatch):
    monkeypatch.setattr(api._core("flow"), "tune_in_start", lambda *a, **k: {"ok": True, "round": {"tracks": []}})
    started = api.tune_in_start({"job": ID})
    assert started["data"]["job"]["id"] == ID
    assert api.flow_job(id=ID)["data"]["result"]["data"]["ok"]
    assert api.tune_in_start({})["data"]["ok"]
