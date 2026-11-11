import json

import pytest

from core import store as st
from core import taste as ts
from core.store import DAY
from fakes import artist, track, uri

T0 = 1_800_000_000  # fixed clock


@pytest.fixture
def s():
    db = st.open_store(":memory:")
    yield db
    db.close()


def onboard(s, now=T0):
    ts.apply_seed(s, {}, now)


# ── decay golden table ──────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("kind,days,expected", [
    ("love", 0, 3.0), ("love", 30, 2.5227), ("love", 120, 1.5), ("love", 365, 0.3643),
    ("okay", 0, 1.0), ("okay", 30, 0.6300), ("okay", 120, 0.1575), ("okay", 365, 0.0036),
    ("down", 0, 2.0), ("down", 30, 1.7818), ("down", 120, 1.2599), ("down", 365, 0.4905),
    ("seed", 0, 10.0), ("seed", 30, 5.0), ("seed", 120, 0.625), ("seed", 365, 0.0022),
])
def test_decay_golden(kind, days, expected):
    base = ts.WEIGHT.get(kind, 10.0)
    assert ts.decay(base, days * DAY, ts.HALF_LIFE_DAYS[kind]) == pytest.approx(expected, abs=1e-4)


def test_decay_never_grows_for_future_timestamps():
    assert ts.decay(3.0, -5 * DAY, 120) == 3.0


# ── live scores and digest invariance ───────────────────────────────────────────────────────────

def test_love_scores_three_and_down_penalised(s):
    s.add_reaction(uri(1), "love", artists=["Tinariwen"], ts=T0)
    s.add_reaction(uri(2), "down", artists=["Synth Lord"], ts=T0)
    by = {x.display: x for x in ts.scores(s, T0)}
    assert by["Tinariwen"].score == pytest.approx(3.0)
    assert by["Synth Lord"].score == pytest.approx(-3.0)  # 2 * 1.5
    assert by["Tinariwen"].trend == "up" and by["Synth Lord"].trend == "down"


def test_digest_does_not_change_scores(s):
    s.add_reaction(uri(1), "love", artists=["A"], ts=T0)
    s.add_reaction(uri(2), "okay", artists=["A"], ts=T0 + DAY)
    s.add_reaction(uri(3), "down", artists=["B"], ts=T0 + 2 * DAY)
    now = T0 + 40 * DAY
    before = {x.artist: round(x.score, 6) for x in ts.scores(s, now)}
    rep = ts.digest(s, now)
    assert rep.digested == 3 and rep.loved == 1 and rep.blocked == 1
    after = {x.artist: round(x.score, 6) for x in ts.scores(s, now)}
    assert before == after
    # …and they keep decaying identically afterwards
    later = T0 + 200 * DAY
    assert ts.score_of(s, "A", later).score == pytest.approx(
        ts.decay(3, later - T0, 120) + ts.decay(1, later - T0 - DAY, 45), rel=1e-9)


def test_digest_waits_for_raw_window(s):
    s.add_reaction(uri(1), "love", artists=["A"], ts=T0)
    assert ts.digest(s, T0 + 29 * DAY).digested == 0
    assert ts.digest(s, T0 + 31 * DAY).digested == 1
    assert s.loved()[0]["track_uri"] == uri(1)
    assert s.get_meta_int("last_digest_at") == T0 + 31 * DAY


def test_digest_overflow_digests_oldest_early(s, monkeypatch):
    monkeypatch.setattr(ts, "RAW_OVERFLOW", 5)
    for i in range(8):
        s.add_reaction(uri(i), "okay", artists=["A"], ts=T0 + i)
    rep = ts.digest(s, T0 + 10)
    assert rep.digested == 3 and len(s.undigested()) == 5


def test_three_okays_promote_to_love(s):
    for i in range(3):
        s.add_reaction(uri(1), "okay", title="Song", artists=["A"], ts=T0 + i)
    live = ts.score_of(s, "A", T0 + 2)
    assert live.love == pytest.approx(3.0) and live.okay == 0
    rep = ts.digest(s, T0 + 31 * DAY)
    assert rep.promoted == 1 and rep.loved == 1
    assert s.loved()[0]["track_uri"] == uri(1)


def test_two_okays_stay_okay_and_track_not_kept(s):
    for i in range(2):
        s.add_reaction(uri(1), "okay", artists=["A"], ts=T0 + i)
    ts.digest(s, T0 + 31 * DAY)
    assert s.loved() == [] and s.count_blocked() == 0
    row = s.artist_row("A")
    assert row["okay"] > 0 and row["love"] == 0


