"""Rotations: presets, spec validation, storage, refresh with archive (managed-only), tool contract."""

import json
from datetime import datetime, timezone

import pytest

from core import rotations as rot
from core import spotify as sp
from core import store as st
from core.store import DAY
from fakes import FakeSpotify, cat_track, reply
from loshify.tools import handlers as h

NOW = 1_800_000_000

# Library (saved) tracks by "Lib N"; discovery catalog by "New N".
LIB = [cat_track(f"Old Song {i}", f"Lib Artist {i}", f"l{i:02d}") for i in range(40)]
NEW = [cat_track(f"Fresh Song {i}", f"New Artist {i}", f"n{i:02d}") for i in range(60)]
CATALOG = LIB + NEW


def iso(year: int) -> str:
    return datetime(year, 3, 1, tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fake_spotify():
    fake = FakeSpotify(CATALOG)
    fake.on("GET", "/v1/me/tracks", reply(200, {"items": [{"added_at": iso(2012 + i % 10), "item": t}
                                                          for i, t in enumerate(LIB)], "next": None}))
    fake.on("GET", "/v1/me/top/tracks", reply(200, {"items": []}))
    fake.on("GET", "/v1/me/player/recently-played", reply(200, {"items": []}))
    return fake


def client(fake):
    return sp.SpotifyClient(fake, sleep=lambda s: None)


@pytest.fixture
def db():
    store = st.open_store(":memory:")
    store.set_meta("onboarded_at", NOW - 30 * DAY)
    yield store
    store.close()


def cands(start, n, artist_step=1):
    return [{"title": f"Fresh Song {i}", "artist": f"New Artist {i}", "reason": f"because {i}"}
            for i in range(start, start + n, artist_step)]


# ── presets ────────────────────────────────────────────────────────────────────────────────────

def test_presets_are_generic_valid_and_include_the_energy_rotation():
    assert 3 <= len(rot.PRESETS) <= 5
    for pid, preset in rot.PRESETS.items():
        spec = rot.from_preset(pid)
        assert spec["preset"] == pid and 1 <= len(spec["slots"]) <= rot.MAX_SLOTS
        assert spec["cadence"] in rot.CADENCES and spec["archive"] in rot.ARCHIVE_POLICIES
        assert all(s["intent"] for s in spec["slots"])  # one-line intent per slot
    energy = rot.from_preset("rhythm_quiet_mixed")
    assert [s["name"] for s in energy["slots"]] == ["Rhythmic", "Quiet", "Mixed"]
    assert energy["cadence"] == "weekly" and energy["archive"] == "archive"
    view = rot.presets_view()
    assert {p["id"] for p in view} == set(rot.PRESETS) and all(p["slots"] for p in view)


def test_preset_overrides_and_unknown_preset():
    assert rot.from_preset("day_parts", cadence="daily", archive="replace")["cadence"] == "daily"
    with pytest.raises(ValueError, match="preset must be one of"):
        rot.from_preset("nope")


# ── validation ─────────────────────────────────────────────────────────────────────────────────

def test_validate_normalizes_a_custom_spec():
    spec = rot.validate_spec({"slots": [{"name": "  Cooking  ", "intent": "warm\nand loud"}, "Commute"],
                              "cadence": "Daily", "source_text": "one for cooking, one for the train"})
    assert spec["name"] == "Cooking / Commute" and spec["cadence"] == "daily" and spec["archive"] == "archive"
    assert spec["slots"][0] == {"name": "Cooking", "intent": "warm and loud", "mode": "mixed"}
    assert spec["source_text"].startswith("one for cooking") and spec["preset"] is None


@pytest.mark.parametrize("raw,needle", [
    ({"slots": [{"name": f"S{i}"} for i in range(7)]}, "at most 6 slots"),
    ({"slots": []}, "non-empty"),
    ({"slots": [{"name": "A"}], "cadence": "hourly"}, "cadence must be one of daily, weekly, manual"),
    ({"slots": [{"name": "A"}], "archive": "delete"}, "archive must be one of"),
    ({"slots": [{"name": "A"}, {"name": "a"}]}, "unique"),
    ({"slots": [{"name": "Archive"}]}, "reserved"),
    ({"slots": [{"name": "A", "mode": "chaos"}]}, "mode must be one of"),
    ({"slots": [{"intent": "no name"}]}, "needs a name"),
    ("not an object", "must be an object"),
])
def test_validate_rejects(raw, needle):
    with pytest.raises(ValueError, match=needle):
        rot.validate_spec(raw)


def test_set_get_clear_round_trip(db):
    assert rot.get_spec(db) is None and rot.status(db, NOW)["configured"] is False
    rot.set_spec(db, rot.from_preset("discovery_dial"), NOW)
    s = rot.status(db, NOW)
    assert s["configured"] and s["spec"]["name"] == "Comfort / Edge / Wild" and s["due"] and s["edition"] == 0
    assert "Wild (discover)" in s["description"]
    assert rot.clear_spec(db) is True and rot.get_spec(db) is None


def test_damaged_spec_reads_as_none(db):
    db.set_meta(rot.SPEC_KEY, json.dumps({"slots": [{"name": f"S{i}"} for i in range(9)]}))
    assert rot.get_spec(db) is None
    db.set_meta(rot.SPEC_KEY, "{not json")
    assert rot.get_spec(db) is None


def test_due_by_cadence():
    weekly, manual = rot.from_preset("rhythm_quiet_mixed"), rot.from_preset("then_now_next")
    assert rot.is_due(weekly, {"last_refresh_ts": 0}, NOW)  # never built
    assert not rot.is_due(weekly, {"last_refresh_ts": NOW - 3 * DAY}, NOW)
    assert rot.is_due(weekly, {"last_refresh_ts": NOW - 7 * DAY}, NOW)
    assert not rot.is_due(manual, {"last_refresh_ts": NOW - 300 * DAY}, NOW)
    assert not rot.is_due(None, {}, NOW)


# ── refresh ────────────────────────────────────────────────────────────────────────────────────

def test_first_edition_builds_one_managed_playlist_per_slot_without_overlap(db):
    fake = fake_spotify()
    rot.set_spec(db, rot.from_preset("discovery_dial"), NOW)
    out = rot.refresh(client(fake), db, size=8, now=NOW,
                      candidates={"edge": cands(0, 10), "Wild": cands(20, 12)})
    assert out["ok"] and out["edition"] == 1
    by_slot = {s["slot"]: s for s in out["slots"]}
    assert by_slot["Comfort"]["filled_from"] == "library" and by_slot["Edge"]["filled_from"] == "candidates"
    ids = [s["playlist"]["id"] for s in out["slots"]]
    assert len(set(ids)) == 3 and all(db.is_managed(i) for i in ids)
    uris = [u for i in ids for u in fake.playlists[i]]
    assert len(uris) == len(set(uris)) == 24  # no track in two slots of one edition
    assert all(s["playlist"]["action"] == "created" for s in out["slots"])
    assert not any(p["template"] == rot.ARCHIVE_TEMPLATE for p in db.playlists())  # nothing to archive yet
    status = rot.status(db, NOW)
    assert status["edition"] == 1 and not status["due"] and all(p["playlist_id"] for p in status["playlists"])
    create = [c for c in fake.calls if c.path == "/v1/me/playlists"]
    assert "edition 1" in create[0].json["description"] and create[0].json["public"] is False


def test_second_edition_archives_previous_tracks_into_one_managed_archive(db):
    fake = fake_spotify()
    rot.set_spec(db, rot.from_preset("discovery_dial"), NOW)
    first = rot.refresh(client(fake), db, size=6, now=NOW, candidates={"Edge": cands(0, 8), "Wild": cands(10, 8)})
    ids = {s["slot"]: s["playlist"]["id"] for s in first["slots"]}
    old = {slot: list(fake.playlists[pid]) for slot, pid in ids.items()}

    later = NOW + 7 * DAY
    second = rot.refresh(client(fake), db, size=6, now=later, candidates={"Edge": cands(30, 8), "Wild": cands(40, 8)})
    assert second["ok"] and second["edition"] == 2
    archive_id = rot.get_state(db)["archive_playlist_id"]
    assert archive_id and db.is_managed(archive_id)
    assert db.get_playlist(archive_id)["template"] == rot.ARCHIVE_TEMPLATE
    archived = list(fake.playlists[archive_id])
    assert set(archived) == {u for v in old.values() for u in v} and len(archived) == 18
    for slot, pid in ids.items():  # same playlists, new tracks
        assert {s["slot"]: s["playlist"]["id"] for s in second["slots"]}[slot] == pid
        assert not set(fake.playlists[pid]) & set(old[slot])
    # Archive is prepended (newest first) and created once.
    third = rot.refresh(client(fake), db, size=6, now=later + 7 * DAY,
                        candidates={"Edge": cands(50, 8), "Wild": cands(2, 8)})
    assert third["ok"]
    assert sum(1 for c in fake.calls if c.path == "/v1/me/playlists") == 4  # 3 slots + 1 archive
    assert fake.playlists[archive_id][-18:] == archived  # earlier editions stay below the newer ones
    assert len(fake.playlists[archive_id]) == 36


def test_replace_policy_never_creates_an_archive(db):
    fake = fake_spotify()
    rot.set_spec(db, rot.from_preset("then_now_next"), NOW)
    c = {"Now": cands(0, 8), "Next": cands(10, 8)}
    rot.refresh(client(fake), db, size=5, now=NOW, candidates=c)
    rot.refresh(client(fake), db, size=5, now=NOW + DAY, candidates={"Now": cands(20, 8), "Next": cands(30, 8)})
    assert rot.get_state(db)["archive_playlist_id"] is None
    assert not any(c.method == "POST" and c.path.endswith("/items") and (c.json or {}).get("position") == 0
                   for c in fake.calls)


def test_discover_slot_without_candidates_is_skipped_and_previous_edition_kept(db):
    fake = fake_spotify()
    rot.set_spec(db, rot.from_preset("discovery_dial"), NOW)
    out = rot.refresh(client(fake), db, size=5, now=NOW, candidates={"Edge": cands(0, 6)})
    wild = next(s for s in out["slots"] if s["slot"] == "Wild")
    assert out["ok"] and wild["ok"] is False and wild["skipped"] == "needs_candidates"
    assert "wild" not in rot.get_state(db)["slots"]


def test_only_named_slots_refresh_and_archive_dedupes(db):
    fake = fake_spotify()
    rot.set_spec(db, rot.from_preset("discovery_dial"), NOW)
    rot.refresh(client(fake), db, size=5, now=NOW, candidates={"Edge": cands(0, 6), "Wild": cands(10, 6)})
    wild_id = rot.get_state(db)["slots"]["wild"]
    out = rot.refresh(client(fake), db, size=5, now=NOW + DAY, slots=["wild"], candidates={"Wild": cands(20, 6)})
    assert [s["slot"] for s in out["slots"]] == ["Wild"] and out["edition"] == 2
    archive_id = rot.get_state(db)["archive_playlist_id"]
    first_archive = list(fake.playlists[archive_id])
    assert len(first_archive) == 5
    # Retry of the same slot: its current edition is archived, but uris already archived are not added twice.
    fake.playlists[wild_id] = fake.playlists[wild_id] + first_archive[:2]
    rot.refresh(client(fake), db, size=5, now=NOW + 2 * DAY, slots=["Wild"], candidates={"Wild": cands(30, 6)})
    assert len(fake.playlists[archive_id]) == len(set(fake.playlists[archive_id])) == 10
    with pytest.raises(ValueError, match="no slot named"):
        rot.refresh(client(fake), db, now=NOW, slots=["Nope"])
    with pytest.raises(ValueError, match="no slot named"):
        rot.refresh(client(fake), db, now=NOW, candidates={"Nope": cands(0, 3)})


def test_refresh_never_touches_a_playlist_loshify_did_not_create(db):
    fake = fake_spotify()
    rot.set_spec(db, rot.from_preset("discovery_dial"), NOW)
    user_pid = "userplaylist0000000000"
    fake.add_foreign_playlist(user_pid, [LIB[0]["uri"]])
    # A stale/forged state that points at the user's own playlist.
    db.set_meta(rot.STATE_KEY, json.dumps({"edition": 1, "last_refresh_ts": NOW - 8 * DAY,
                                           "slots": {"comfort": user_pid}, "archive_playlist_id": user_pid}))
    out = rot.refresh(client(fake), db, size=5, now=NOW, slots=["Comfort"])
    assert out["ok"]
    assert fake.playlists[user_pid] == [LIB[0]["uri"]]
    assert not any(user_pid in c.path for c in fake.calls)
    new_id = out["slots"][0]["playlist"]["id"]
    assert new_id != user_pid and db.is_managed(new_id)


def test_blocked_tracks_never_enter_a_slot(db):
    fake = fake_spotify()
    db.add_blocked(NEW[0]["uri"], "Fresh Song 0", "New Artist 0", NOW - DAY)
    rot.set_spec(db, {"slots": [{"name": "Only", "mode": "discover"}], "cadence": "manual"}, NOW)
    out = rot.refresh(client(fake), db, size=5, now=NOW, candidates={"Only": cands(0, 6)})
    pid = out["slots"][0]["playlist"]["id"]
    assert NEW[0]["uri"] not in fake.playlists[pid]
    assert {"title": "Fresh Song 0", "artist": "New Artist 0", "why": "blocked"} in out["slots"][0]["rejected"]


def test_refresh_without_spec_or_with_nothing_buildable(db):
    fake = fake_spotify()
    assert rot.refresh(client(fake), db, now=NOW)["ok"] is False
    rot.set_spec(db, {"slots": [{"name": "Only", "mode": "discover"}]}, NOW)
    out = rot.refresh(client(fake), db, now=NOW)
    assert out["ok"] is False and not fake.playlists and rot.get_state(db)["edition"] == 0


# ── tool contract ──────────────────────────────────────────────────────────────────────────────

class Env:
    def __init__(self, tmp_path):
        self.path = tmp_path / "data.db"
        self.fake = fake_spotify()
        self.rt = h.Runtime(store_factory=lambda: st.open_store(self.path),
                            client_factory=lambda: sp.SpotifyClient(self.fake, sleep=lambda s: None),
                            now=lambda: NOW)

    def call(self, args):
        return json.loads(h.TOOL_HANDLERS["loshify_rotation"](self.rt, args))


def test_tool_presets_preview_set_get_refresh(tmp_path):
    env = Env(tmp_path)
    p = env.call({"action": "presets"})
    assert p["ok"] and "own words" in p["ask"] and len(p["presets"]) == len(rot.PRESETS)
    custom = {"name": "Kitchen & Train", "cadence": "weekly",
              "slots": [{"name": "Kitchen", "intent": "loud and warm", "mode": "familiar"},
                        {"name": "Train", "intent": "headphones, new stuff", "mode": "discover"}],
              "source_text": "something for cooking and something new for the train"}
    prev = env.call({"action": "preview", "spec": custom})
    assert prev["ok"] and prev["saved"] is False and "Train (discover)" in prev["description"]
    assert env.call({"action": "get"})["configured"] is False  # preview saves nothing
    assert env.call({"action": "set", "spec": custom})["saved"] is True
    # Not onboarded yet → the setup step, no Spotify call.
    assert env.call({"action": "refresh"})["setup"] == "onboard" and not env.fake.calls
    db = st.open_store(env.path)
    db.set_meta("onboarded_at", NOW - DAY)
    db.close()
    out = env.call({"action": "refresh", "size": 5, "candidates": {"Train": cands(0, 6)}})
    assert out["ok"] and out["edition"] == 1 and [s["ok"] for s in out["slots"]] == [True, True]
    got = env.call({"action": "get"})
    assert got["configured"] and got["edition"] == 1 and got["spec"]["source_text"].startswith("something")


def test_tool_rejects_bad_specs_with_readable_errors(tmp_path):
    env = Env(tmp_path)
    bad = env.call({"action": "set", "spec": {"slots": [{"name": f"S{i}"} for i in range(8)], "cadence": "hourly"}})
    assert bad["ok"] is False and "at most 6 slots" in bad["error"] and "cadence" in bad["error"]
    assert env.call({"action": "preview"})["ok"] is False
    assert env.call({"action": "explode"})["ok"] is False
    assert env.call({"action": "set", "preset": "day_parts", "cadence": "daily"})["spec"]["cadence"] == "daily"
    assert env.call({"action": "clear"})["cleared"] is True


def test_setup_points_to_the_rotation_question(tmp_path):
    env = Env(tmp_path)
    env.fake.on("GET", "/v1/me/top/artists", reply(200, {"items": [{"name": "Lib Artist 0"}]}))
    out = json.loads(h.TOOL_HANDLERS["loshify_setup"](env.rt, {}))
    assert out["onboarded"] and "how do they like their music organized" in out["next"].lower()
    again = json.loads(h.TOOL_HANDLERS["loshify_setup"](env.rt, {}))
    # onboarding in progress: setup resumes it in order (portrait → anchors → calibration → rotation …)
    assert again["rotation"] == {"configured": False} and again["onboarding"]["next_step"] == "portrait"
    for step in ("portrait", "anchors", "calibration"):
        json.loads(h.TOOL_HANDLERS["loshify_onboard"](env.rt, {"action": "skip", "step": step}))
    third = json.loads(h.TOOL_HANDLERS["loshify_setup"](env.rt, {}))
    assert third["onboarding"]["next_step"] == "rotation" and "loshify_rotation" in third["next"]


def test_failed_slot_write_after_archiving_does_not_archive_twice_on_retry(db):
    fake = fake_spotify()
    rot.set_spec(db, {"slots": [{"name": "Only", "mode": "discover"}], "cadence": "weekly"}, NOW)
    first = rot.refresh(client(fake), db, size=5, now=NOW, candidates={"Only": cands(0, 6)})
    pid = first["slots"][0]["playlist"]["id"]
    old = list(fake.playlists[pid])
    fake.routes[("PUT", f"/v1/playlists/{pid}/items")] = [reply(500, {"error": {"status": 500}})]
    with pytest.raises(sp.SpotifyError):
        rot.refresh(client(fake), db, size=5, now=NOW + 7 * DAY, candidates={"Only": cands(10, 6)})
    archive_id = rot.get_state(db)["archive_playlist_id"]
    assert fake.playlists[archive_id] == old and fake.playlists[pid] == old  # copied first, nothing lost
    assert rot.get_state(db)["edition"] == 1
    fake.routes[("PUT", f"/v1/playlists/{pid}/items")] = [lambda c, p=pid: fake._replace(p, c)]
    again = rot.refresh(client(fake), db, size=5, now=NOW + 7 * DAY, candidates={"Only": cands(10, 6)})
    assert again["ok"] and again["edition"] == 2
    assert fake.playlists[archive_id] == old  # not archived a second time


def test_failed_track_write_reuses_created_playlist(db, monkeypatch):
    fake = fake_spotify()
    spotify = client(fake)
    rot.set_spec(db, {"slots": [{"name": "Only", "mode": "familiar"}], "archive": "replace"}, NOW)
    replace = spotify.replace_items
    monkeypatch.setattr(spotify, "replace_items", lambda *args: (_ for _ in ()).throw(RuntimeError("write failed")))
    with pytest.raises(RuntimeError, match="write failed"):
        rot.refresh(spotify, db, size=5, now=NOW)
    created_id = next(iter(fake.playlists))
    monkeypatch.setattr(spotify, "replace_items", replace)
    result = rot.refresh(spotify, db, size=5, now=NOW)
    assert result["slots"][0]["playlist"]["id"] == created_id
    assert result["slots"][0]["playlist"]["action"] == "refreshed"
    assert len(fake.playlists) == 1
