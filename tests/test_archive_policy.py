"""Curated Archive: keepers (❤️), probation with expiry, 👎 kept out, hard cap, managed-only removals."""

import json

import pytest

from core import rotations as rot
from core import spotify as sp
from core import store as st
from core import taste
from core.store import DAY
from fakes import reply
from loshify.tools import handlers as h
from test_rotations import NOW, cands, client, fake_spotify

ONE_SLOT = {"name": "Solo", "slots": [{"name": "Only", "mode": "discover"}], "cadence": "weekly"}
WEEK = 7 * DAY


@pytest.fixture
def db():
    store = st.open_store(":memory:")
    store.set_meta("onboarded_at", NOW - 30 * DAY)
    rot.set_spec(store, ONE_SLOT, NOW)
    yield store
    store.close()


class Roll:
    """Refreshes a one-slot rotation edition by edition (5 new tracks each) on one fake Spotify."""

    def __init__(self, db, **policy):
        self.db, self.fake, self.policy, self.n = db, fake_spotify(), policy, 0

    def __call__(self, at, **policy):
        out = rot.refresh(client(self.fake), self.db, size=5, now=at,
                          candidates={"Only": cands(self.n * 6 % 60, 6)}, **{**self.policy, **policy})
        assert out["ok"], out
        self.n += 1
        return out

    @property
    def slot(self):
        return list(self.fake.playlists[rot.get_state(self.db)["slots"]["only"]])

    @property
    def archive(self):
        return list(self.fake.playlists[rot.get_state(self.db)["archive_playlist_id"]])

    def records(self):
        return rot.get_state(self.db)["archive_tracks"]


def react(db, uri, kind, at):
    db.add_reaction(uri, kind, title="t", artists=["A"], ts=at)


# ── admission ──────────────────────────────────────────────────────────────────────────────────

def test_unreacted_and_okay_tracks_enter_on_probation_with_the_default_expiry(db):
    roll = Roll(db)
    roll(NOW)
    first = roll.slot
    react(db, first[0], "okay", NOW + DAY)
    out = roll(NOW + WEEK)
    assert roll.archive == first
    recs = roll.records()
    assert all(recs[u]["status"] == "probation" for u in first)
    assert all(recs[u]["expires"] == NOW + WEEK + 90 * DAY for u in first)
    assert out["archive"]["added"] == 5 and out["archive"]["keepers"] == 0 and out["archive"]["kept"] == 5


def test_loved_tracks_enter_as_keepers_and_never_age_out(db):
    roll = Roll(db)
    roll(NOW)
    first = roll.slot
    react(db, first[0], "love", NOW + DAY)
    roll(NOW + WEEK)
    assert roll.records()[first[0]] == {"status": "keeper", "added": NOW + WEEK, "expires": None}
    out = roll(NOW + WEEK + 400 * DAY)  # long after every probation ended
    assert first[0] in roll.archive and set(first[1:]).isdisjoint(roll.archive)
    assert out["archive"]["expired"] == 4 and out["archive"]["keepers"] == 1


def test_three_okays_promote_to_a_keeper(db):
    roll = Roll(db)
    roll(NOW)
    track = roll.slot[2]
    for i in range(3):
        react(db, track, "okay", NOW + i * 3600)
    roll(NOW + WEEK)
    assert roll.records()[track]["status"] == "keeper"


def test_loved_after_digest_is_still_a_keeper(db):
    roll = Roll(db)
    roll(NOW)
    track = roll.slot[1]
    react(db, track, "love", NOW + DAY)
    taste.digest(db, NOW + 2 * DAY, force=True)  # the raw reaction is folded into loved_tracks
    assert not db.undigested()
    roll(NOW + WEEK)
    assert roll.records()[track]["status"] == "keeper"


def test_okays_weeks_apart_across_digests_make_an_archive_keeper(db):
    """A track that comes round every few weeks: 👌, digest, 👌, digest, 👌 → keeper (3×👌 = ❤️)."""
    roll = Roll(db, probation_days=200)
    roll(NOW)
    track = roll.slot[3]
    react(db, track, "okay", NOW + DAY)
    roll(NOW + WEEK)  # archived on probation
    assert roll.records()[track]["status"] == "probation"
    for k, day in enumerate((40, 80)):
        taste.digest(db, NOW + (day - 1) * DAY)  # the earlier 👌 is folded away first
        assert not [r for r in db.undigested() if r["track_uri"] == track]
        react(db, track, "okay", NOW + day * DAY)
        out = roll(NOW + (day + 1) * DAY)
        assert roll.records()[track]["status"] == ("probation" if k == 0 else "keeper")
    assert out["archive"]["promoted"] == 1 and out["archive"]["keepers"] == 1


