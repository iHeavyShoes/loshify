import pytest

from core import reactions as rx
from core import spotify as sp
from core import store as st
from core.store import DAY
from fakes import FakeSpotify, cat_track

NOW = 1_800_000_000
T1 = cat_track("Track One", "Band", "r01")
T2 = cat_track("Track Two", "Band", "r02")
T3 = cat_track("Track Three", "Other", "r03")


@pytest.fixture
def db():
    s = st.open_store(":memory:")
    yield s
    s.close()


def client(fake):
    return sp.SpotifyClient(fake, sleep=lambda s: None)


def managed(fake, db, uris):
    c = client(fake)
    pid = c.create_playlist("Loshify test")["id"]
    c.replace_items(pid, uris)
    db.add_playlist(pid, "Loshify test", "deep_dig", ts=NOW)
    fake.calls.clear()
    return pid


def test_normalize_reaction_aliases():
    assert rx.normalize_reaction("heart") == "love" and rx.normalize_reaction("👌") == "okay"
    assert rx.normalize_reaction("Never") == "down"
    with pytest.raises(ValueError):
        rx.normalize_reaction("meh")


def test_track_snapshot_rejects_non_tracks():
    assert rx.track_snapshot(None) is None
    assert rx.track_snapshot({"item": {"type": "episode", "uri": "spotify:episode:x"}}) is None
    snap = rx.track_snapshot({"item": T1, "context": {"uri": "spotify:artist:" + "a" * 22}, "device": {"id": "d"}})
    assert snap["track_uri"] == T1["uri"] and snap["playlist_uri"] is None and snap["device_id"] == "d"


def test_down_on_managed_playlist_skips_removes_and_undo_restores_positions(db):
    fake = FakeSpotify([T1, T2, T3])
    pid = managed(fake, db, [T1["uri"], T2["uri"], T1["uri"], T3["uri"]])
    fake.play(T1, pid)
    snap = rx.track_snapshot(fake.player)
    out = rx.record(db, snap, "down", source="desktop", client=client(fake), now=NOW)
    assert out["undoable"] and out["actions"]["skip"]["status"] == "skipped"
    assert out["actions"]["removal"] == {"status": "removed", "playlist_uri": f"spotify:playlist:{pid}",
                                         "positions": [0, 2], "snapshot_id": out["actions"]["removal"]["snapshot_id"]}
    assert fake.playlists[pid] == [T2["uri"], T3["uri"]]
    assert T1["uri"] in db.blocked_uris()
    # the reaction was stored before any Spotify write
    assert db.get_reaction(out["id"])["reaction"] == "down"

    back = rx.undo(db, out["id"], client=client(fake), now=NOW + 30)
    assert back["ok"] and back["retracted"] and back["restored"]["status"] == "restored"
    assert fake.playlists[pid] == [T1["uri"], T2["uri"], T1["uri"], T3["uri"]]
    assert T1["uri"] not in db.blocked_uris() and db.get_reaction(out["id"]) is None
    assert rx.undo(db, out["id"], client=client(fake), now=NOW + 40)["ok"] is False  # once only


def test_down_never_touches_a_playlist_loshify_did_not_create(db):
    fake = FakeSpotify([T1, T2])
    fake.add_foreign_playlist("userplaylist0000000001", [T1["uri"], T2["uri"]])
    fake.play(T1, "userplaylist0000000001")
    out = rx.record(db, rx.track_snapshot(fake.player), "down", source="chat", client=client(fake), now=NOW)
    assert out["actions"]["removal"]["status"] == "not_managed"
    assert fake.playlists["userplaylist0000000001"] == [T1["uri"], T2["uri"]]
    assert not any(c.path.startswith("/v1/playlists/") for c in fake.calls)  # not even a read


def test_down_respects_settings_and_does_not_skip_a_changed_track(db):
    fake = FakeSpotify([T1, T2])
    pid = managed(fake, db, [T1["uri"]])
    fake.play(T1, pid)
    snap = rx.track_snapshot(fake.player)
    fake.play(T2, pid)  # user moved on before the 👎 landed
    out = rx.record(db, snap, "down", source="desktop", client=client(fake),
                    settings={"dislike_removes_from_playlist": False}, now=NOW)
    assert out["actions"]["skip"]["status"] == "track_changed"
    assert out["actions"]["removal"]["status"] == "off" and fake.playlists[pid] == [T1["uri"]]


def test_love_and_okay_record_without_actions_unless_love_saves(db):
    fake = FakeSpotify([T1])
    fake.on("PUT", "/v1/me/library", lambda c: __import__("fakes").reply(200))
    fake.play(T1)
    snap = rx.track_snapshot(fake.player)
    ok = rx.record(db, snap, "okay", source="chat", client=client(fake), now=NOW)
    assert "actions" not in ok and not ok["undoable"]
    love = rx.record(db, snap, "love", source="chat", client=client(fake), settings={"love_saves_to_liked": True},
                     now=NOW)
    assert love["actions"]["saved_to_liked"]["status"] == "saved"
    assert any(c.path == "/v1/me/library" and c.method == "PUT" for c in fake.calls)


def test_undo_of_love_just_retracts(db):
    rid = rx.record(db, {"track_uri": T1["uri"], "title": "x", "artists": ["Band"]}, "love", source="chat",
                    now=NOW)["id"]
    assert rx.undo(db, rid, now=NOW)["retracted"] is True


def test_undo_expires_after_a_day_and_unblocks_digested(db):
    fake = FakeSpotify([T1])
    pid = managed(fake, db, [T1["uri"]])
    fake.play(T1, pid)
    out = rx.record(db, rx.track_snapshot(fake.player), "down", source="desktop", client=client(fake), now=NOW)
    assert rx.undo(db, out["id"], client=client(fake), now=NOW + DAY + 5)["ok"] is True  # still retracts the row
    assert fake.playlists[pid] == []  # but the put-back window passed


def test_undo_keeps_record_when_spotify_fails(db):
    fake = FakeSpotify([T1])
    pid = managed(fake, db, [T1["uri"]])
    fake.play(T1, pid)
    out = rx.record(db, rx.track_snapshot(fake.player), "down", source="desktop", client=client(fake), now=NOW)
    fake.routes[("POST", f"/v1/playlists/{pid}/items")] = [__import__("fakes").reply(500)]
    assert rx.undo(db, out["id"], client=client(fake), now=NOW + 1)["ok"] is False
    fake.routes[("POST", f"/v1/playlists/{pid}/items")] = [lambda c: fake._add(pid, c)]
    assert rx.undo(db, out["id"], client=client(fake), now=NOW + 2)["restored"]["status"] == "restored"


def test_recent_feed_marks_loshify_playlists(db):
    fake = FakeSpotify([T1])
    pid = managed(fake, db, [T1["uri"]])
    db.add_reaction(T1["uri"], "love", artists=["Band"], playlist_uri=f"spotify:playlist:{pid}", ts=NOW, note="wow")
    db.add_reaction(T2["uri"], "okay", artists=["Band"], ts=NOW + 1)
    feed = rx.recent_feed(db, 0)
    assert feed[0]["from_loshify_playlist"] and feed[0]["note"] == "wow"
    assert "from_loshify_playlist" not in feed[1]
