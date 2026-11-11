import sqlite3

import pytest

from core import store as st
from fakes import uri


@pytest.fixture
def s():
    db = st.open_store(":memory:")
    yield db
    db.close()


def test_migration_sets_user_version_and_is_idempotent(tmp_path):
    path = tmp_path / "data.db"
    db = st.open_store(path)
    assert db.conn.execute("PRAGMA user_version").fetchone()[0] == st.SCHEMA_VERSION
    db.close()
    db = st.open_store(path)  # re-open: no re-run, no error
    tables = {r[0] for r in db.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"meta", "reactions", "artist_score", "loved_tracks", "blocked_tracks", "served", "playlists"} <= tables
    db.close()


def test_newer_schema_is_refused(tmp_path):
    path = tmp_path / "data.db"
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA user_version = {st.SCHEMA_VERSION + 1}")
    conn.close()
    with pytest.raises(RuntimeError, match="newer version"):
        st.open_store(path)


def test_meta_roundtrip_and_portrait_cap(s):
    s.set_meta("k", 5)
    assert s.get_meta("k") == "5" and s.get_meta_int("k") == 5
    s.set_meta("k", None)
    assert s.get_meta("k") is None
    text = s.set_portrait("word " * 200)
    assert len(text) <= st.PORTRAIT_MAX and s.get_meta("portrait") == text


def test_reaction_validation(s):
    with pytest.raises(ValueError):
        s.add_reaction(uri(1), "meh")
    with pytest.raises(ValueError):
        s.add_reaction("spotify:album:x", "love")
    with pytest.raises(ValueError):
        s.add_reaction(uri(1), "love", source="cron")


def test_reaction_amend_and_retract(s):
    rid = s.add_reaction(uri(1), "okay", title="T", artists=["A", "B"], ts=100)
    row = s.get_reaction(rid)
    assert row["artists"] == ["A", "B"] and row["digested"] == 0
    assert s.amend_reaction(rid, reaction="love", note="so good")
    assert s.get_reaction(rid)["reaction"] == "love"
    assert s.retract_reaction(rid)
    assert s.get_reaction(rid) is None
    assert not s.retract_reaction(rid)


def test_digested_reaction_cannot_be_retracted(s):
    rid = s.add_reaction(uri(1), "love", artists=["A"], ts=100)
    s.mark_digested([rid])
    assert not s.retract_reaction(rid)
    assert not s.amend_reaction(rid, reaction="down")


def test_loved_cap_drops_least_recent(s):
    for i in range(st.LOVED_CAP + 5):
        s.add_loved(uri(i), f"t{i}", "A", ts=1000 + i)
    loved = s.loved()
    assert len(loved) == st.LOVED_CAP
    assert loved[0]["track_uri"] == uri(st.LOVED_CAP + 4)
    assert uri(0) not in {r["track_uri"] for r in loved}


def test_loved_again_counts(s):
    s.add_loved(uri(1), "t", "A", ts=10)
    s.add_loved(uri(1), "t", "A", ts=20)
    row = s.loved()[0]
    assert row["n"] == 2 and row["last_ts"] == 20


def test_blocking_removes_from_loved_and_counts_fresh_downs(s):
    s.add_loved(uri(1), "t", "A", ts=10)
    s.add_blocked(uri(1), "t", "A", ts=20)
    assert s.loved() == []
    s.add_reaction(uri(2), "down", artists=["B"], ts=30)  # undigested 👎 blocks immediately
    assert s.blocked_uris() == {uri(1), uri(2)}
    assert s.unblock(uri(1)) and s.blocked_uris() == {uri(2)}


def test_served_cap_and_age(s):
    now = 10_000_000
    s.record_served("old", [(uri(0), "r")], ts=now - st.SERVED_MAX_AGE_S - 10)
    s.record_served("p", [(uri(i), "r") for i in range(1, st.SERVED_CAP + 50)], ts=now)
    assert s.count_served() == st.SERVED_CAP
    assert uri(0) not in s.served_since(0)


def test_served_since(s):
    s.record_served("p", [(uri(1), "a")], ts=100)
    s.record_served("p", [(uri(2), "b")], ts=200)
    assert s.served_since(150) == {uri(2)}


def test_managed_playlists(s):
    s.add_playlist("abc", "Mix", "deep_dig", {"size": 25}, ts=1)
    assert s.is_managed("abc") and s.is_managed("spotify:playlist:abc")
    assert not s.is_managed("spotify:playlist:someone-elses")
    assert s.touch_playlist("abc", ts=5)
    assert s.playlists()[0]["params"] == {"size": 25} and s.playlists()[0]["updated_ts"] == 5
    assert s.forget_playlist("abc") and not s.is_managed("abc")


def test_artist_rows_and_prune(s):
    s.put_artist("a", "A", 1, 0, 0, 0, 1)
    s.put_artist("b", "B", 0, 0, 0, 0.001, 1)
    assert s.prune_artists() == 1
    assert s.artist_row("A")["display"] == "A"
    assert s.forget_artist("a") and s.artist_rows() == []


def test_open_store_default_uses_hermes_plugin_db(monkeypatch, tmp_path):
    """path=None goes through Hermes's plugin_db("loshify") (stubbed here: no Hermes in the sandbox)."""
    import sys
    import types

    calls = []
    mod = types.ModuleType("plugins.plugin_storage")
    mod.plugin_db = lambda name: calls.append(name) or sqlite3.connect(tmp_path / "x.db")
    pkg = types.ModuleType("plugins")
    pkg.__path__ = []
    monkeypatch.setitem(sys.modules, "plugins", pkg)
    monkeypatch.setitem(sys.modules, "plugins.plugin_storage", mod)
    db = st.open_store()
    assert calls == ["loshify"]
    db.close()