def test_okay_okay_then_down_leaves_the_archive(db):
    roll = Roll(db, probation_days=200)
    roll(NOW)
    track = roll.slot[3]
    react(db, track, "okay", NOW + DAY)
    roll(NOW + WEEK)
    taste.digest(db, NOW + 39 * DAY)
    react(db, track, "okay", NOW + 40 * DAY)
    taste.digest(db, NOW + 79 * DAY)
    react(db, track, "down", NOW + 80 * DAY)
    out = roll(NOW + 81 * DAY)
    assert track not in roll.records() and track not in roll.archive and out["archive"]["keepers"] == 0


def test_last_archive_counts_are_the_real_refresh_counts(db):
    roll = Roll(db)
    assert rot.last_archive(db) is None
    roll(NOW)
    assert rot.last_archive(db) is None  # the first edition archives nothing: no counts, nothing made up
    react(db, roll.slot[0], "love", NOW + DAY)
    out = roll(NOW + WEEK)
    last = rot.last_archive(db)
    assert {k: last[k] for k in rot.LAST_ARCHIVE_FIELDS} == {k: out["archive"][k] for k in rot.LAST_ARCHIVE_FIELDS}
    assert last["ts"] == NOW + WEEK and last["line"] == out["archive"]["line"]
    assert (last["added"], last["keepers"]) == (5, 1)
    assert rot.get_state(db)["archive_tracks"]  # the records are untouched by the extra field


def test_disliked_tracks_never_enter_the_archive(db):
    roll = Roll(db)
    roll(NOW)
    first = roll.slot
    react(db, first[0], "down", NOW + DAY)
    out = roll(NOW + WEEK)
    assert first[0] not in roll.archive and first[0] not in roll.records()
    assert len(roll.archive) == 4 and out["archive"]["disliked"] == 1 and out["archive"]["added"] == 4


# ── pruning on refresh ─────────────────────────────────────────────────────────────────────────

def test_expired_probation_ages_out_on_the_next_refresh_only(db):
    roll = Roll(db, probation_days=30)
    roll(NOW)
    first = roll.slot
    roll(NOW + WEEK)  # first edition archived, expires at NOW + WEEK + 30 d
    out = roll(NOW + WEEK + 29 * DAY)
    assert set(first) <= set(roll.archive) and out["archive"]["expired"] == 0
    out = roll(NOW + WEEK + 30 * DAY)
    assert set(first).isdisjoint(roll.archive) and out["archive"]["expired"] == 5
    assert set(first).isdisjoint(roll.records())


def test_probation_track_loved_since_is_promoted_and_survives_expiry(db):
    roll = Roll(db, probation_days=30)
    roll(NOW)
    first = roll.slot
    roll(NOW + WEEK)
    react(db, first[3], "love", NOW + 20 * DAY)  # loved while it sat in the Archive
    out = roll(NOW + 60 * DAY)
    assert out["archive"]["promoted"] == 1 and out["archive"]["expired"] == 4
    assert [u for u in roll.archive if u in first] == [first[3]]
    assert roll.records()[first[3]]["status"] == "keeper"


def test_track_disliked_after_archiving_is_removed(db):
    roll = Roll(db)
    roll(NOW)
    first = roll.slot
    roll(NOW + WEEK)
    react(db, first[4], "down", NOW + 8 * DAY)
    out = roll(NOW + 2 * WEEK)
    assert first[4] not in roll.archive and first[4] not in roll.records()
    assert out["archive"]["disliked"] == 1


def test_keeper_disliked_later_is_removed_and_love_after_down_unblocks(db):
    roll = Roll(db)
    roll(NOW)
    a, b = roll.slot[:2]
    react(db, a, "love", NOW + DAY)
    react(db, b, "down", NOW + DAY)
    react(db, b, "love", NOW + 2 * DAY)  # changed their mind: the latest decides
    roll(NOW + WEEK)
    assert roll.records()[a]["status"] == roll.records()[b]["status"] == "keeper"
    react(db, a, "down", NOW + 8 * DAY)
    roll(NOW + 2 * WEEK)
    assert a not in roll.archive and b in roll.archive


# ── cap ────────────────────────────────────────────────────────────────────────────────────────

