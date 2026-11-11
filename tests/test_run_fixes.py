"""Fixes from the first real onboarding run (2026-11-09): top-up, discover fill, calibration 👎, Then reasons."""

import json
from datetime import datetime, timezone

import pytest

from core import curate as cur
from core import onboarding as ob
from core import rotations as rot
from core import spotify as sp
from core import store as st
from core.store import DAY
from fakes import FakeSpotify, cat_track, reply

NOW = 1_800_000_000
LIB = [cat_track(f"Old Song {i}", f"Lib Artist {i}", f"l{i:02d}") for i in range(40)]
NEW = [cat_track(f"Fresh Song {i}", f"New Artist {i}", f"n{i:02d}") for i in range(60)]


def iso(year, month=3):
    return datetime(year, month, 1, tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fake_spotify(extra=()):
    fake = FakeSpotify(LIB + NEW + list(extra))
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


def cands(start, n):
    return [{"title": f"Fresh Song {i}", "artist": f"New Artist {i}", "reason": f"because {i}"}
            for i in range(start, start + n)]


def only_discover(db):
    rot.set_spec(db, {"name": "Next only", "slots": [{"name": "Next", "mode": "discover"}], "cadence": "weekly",
                      "archive": "archive"}, NOW)


# ── item 1: a retry tops a short slot up; it never shrinks it ─────────────────────────────────

def test_retry_of_a_short_slot_keeps_its_tracks_and_adds_new_ones(db):
    fake = fake_spotify()
    only_discover(db)
    first = rot.refresh(client(fake), db, size=15, now=NOW, candidates={"Next": cands(0, 8)})
    slot = first["slots"][0]
    pid = slot["playlist"]["id"]
    assert first["edition"] == 1 and len(fake.playlists[pid]) == 8 and slot["short"] and slot["need"] == 7
    before = list(fake.playlists[pid])

    retry = rot.refresh(client(fake), db, size=15, now=NOW + 600, slots=["Next"], candidates={"Next": cands(20, 12)})
    row = retry["slots"][0]
    assert retry["ok"] and retry["top_up"] and retry["edition"] == 1  # same edition, not a new one
    assert row["kept"] == 8 and row["added"] == 7 and not row["short"]
    assert fake.playlists[pid][:8] == before and len(fake.playlists[pid]) == 15
    assert rot.get_state(db)["archive_playlist_id"] is None  # nothing archived by a retry


def test_retry_with_candidates_that_all_fail_keeps_the_slot_untouched(db):
    fake = fake_spotify()
    only_discover(db)
    first = rot.refresh(client(fake), db, size=15, now=NOW, candidates={"Next": cands(0, 8)})
    pid = first["slots"][0]["playlist"]["id"]
    before = list(fake.playlists[pid])
    writes = sum(1 for c in fake.calls if c.method in ("PUT", "POST") and pid in c.path)
    bad = [{"title": "No Such Song", "artist": "Nobody", "reason": "x"}] * 3
    retry = rot.refresh(client(fake), db, size=15, now=NOW + 60, slots=["Next"], candidates={"Next": bad})
    assert retry["slots"][0]["kept"] == 8 and retry["slots"][0]["added"] == 0
    assert fake.playlists[pid] == before
    assert sum(1 for c in fake.calls if c.method in ("PUT", "POST") and pid in c.path) == writes


def test_this_editions_tracks_are_not_served_recently_for_their_own_slot(db):
    fake = fake_spotify()
    only_discover(db)
    rot.refresh(client(fake), db, size=15, now=NOW, candidates={"Next": cands(0, 8)})
    # The agent resends the same 8 first, then new ones: the 8 are not rejected as served_recently.
    retry = rot.refresh(client(fake), db, size=15, now=NOW + 60, slots=["Next"], candidates={"Next": cands(0, 20)})
    row = retry["slots"][0]
    assert not any(r["why"] == "served_recently" for r in row["rejected"])
    assert row["kept"] == 8 and row["added"] == 7


def test_a_named_refresh_after_the_window_is_a_new_edition(db):
    fake = fake_spotify()
    only_discover(db)
    rot.refresh(client(fake), db, size=5, now=NOW, candidates={"Next": cands(0, 6)})
    later = rot.refresh(client(fake), db, size=5, now=NOW + rot.TOP_UP_WINDOW_S + 1, slots=["Next"],
                        candidates={"Next": cands(20, 6)})
    assert later["edition"] == 2 and not later.get("top_up")
    forced = rot.refresh(client(fake), db, size=5, now=NOW + rot.TOP_UP_WINDOW_S + 2, slots=["Next"],
                         candidates={"Next": cands(40, 6)}, top_up=False)
    assert forced["edition"] == 3


# ── item 2: Next (discover) avoids known artists without spending searches; mixed tops up ───────

def test_discover_defers_known_artists_and_never_searches_them_when_full(db):
    fake = fake_spotify()
    only_discover(db)
    for i in range(6):  # six artists the user already knows
        db.put_artist(f"lib artist {i}", f"Lib Artist {i}", 0.0, 0.0, 0.0, 2.0, NOW)
    known_first = [{"title": f"Old Song {i}", "artist": f"Lib Artist {i}", "reason": "you know it"} for i in range(6)]
    out = rot.refresh(client(fake), db, size=10, now=NOW, candidates={"Next": known_first + cands(0, 12)})
    row = out["slots"][0]
    queried = [c.query.get("q", "") for c in fake.calls if c.path == "/v1/search"]
    assert not any("Lib Artist" in q for q in queried[:10])  # new artists were resolved first
    picked = [t["artist"] for t in row["tracks"]]
    assert len(picked) == 10 and sum(a.startswith("Lib Artist") for a in picked) <= 2  # <= 20 %
    assert sum(1 for r in row["rejected"] if r["why"] == "familiar_quota") >= 4
    names = cur.known_artist_names(db, NOW)
    assert names[:1] and all(n.startswith("Lib Artist") for n in names)


def test_mixed_slot_with_too_few_candidates_is_filled_from_the_library(db):
    fake = fake_spotify()
    rot.set_spec(db, {"name": "M", "slots": [{"name": "Mixed", "mode": "mixed"}], "cadence": "weekly",
                      "archive": "archive"}, NOW)
    out = rot.refresh(client(fake), db, size=12, now=NOW, candidates={"Mixed": cands(0, 4)})
    row = out["slots"][0]
    assert row["filled_from"] == "candidates+library" and len(row["tracks"]) == 12 and not row["short"]


def test_parallel_prefetch_spends_the_same_budget_as_sequential(db):
    seq = cur.Resolver(client(fake_spotify()), workers=1)
    par = cur.Resolver(client(fake_spotify()), workers=4)
    picks = [(f"Fresh Song {i}", f"New Artist {i}") for i in range(10)]
    for r in (seq, par):
        r.prefetch(picks)
        assert all(r.resolve(t, a)[0] for t, a in picks)
    assert seq.searches == par.searches
    tight = cur.Resolver(client(fake_spotify()), workers=4, max_searches=3)
    tight.prefetch(picks)
    assert tight.searches == 3  # the budget is never overspent by concurrency


# ── item 3: a calibration 👎 records only (no skip, no removal) ─────────────────────────────────

PICKS = [{"title": f"Fresh Song {i}", "artist": f"New Artist {i}", "kind": k, "reason": "r"}
         for i, k in enumerate(["familiar", "familiar", "near", "near", "stretch"])]


def test_calibration_down_never_skips_even_if_that_track_is_playing(db):
    fake = fake_spotify()
    ob.start(db, NOW)
    ob.propose(client(fake), db, PICKS, NOW)
    fake.play(NEW[1])  # the user is previewing exactly that track
    fake.calls.clear()
    out = ob.react(db, 2, "down", source="desktop", client=client(fake),
                   settings={"dislike_skips": True, "dislike_removes_from_playlist": True}, now=NOW + 1)
    assert out["ok"] and out["reaction"] == "down" and out["actions"] == {"recorded_only": True}
    assert not any(c.path.startswith("/v1/me/player") for c in fake.calls)
    assert not any(c.method == "DELETE" for c in fake.calls)
    assert db.get_reaction(out["id"])["reaction"] == "down"  # learned through the normal path
    log = json.loads(db.get_meta("calibration_log"))
    assert log[-1]["artist"] == "New Artist 1" and log[-1]["reaction"] == "down" and log[-1]["source"] == "calibration"


# ── item 4: Then reasons are varied and specific ───────────────────────────────────────────────

def test_then_reasons_vary_and_tell_each_story_once():
    album = {"name": "Folk Singer"}
    saved = []
    for i in range(10):
        t = cat_track(f"Blues {i}", "Muddy Waters", f"b{i:02d}")
        t["album"] = album
        saved.append({"added_at": iso(2018, 3), "track": t})
    for i in range(10):
        saved.append({"added_at": iso(2019 + i % 3, 1 + i), "track": cat_track(f"Misc {i}", f"Band {i}", f"m{i:02d}")})
    ex = cur.Exclusions()
    picks, _ = cur.time_capsule(saved=saved, top_long=[], top_short=[], recent=[], loved=[], ex=ex, size=15)
    reasons = [p.reason for p in picks]
    assert len(set(reasons)) >= len(reasons) - 2  # not one template x 15
    assert sum("not heard lately" in r for r in reasons) <= 4
    assert sum("Folk Singer was a 2018 obsession" in r for r in reasons) == 1
    assert all(r and "placeholder" not in r for r in reasons)