def test_okays_on_one_track_are_digested_together(s):
    """A due okay drags the track's newer okays into the same digest, so promotion isn't split."""
    s.add_reaction(uri(1), "okay", artists=["A"], ts=T0)
    s.add_reaction(uri(1), "okay", artists=["A"], ts=T0 + 20 * DAY)
    s.add_reaction(uri(1), "okay", artists=["A"], ts=T0 + 25 * DAY)
    rep = ts.digest(s, T0 + 31 * DAY)
    assert rep.digested == 3 and rep.promoted == 1


def test_okays_spread_over_90_days_promote_across_digests(s):
    """The user's rule: three 👌 on one track = ❤️, whenever they happen (each 👌 digested before the next)."""
    for day in (0, 45, 90):
        s.add_reaction(uri(1), "okay", title="Song", artists=["A"], ts=T0 + day * DAY)
        if day < 90:
            ts.digest(s, T0 + (day + 31) * DAY)  # this 👌 is folded away before the next one
            assert s.undigested() == [] and s.loved() == []
            assert ts.track_verdicts(s, [uri(1)]) == {uri(1): "okay"}
    assert s.okay_counts([uri(1)]) == {uri(1): 2}
    # live, before the third 👌 is digested: already a ❤️ (keeper, recent loves, taste weight)
    assert ts.track_verdicts(s, [uri(1)]) == {uri(1): "love"}
    assert [l["uri"] for l in ts._recent_loves(s, 5)] == [uri(1)]
    assert ts.score_of(s, "A", T0 + 90 * DAY).recent == pytest.approx(3.0)
    rep = ts.digest(s, T0 + 121 * DAY)
    assert rep.promoted == 1 and rep.loved == 1
    assert [r["track_uri"] for r in s.loved()] == [uri(1)]
    assert s.okay_counts([uri(1)]) == {}  # promoted: the count starts over
    assert ts.track_verdicts(s, [uri(1)]) == {uri(1): "love"}
    # the ❤️ weight (3), decayed from the third 👌 (day 90) to the digest (day 121), like any ❤️
    assert s.artist_row("A")["love"] == pytest.approx(ts.decay(3.0, 31 * DAY, ts.HALF_LIFE_DAYS["love"]))


def test_okay_okay_then_down_is_not_promoted(s):
    s.add_reaction(uri(1), "okay", artists=["A"], ts=T0)
    ts.digest(s, T0 + 31 * DAY)
    s.add_reaction(uri(1), "okay", artists=["A"], ts=T0 + 40 * DAY)
    ts.digest(s, T0 + 71 * DAY)
    s.add_reaction(uri(1), "down", artists=["A"], ts=T0 + 80 * DAY)
    assert ts.track_verdicts(s, [uri(1)]) == {uri(1): "down"}
    ts.digest(s, T0 + 111 * DAY)
    assert s.loved() == [] and uri(1) in s.blocked_uris()
    assert s.okay_counts([uri(1)]) == {}  # the 👎 reset it
    # a later 👌 starts from zero: one 👌 after the 👎 is not a ❤️
    s.add_reaction(uri(1), "okay", artists=["A"], ts=T0 + 120 * DAY)
    assert ts.track_verdicts(s, [uri(1)]) == {uri(1): "down"}
    ts.digest(s, T0 + 151 * DAY)
    assert s.loved() == [] and uri(1) in s.blocked_uris()


def test_down_between_okays_in_one_window_resets_the_count(s):
    for day, kind in ((0, "okay"), (1, "okay"), (2, "down"), (3, "okay")):
        s.add_reaction(uri(1), kind, artists=["A"], ts=T0 + day * DAY)
    assert ts.track_verdicts(s, [uri(1)]) == {uri(1): "down"}
    rep = ts.digest(s, T0 + 40 * DAY)
    assert rep.promoted == 0 and s.loved() == [] and s.okay_counts([uri(1)]) == {uri(1): 1}


def test_digest_boundary_inside_the_three_okays(s):
    """Two 👌 digested together, the third inside the raw window: the live view and the next digest agree."""
    s.add_reaction(uri(1), "okay", artists=["A"], ts=T0)
    s.add_reaction(uri(1), "okay", artists=["A"], ts=T0 + DAY)
    ts.digest(s, T0 + 32 * DAY)
    assert s.okay_counts([uri(1)]) == {uri(1): 2} and s.loved() == []
    s.add_reaction(uri(1), "okay", artists=["A"], ts=T0 + 33 * DAY)
    assert ts.track_verdicts(s, [uri(1)]) == {uri(1): "love"}
    live = ts.score_of(s, "A", T0 + 40 * DAY).score
    rep = ts.digest(s, T0 + 40 * DAY, force=True)
    assert rep.promoted == 1 and [r["track_uri"] for r in s.loved()] == [uri(1)]
    assert ts.score_of(s, "A", T0 + 40 * DAY).score == pytest.approx(live)  # digest-invariant