def test_cap_evicts_the_oldest_non_keepers_first(db):
    roll = Roll(db, archive_cap=10)
    roll(NOW)
    e1 = roll.slot
    react(db, e1[0], "love", NOW + DAY)
    roll(NOW + WEEK)
    e2 = roll.slot
    roll(NOW + 2 * WEEK)
    e3 = roll.slot
    out = roll(NOW + 3 * WEEK)  # 15 archived > cap 10
    assert out["archive"]["evicted"] == 5 and out["archive"]["kept"] == 10
    assert e1[0] in roll.archive  # the keeper is the oldest, but never evicted
    assert set(e1[1:]).isdisjoint(roll.archive)  # oldest non-keepers left first (4 of edition 1 …)
    assert len(set(e2) & set(roll.archive)) == 4  # … then 1 of edition 2
    assert set(e3) <= set(roll.archive)  # the newest stay
    assert len(roll.archive) == 10 and "5 over the cap" in out["archive"]["line"]


def test_keepers_alone_over_the_cap_are_all_kept_and_reported(db):
    roll = Roll(db, archive_cap=10)
    for i in range(4):
        roll(NOW + i * WEEK)
        for u in roll.slot:
            react(db, u, "love", NOW + i * WEEK + DAY)
    roll(NOW + 4 * WEEK)
    out = roll(NOW + 5 * WEEK)
    assert out["archive"]["keepers"] == 20 and out["archive"]["kept"] == 20 + 5 - 5
    assert out["archive"]["over_cap"] is True and len(roll.archive) == 20
    assert "exceed the cap of 10" in out["archive"]["line"]


def test_policy_values_are_clamped(db):
    roll = Roll(db, archive_cap=3, probation_days="nonsense")
    roll(NOW)
    out = roll(NOW + WEEK)
    assert out["archive"]["cap"] == 10  # minimum
    assert roll.records()[roll.archive[0]]["expires"] == NOW + WEEK + 90 * DAY  # default on garbage


# ── safety ─────────────────────────────────────────────────────────────────────────────────────

def test_only_tracks_loshify_archived_are_ever_removed(db):
    roll = Roll(db, probation_days=10, archive_cap=10)
    roll(NOW)
    roll(NOW + WEEK)
    pid = rot.get_state(db)["archive_playlist_id"]
    mine = "spotify:track:" + "u" * 22  # the user dropped a track into the Archive by hand
    roll.fake.playlists[pid].append(mine)
    react(db, mine, "down", NOW + 8 * DAY)  # even a 👎'd one: not ours to remove here
    for i in range(2, 6):
        roll(NOW + i * WEEK)
    assert mine in roll.archive
    deletes = [c for c in roll.fake.calls if c.method == "DELETE"]
    assert deletes and all(i["uri"] != mine for c in deletes for i in c.json["items"])


def test_failed_prune_keeps_records_and_retries_next_refresh(db):
    roll = Roll(db, probation_days=10)
    roll(NOW)
    first = roll.slot
    roll(NOW + WEEK)
    pid = rot.get_state(db)["archive_playlist_id"]
    roll.fake.routes[("DELETE", f"/v1/playlists/{pid}/items")] = [reply(500, {"error": {"status": 500}})]
    out = roll(NOW + 30 * DAY)
    assert "error" in out["archive"] and out["archive"]["expired"] == 0
    assert set(first) <= set(roll.records())  # nothing forgotten while Spotify still has them
    roll.fake.routes[("DELETE", f"/v1/playlists/{pid}/items")] = [lambda c, p=pid: roll.fake._remove(p, c)]
    out = roll(NOW + 31 * DAY)
    assert out["archive"]["expired"] >= 5 and set(first).isdisjoint(roll.archive)


def test_prune_sign_in_failure_still_raises_for_the_setup_step(db):
    roll = Roll(db, probation_days=10)
    roll(NOW)
    roll(NOW + WEEK)
    pid = rot.get_state(db)["archive_playlist_id"]
    roll.fake.routes[("DELETE", f"/v1/playlists/{pid}/items")] = [reply(401, {"error": {"status": 401}})]
    with pytest.raises(sp.SpotifyError) as exc:
        roll(NOW + 30 * DAY)
    assert exc.value.setup


def test_retry_after_a_failed_slot_write_does_not_archive_twice(db):
    roll = Roll(db)
    roll(NOW)
    first = roll.slot
    pid = rot.get_state(db)["slots"]["only"]
    roll.fake.routes[("PUT", f"/v1/playlists/{pid}/items")] = [reply(500, {"error": {"status": 500}})]
    with pytest.raises(sp.SpotifyError):
        roll(NOW + WEEK)
    roll.fake.routes[("PUT", f"/v1/playlists/{pid}/items")] = [lambda c, p=pid: roll.fake._replace(p, c)]
    roll(NOW + WEEK)
    assert roll.archive == first


