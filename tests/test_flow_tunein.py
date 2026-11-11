"""The click-through Desktop flow (no chat) and the recurring Tune-in round."""

import json

import pytest

from core import flow
from core import onboarding as ob
from core import rotations as rot
from core import spotify as sp
from core import store as st
from core import tunein
from core.store import DAY
from fakes import FakeSpotify, cat_track, reply

NOW = 1_800_000_000
LIB = [cat_track(f"Old Song {i}", f"Lib Artist {i}", f"l{i:02d}") for i in range(30)]
NEW = [cat_track(f"Fresh Song {i}", f"New Artist {i}", f"n{i:02d}") for i in range(80)]


def fake_spotify():
    fake = FakeSpotify(LIB + NEW)
    fake.on("GET", "/v1/me/top/artists", lambda c: reply(200, {"items": [
        {"name": f"Lib Artist {i}", "genres": ["dream pop"]} for i in range(6)]}))
    fake.on("GET", "/v1/me/top/tracks", reply(200, {"items": LIB[:8]}))
    fake.on("GET", "/v1/me/tracks", reply(200, {"items": [{"added_at": f"{2015 + i % 9}-03-01T00:00:00Z", "item": t}
                                                          for i, t in enumerate(LIB)], "next": None}))
    fake.on("GET", "/v1/me/player/recently-played", reply(200, {"items": [{"played_at": "x", "track": LIB[0]}]}))
    return fake


def client(fake):
    return sp.SpotifyClient(fake, sleep=lambda s: None)


@pytest.fixture
def db():
    store = st.open_store(":memory:")
    yield store
    store.close()


class FakeLlm:
    """Answers each purpose like a cooperative model; records what it was shown."""

    def __init__(self):
        self.calls = []

    def __call__(self, purpose, instructions, data):
        self.calls.append((purpose, data))
        if purpose == "portrait":
            return {"portrait": "Right now you keep coming back to Lib Artist 0 and dreamy guitars."}
        if purpose in ("calibration", "tune_in"):
            kinds = ["familiar", "familiar", "near", "near", "stretch"] * 2
            base = 0 if purpose == "calibration" else 40
            return {"picks": [{"title": f"Fresh Song {base + i}", "artist": f"New Artist {base + i}", "kind": kinds[i],
                               "reason": f"a reason {i}"} for i in range(10)]}
        if purpose == "rotation":
            return {"slots": {s["name"]: [{"title": f"Fresh Song {10 + j}", "artist": f"New Artist {10 + j}",
                                           "reason": "new to you"} for j in range(25)]
                              for s in data["slots"]}}
        if purpose == "rotation_spec":
            return {"name": "Mine", "cadence": "weekly", "archive": "archive",
                    "slots": [{"name": "Fresh", "intent": "new stuff", "mode": "discover"}]}
        return None


def test_click_through_flow_end_to_end_with_an_llm(db):
    fake, llm = fake_spotify(), FakeLlm()
    met = flow.meet(client(fake), db, llm, NOW)
    assert met["ok"] and met["portrait_by"] == "llm" and "Lib Artist 0" in met["portrait"]
    assert met["calibration"]["total"] == 5 and met["calibration_by"] == "llm"
    assert set(met["timing"]) >= {"learn_taste", "portrait", "calibration_picks", "calibration_resolve", "total"}
    assert db.get_meta("portrait") == met["portrait"]

    v = flow.verdict(client(fake), db, "sounds_like_me", None, llm, NOW + 1)
    assert v["verdict"] == "sounds_like_me" and "portrait" not in v
    for n, r in ((1, "love"), (2, "okay"), (3, "down"), (4, "love"), (5, "okay")):
        ob.react(db, n, r, source="desktop", now=NOW + 2)

    built = flow.build(client(fake), db, preset="then_now_next", note="no christmas music", llm=llm, now=NOW + 3)
    assert built["ok"] and len(built["playlists"]) == 3 and all(p["url"] for p in built["playlists"])
    assert built["candidates_by"] == "llm" and "first_edition" in built["timing"]
    status = ob.status(db)
    assert status["finished"] and {s["step"]: s["state"] for s in status["steps"]}["note"] == "done"
    rotation_call = next(d for p, d in llm.calls if p == "rotation")
    assert "Lib Artist 0" in rotation_call["known_artists"] and "christmas" in rotation_call["user_note"]


def test_flow_without_any_llm_still_completes_from_the_users_own_data(db):
    fake = fake_spotify()
    met = flow.meet(client(fake), db, None, NOW)
    assert met["portrait_by"] == "fallback" and met["portrait"]
    assert met["calibration_by"] == "fallback" and met["calibration"]["total"] >= 3
    assert all(t["kind"] == "familiar" for t in met["calibration"]["tracks"])
    built = flow.build(client(fake), db, preset="then_now_next", llm=None, now=NOW + 1)
    slots = {s["slot"]: s for s in built["slots"]}
    assert slots["Then"]["ok"] and slots["Next"]["skipped"] == "needs_candidates"
    assert built["ok"] and built["candidates_by"] == "fallback"