def test_six_okays_make_two_loves_and_the_count_keeps_going(s):
    for i in range(7):
        s.add_reaction(uri(1), "okay", artists=["A"], ts=T0 + i * 20 * DAY)
        ts.digest(s, T0 + (i * 20 + 31) * DAY)
    assert s.loved()[0]["n"] == 2 and s.okay_counts([uri(1)]) == {uri(1): 1}


def test_okay_tally_survives_reopen_and_is_capped(tmp_path, monkeypatch):
    path = tmp_path / "data.db"
    db = st.open_store(path)
    db.add_reaction(uri(1), "okay", artists=["A"], ts=T0)
    ts.digest(db, T0 + 31 * DAY)
    db.close()
    db = st.open_store(path)
    assert db.okay_counts([uri(1), uri(2)]) == {uri(1): 1}
    monkeypatch.setattr(st, "OKAY_TALLY_CAP", 2)
    db.set_okay_counts({uri(2): 1, uri(3): 2}, T0 + 40 * DAY)
    assert db.okay_counts([uri(1), uri(2), uri(3)]) == {uri(2): 1, uri(3): 2}  # least recent dropped
    db.close()


def test_retract_before_digest_removes_signal(s):
    rid = s.add_reaction(uri(1), "down", artists=["A"], ts=T0)
    assert uri(1) in s.blocked_uris()
    s.retract_reaction(rid)
    assert ts.scores(s, T0) == [] and uri(1) not in s.blocked_uris()


def test_love_after_down_unblocks_track(s):
    s.add_reaction(uri(1), "down", artists=["A"], ts=T0)
    ts.digest(s, T0 + 31 * DAY)
    assert uri(1) in s.blocked_uris()
    s.add_reaction(uri(1), "love", artists=["A"], ts=T0 + 32 * DAY)
    ts.digest(s, T0 + 70 * DAY)
    assert uri(1) not in s.blocked_uris() and s.loved()[0]["track_uri"] == uri(1)


def test_avoid_rule(s):
    s.add_reaction(uri(1), "down", artists=["Noisy"], ts=T0)
    s.add_reaction(uri(2), "down", artists=["Noisy"], ts=T0)
    a = ts.score_of(s, "Noisy", T0)
    assert a.down == pytest.approx(4.0) and a.avoid
    s.add_reaction(uri(3), "love", artists=["Noisy"], ts=T0)
    assert not ts.score_of(s, "Noisy", T0).avoid  # love >= 1 lifts it


def test_artist_key_merges_the_prefix_and_case(s):
    s.add_reaction(uri(1), "love", artists=["The Beatles"], ts=T0)
    s.add_reaction(uri(2), "love", artists=["beatles"], ts=T0)
    assert len(ts.scores(s, T0)) == 1


# ── seed ────────────────────────────────────────────────────────────────────────────────────────

def test_seed_weights_follow_plan():
    w = ts.seed_weights(
        {"short_term": [artist("A")], "medium_term": [artist("A")], "long_term": [artist("B")]},
        {"short_term": [track("x", "C")]},
        [{"track": track(str(i), "D", i)} for i in range(5)],
        [{"track": track("r", "E")}])
    assert w["a"] == ("A", 5.0)
    assert w["b"][1] == 1.0 and w["c"][1] == 1.5
    assert w["d"][1] == pytest.approx(1.0) and w["e"][1] == 0.5


def test_apply_seed_marks_onboarded_and_decays(s):
    ts.apply_seed(s, {"a": ("A", 10.0)}, T0)
    assert s.onboarded_at == T0
    assert ts.score_of(s, "A", T0 + 30 * DAY).seed == pytest.approx(5.0)


def test_reseed_replaces_seed_keeps_reactions(s):
    ts.apply_seed(s, {"a": ("A", 10.0), "b": ("B", 4.0)}, T0)
    s.add_reaction(uri(1), "love", artists=["B"], ts=T0)
    ts.digest(s, T0 + 31 * DAY)
    ts.apply_seed(s, {"a": ("A", 2.0)}, T0 + 31 * DAY)
    now = T0 + 31 * DAY
    assert ts.score_of(s, "A", now).seed == pytest.approx(2.0)
    b = ts.score_of(s, "B", now)
    assert b.seed == 0 and b.love == pytest.approx(ts.decay(3, 31 * DAY, 120))
    assert s.onboarded_at == T0  # first onboarding date is kept