def test_legacy_state_without_records_becomes_probation(db):
    legacy = ["spotify:track:" + f"{i:022d}" for i in range(3)]
    db.set_meta(rot.STATE_KEY, json.dumps({"edition": 2, "last_refresh_ts": NOW, "slots": {},
                                           "archive_playlist_id": None, "archived_uris": legacy}))
    recs = rot.get_state(db)["archive_tracks"]
    assert set(recs) == set(legacy)
    assert all(r == {"status": "probation", "added": NOW, "expires": NOW + 90 * DAY} for r in recs.values())
    rot._save_state(db, rot.get_state(db))
    assert "archived_uris" not in json.loads(db.get_meta(rot.STATE_KEY))


def test_replace_policy_reports_no_archive(db):
    rot.set_spec(db, {**ONE_SLOT, "archive": "replace"}, NOW)
    roll = Roll(db)
    roll(NOW)
    assert "archive" not in roll(NOW + WEEK)


def test_status_shows_archive_counts(db):
    roll = Roll(db)
    roll(NOW)
    react(db, roll.slot[0], "love", NOW + DAY)
    roll(NOW + WEEK)
    assert rot.status(db, NOW + WEEK)["archive"] == {"tracks": 5, "keepers": 1, "probation": 4}


# ── verdicts + line ────────────────────────────────────────────────────────────────────────────

def test_track_verdicts_follow_the_taste_memory(db):
    u = ["spotify:track:" + f"v{i:021d}" for i in range(5)]
    db.add_loved(u[0], "t", "A", NOW - 90 * DAY)
    db.add_blocked(u[1], "t", "A", NOW - 90 * DAY)
    react(db, u[2], "okay", NOW)
    react(db, u[3], "love", NOW)
    react(db, u[3], "down", NOW + 1)  # latest decides
    react(db, u[1], "love", NOW)  # a love after an old 👎 un-blocks
    v = taste.track_verdicts(db, u)
    assert v == {u[0]: "love", u[1]: "love", u[2]: "okay", u[3]: "down"}
    assert taste.track_verdicts(db, []) == {}


def test_archive_line_reads_like_a_sentence():
    line = rot.archive_line({"added": 14, "expired": 9, "keepers": 3, "kept": 52})
    assert line == "Archive: 14 added, 9 aged out, 3 keepers (52 tracks)"
    line = rot.archive_line({"added": 1, "expired": 0, "evicted": 2, "disliked": 1, "keepers": 1, "kept": 10})
    assert line == "Archive: 1 added, 0 aged out, 2 over the cap, 1 👎 kept out, 1 keeper (10 tracks)"


# ── tool contract + config ─────────────────────────────────────────────────────────────────────

def test_tool_reports_counts_and_honors_config(tmp_path):
    path = tmp_path / "data.db"
    fake = fake_spotify()
    config = {"archive_probation_days": 14, "archive_cap": "12"}
    clock = {"now": NOW}
    rt = h.Runtime(store_factory=lambda: st.open_store(path),
                   client_factory=lambda: sp.SpotifyClient(fake, sleep=lambda s: None),
                   get_setting=lambda key, default: config.get(key, default), now=lambda: clock["now"])
    db = st.open_store(path)
    db.set_meta("onboarded_at", NOW - DAY)
    rot.set_spec(db, ONE_SLOT, NOW)
    db.close()

    def call(start):
        return json.loads(h.TOOL_HANDLERS["loshify_rotation"](rt, {"action": "refresh", "size": 5,
                                                                   "candidates": {"Only": cands(start, 6)}}))

    call(0)
    clock["now"] = NOW + WEEK
    out = call(10)
    a = out["archive"]
    assert {"kept", "added", "expired", "evicted", "keepers"} <= set(a)
    assert a["added"] == 5 and a["cap"] == 12
    assert out["say"] == a["line"] == "Archive: 5 added, 0 aged out, 0 keepers (5 tracks)"
    db = st.open_store(path)
    assert {r["expires"] for r in rot.get_state(db)["archive_tracks"].values()} == {NOW + WEEK + 14 * DAY}
    db.close()


def test_settings_defaults_match_the_manifest():
    import yaml
    from pathlib import Path

    manifest = yaml.safe_load((Path(__file__).resolve().parents[1] / "plugin.yaml").read_text(encoding="utf-8"))
    schema = manifest["config_schema"]
    assert schema["archive_probation_days"]["default"] == h.SETTING_DEFAULTS["archive_probation_days"] == 90
    assert schema["archive_cap"]["default"] == h.SETTING_DEFAULTS["archive_cap"] == 200
    assert schema["demo_mode"]["default"] is h.SETTING_DEFAULTS["demo_mode"] is False
    assert schema["demo_mode"]["type"] == "bool"