def test_not_quite_with_anchors_rewrites_the_portrait_with_the_correction(db):
    fake, llm = fake_spotify(), FakeLlm()
    flow.meet(client(fake), db, llm, NOW)
    out = flow.verdict(client(fake), db, "not_quite", ["New Artist 70"], llm, NOW + 1)
    assert out["verdict"] == "not_quite" and out["portrait_by"] == "llm"
    assert [a["artist"] for a in out["anchors"]] == ["New Artist 70"]
    portrait_call = [d for p, d in llm.calls if p == "portrait"][-1]
    assert portrait_call["corrections"]["not_quite"] is True
    assert "New Artist 70" in [x["artist"] for x in portrait_call["lead"]]
    skipped = flow.verdict(client(fake), db, "not_quite", [], llm, NOW + 2)  # anchors are optional
    assert skipped["verdict"] == "not_quite"


def test_own_words_arrangement_and_bad_input(db):
    fake, llm = fake_spotify(), FakeLlm()
    flow.meet(client(fake), db, llm, NOW)
    built = flow.build(client(fake), db, words="just one list of new stuff", llm=llm, now=NOW + 1)
    assert built["ok"] and [p["slot"] for p in built["playlists"]] == ["Fresh"]
    assert rot.get_spec(db)["source_text"] == "just one list of new stuff"
    nothing = flow.build(client(fake), db, words="anything", llm=None, now=NOW + 2)
    assert nothing["ok"] is False and "Pick a card" in nothing["error"]
    with pytest.raises(ValueError):
        flow.build(client(fake), db, now=NOW + 3)


# ── Tune in ────────────────────────────────────────────────────────────────────────────────────

def finished_first_run(db, fake, llm, at=NOW):
    flow.meet(client(fake), db, llm, at)
    flow.verdict(client(fake), db, "sounds_like_me", None, llm, at)
    flow.build(client(fake), db, preset="then_now_next", llm=llm, now=at)
    assert ob.status(db)["finished"]


def test_tune_in_schedule_is_optional_and_counts_down(db):
    assert tunein.status(db, NOW)["eligible"] is False  # nothing before the first run
    finished_first_run(db, fake_spotify(), FakeLlm())
    s = tunein.status(db, NOW + DAY, interval_days=10)
    assert not s["available"] and s["next_in_days"] == 9 and s["line"] == "Next tune-in available in 9 days"
    assert tunein.status(db, NOW + 10 * DAY, 10)["available"]
    assert tunein.clamp_interval(3) == 7 and tunein.clamp_interval(30) == 14 and tunein.clamp_interval("x") == 10
    out = flow.tune_in_start(client(fake_spotify()), db, FakeLlm(), NOW + DAY, 10)
    assert out["ok"] is False and "9 days" in out["error"]


def test_demo_mode_makes_tune_in_available_now_but_not_before_the_first_run(db):
    assert tunein.status(db, NOW, demo=True)["eligible"] is False
    fake, llm = fake_spotify(), FakeLlm()
    finished_first_run(db, fake, llm)
    s = tunein.status(db, NOW + 60, 14, demo=True)
    assert s["available"] and s["demo"] and s["next_in_days"] == 0
    assert "demo" not in tunein.status(db, NOW + 60, 14)
    out = flow.tune_in_start(client(fake), db, llm, NOW + 60, 14, demo=True)
    assert out["ok"] and out["round"]["total"] == 5
    done = flow.tune_in_finish(db, NOW + 120, 14, demo=True)
    assert done["tune_in"]["available"] is True  # right after a round, again
    assert flow.tune_in_finish(db, NOW + 180, 14)["tune_in"]["available"] is False  # off: the wait is back


def test_tune_in_round_targets_uncertainty_and_feeds_the_normal_path(db):
    fake, llm = fake_spotify(), FakeLlm()
    finished_first_run(db, fake, llm)
    later = NOW + 12 * DAY
    db.add_reaction("spotify:track:x1", "down", title="t", artists=["New Artist 12"], ts=later - DAY)
    db.add_reaction("spotify:track:x2", "love", title="t", artists=["New Artist 13"], ts=later - 2 * DAY)
    db.add_reaction("spotify:track:x3", "down", title="t", artists=["New Artist 13"], ts=later - DAY)
    db.add_reaction("spotify:track:x4", "okay", title="t", artists=["New Artist 14"], ts=later - DAY)
    targets = tunein.targets(db, later)
    assert "New Artist 12" in targets["recent_downs"] and "New Artist 13" in targets["edges"]
    assert "New Artist 14" in targets["new_lanes"]

    out = flow.tune_in_start(client(fake), db, llm, later, 10)
    assert out["ok"] and out["round"]["total"] == 5
    assert next(d for p, d in llm.calls if p == "tune_in")["targets"] == targets
    first_cal = ob.calibration_view(db)  # the first-run round is untouched
    r = ob.react(db, 1, "down", source="desktop", now=later + 1, key=tunein.ROUND_KEY)
    assert r["ok"] and r["actions"] == {"recorded_only": True} and db.get_reaction(r["id"])["reaction"] == "down"
    assert ob.calibration_view(db) == first_cal
    log = json.loads(db.get_meta("calibration_log"))
    assert log[-1]["source"] == "tune_in"
    done = flow.tune_in_finish(db, later + 2, 10)
    assert done["tune_in"]["available"] is False and done["tune_in"]["next_in_days"] == 10
    assert tunein.status(db, later + 2 + 10 * DAY, 10)["available"]


def test_tune_in_without_an_llm_does_not_fake_a_round(db):
    fake = fake_spotify()
    finished_first_run(db, fake, FakeLlm())
    out = flow.tune_in_start(client(fake), db, None, NOW + 11 * DAY, 10)
    assert out["ok"] is False and "curator" in out["error"]
    assert tunein.status(db, NOW + 11 * DAY, 10)["available"]  # still available: nothing was used up