# ── summary, brief, hook gate ───────────────────────────────────────────────────────────────────

def _busy_store(s, n_artists=60, n_reactions=312):
    ts.apply_seed(s, {f"artist {i}": (f"Artist {i} " + "x" * 40, float(n_artists - i)) for i in range(n_artists)}, T0)
    for i in range(n_reactions):
        kind = ("love", "okay", "down")[i % 3]
        s.add_reaction(uri(i), kind, title=f"Track {i} " + "y" * 60, artists=[f"Artist {i % n_artists} " + "x" * 40],
                       ts=T0 + i * 3600)
    s.set_portrait("Desert blues at night, Ethio-jazz on Sundays, and a forgotten 2014 shoegaze phase. " * 4)
    for i in range(30):
        s.add_playlist(f"p{i}", f"Playlist {i} " + "z" * 80, "deep_dig", ts=T0 + i)


def test_summary_is_capped_and_ordered(s):
    _busy_store(s)
    out = ts.summary(s, T0 + 20 * DAY)
    assert out["bytes"] <= ts.SUMMARY_MAX_BYTES
    assert len(json.dumps(out, ensure_ascii=False, separators=(",", ":")).encode()) <= ts.SUMMARY_MAX_BYTES + 8
    scores = [a["score"] for a in out["top_artists"]]
    assert scores == sorted(scores, reverse=True) and out["onboarded"]


def test_summary_small_store_keeps_everything(s):
    onboard(s)
    s.add_reaction(uri(1), "love", title="Tamatant Tilay", artists=["Tinariwen"], ts=T0)
    out = ts.summary(s, T0)
    assert out["top_artists"] == [{"name": "Tinariwen", "score": 3.0, "trend": "up"}]
    assert out["recent_loves"][0]["title"] == "Tamatant Tilay"
    assert out["counts"]["pending_reactions"] == 1


def test_brief_cap_and_content(s):
    _busy_store(s)
    text = ts.brief(s, T0 + 20 * DAY)
    assert 0 < len(text) <= ts.BRIEF_MAX_CHARS
    assert text.startswith("Loshify taste (") and "leans →" in text


def test_brief_empty_before_onboarding(s):
    s.add_reaction(uri(1), "love", artists=["A"], ts=T0)
    assert ts.brief(s, T0) == ""


def test_brief_mentions_unseen_then_clears(s):
    onboard(s)
    s.add_reaction(uri(1), "love", title="Song", artists=["A"], ts=T0 + 10)
    assert "1 new reaction since you last looked" in ts.brief(s, T0 + 20)
    ts.mark_seen(s, T0 + 20)
    assert "new reaction" not in ts.brief(s, T0 + 20)


def test_brief_hides_blocked_from_loves(s):
    onboard(s)
    s.add_reaction(uri(1), "love", title="Was good", artists=["A"], ts=T0)
    s.add_reaction(uri(1), "down", title="Was good", artists=["A"], ts=T0 + 5)
    assert "Was good" not in ts.brief(s, T0 + 10)


@pytest.mark.parametrize("message,expected", [
    ("make me a playlist for tonight", True),
    ("What's on Spotify?", True),
    ("תשים לי שיר שקט", True),
    ("בא לי מוזיקה", True),
    ("תכין לי פלייליסט", True),
    ("והשירים האלה?", True),    # ו+ה prefixes stripped → שירים
    ("שלום, מה שלומך?", False),  # ש-prefixed words that are not music
    ("remind me to call mom", False),
    ("מה מזג האוויר מחר", False),
])
def test_music_lexicon(message, expected):
    assert ts.mentions_music(message) is expected


def test_hebrew_prefix_stripping():
    assert ts.mentions_music("בשיר הזה")
    assert ts.mentions_music("למוזיקה")
    assert ts.mentions_music("והפלייליסט")


def test_should_inject_gate(s):
    assert not ts.should_inject(s, "play music")  # not onboarded
    onboard(s)
    assert ts.should_inject(s, "play music")
    assert not ts.should_inject(s, "play music", enabled=False)
    assert not ts.should_inject(s, "book a dentist")
    s.add_reaction(uri(1), "love", artists=["A"], ts=T0 + 1)
    assert ts.should_inject(s, "book a dentist")  # unseen reactions open the gate
    ts.mark_seen(s, T0 + 2)
    assert not ts.should_inject(s, "book a dentist")


def test_forget_artist_removes_aggregate(s):
    ts.apply_seed(s, {"a": ("A", 5.0)}, T0)
    assert s.forget_artist("A") and ts.score_of(s, "A", T0) is None
