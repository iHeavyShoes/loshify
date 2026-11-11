"""Pick quality: the model's drafting notes never become picks, and a thin round retries once.

The bad outputs below are verbatim from a probe against a real provider (996e8c0, 4 tune-in runs):
run 1 sent one pick whose reason was "Wait, wrong title; skip"; run 2 mixed in "Wait: this is an album
title, so use…" and "Ignore: replaced below with a verified pick.".
"""

import pytest

from core import curate
from core import flow
from core import onboarding as ob
from core import writer
from core.text import is_meta_reason
from fakes import FakeSpotify, cat_track
from core import store as st
from core.store import DAY
from test_flow_tunein import LIB, NEW, NOW, FakeLlm, client, fake_spotify, finished_first_run

@pytest.fixture
def db():
    store = st.open_store(":memory:")
    yield store
    store.close()


RUN1 = "Wait, wrong title; skip"
RUN2_ALBUM = "Wait: this is an album title, so use…"
RUN2_IGNORE = "Ignore: replaced below with a verified pick."
PROBE_BAD = [RUN1, RUN2_ALBUM, RUN2_IGNORE]
ALBUM_TRACK = cat_track("Moth Light", "New Artist 77", "al01", album="Night Garden")
KINDS = ["familiar", "familiar", "near", "near", "stretch"] * 2


def pick(i, reason=None, kind=None, title=None, artist=None):
    return {"title": title or f"Fresh Song {i}", "artist": artist or f"New Artist {i}",
            "kind": kind or KINDS[i % 10], "reason": reason or f"same glow as your lead artists, take {i}"}


class ScriptedLlm(FakeLlm):
    """A cooperative FakeLlm, except *purpose* answers come from *script* in order (then cooperative)."""

    def __init__(self, purpose, script):
        super().__init__()
        self.purpose, self.script, self.instructions = purpose, list(script), []

    def __call__(self, purpose, instructions, data):
        if purpose == self.purpose:
            self.calls.append((purpose, data))
            self.instructions.append(instructions)
            return self.script.pop(0) if self.script else None
        return super().__call__(purpose, instructions, data)

    def asked(self):
        return sum(1 for p, _ in self.calls if p == self.purpose)


# ── the filter ─────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("reason", PROBE_BAD + ["placeholder", "TODO: find a better one", "TBD", "n/a", "Skip",
                                               "Skipped — not on Spotify", "Replaced with the radio edit",
                                               "A warm one; skip.", "(ignore)", ""])
def test_meta_reasons_are_rejected(reason):
    assert is_meta_reason(reason)


@pytest.mark.parametrize("reason", ["Worth the wait: the slow build pays off", "A song you never skip on long drives",
                                    "Replaces the synths with strings, same ache", "Na na na chorus you will hum",
                                    "Actually closer to your roots than it sounds", "Same hazy guitars, one step out"])
def test_real_reasons_pass(reason):
    assert not is_meta_reason(reason)


def test_writer_drops_probe_run2_picks_and_keeps_clean_ones():
    out = {"picks": [pick(1), pick(2, RUN2_ALBUM), pick(3), pick(4, RUN2_IGNORE), pick(5), pick(6), pick(7)]}
    llm = ScriptedLlm("tune_in", [out])
    res = writer.calibration_picks({}, llm, targets={"new_lanes": []})
    assert res["by"] == "llm" and res["attempts"] == 1 and llm.asked() == 1
    assert [p["title"] for p in res["picks"]] == [f"Fresh Song {i}" for i in (1, 3, 5, 6, 7)]
    assert not any(is_meta_reason(p["reason"]) for p in res["picks"])


def test_tune_in_prompt_demands_final_answers_like_calibration():
    llm = ScriptedLlm("tune_in", [{"picks": [pick(i) for i in range(10)]}])
    writer.calibration_picks({}, llm, targets={})
    text = llm.instructions[0]
    for phrase in ("Final answers only", "one user-facing sentence", "drafts, corrections", "album titles",
                   "skipped picks", "never an album or EP title"):
        assert phrase in text
    cal = ScriptedLlm("calibration", [{"picks": [pick(i) for i in range(10)]}])
    writer.calibration_picks({}, cal)
    assert text.split(". ", 1)[-1].endswith(cal.instructions[0].split("2 stretch. ", 1)[-1])  # same rules


# ── retry: writer level (too few clean picks) ──────────────────────────────────────────────────

def test_probe_run1_single_abandoned_pick_retries_once_and_recovers():
    llm = ScriptedLlm("tune_in", [{"picks": [pick(1, RUN1)]}, {"picks": [pick(i) for i in range(40, 50)]}])
    res = writer.calibration_picks({}, llm, targets={})
    assert res["by"] == "llm" and res["attempts"] == 2 and llm.asked() == 2 and len(res["picks"]) == 10
    retry_data = llm.calls[1][1]
    assert {"title": "Fresh Song 1", "artist": "New Artist 1"} in retry_data["already_tried"]
    assert "too few usable picks" in llm.instructions[1]


def test_retry_happens_only_once_then_falls_back():
    llm = ScriptedLlm("calibration", [{"picks": [pick(1, RUN1)]}, {"picks": [pick(2, RUN2_IGNORE)]},
                                      {"picks": [pick(i) for i in range(10)]}])
    top = [cat_track("Old Song 0", "Lib Artist 0", "l00")]
    res = writer.calibration_picks({}, llm, top_tracks=top)
    assert res["by"] == "fallback" and llm.asked() == 2 and res["picks"][0]["title"] == "Old Song 0"


def test_retry_tops_up_without_repeating_a_tried_song():
    first = {"picks": [pick(1), pick(2), pick(3, RUN2_ALBUM)]}
    second = {"picks": [pick(1), pick(3), pick(4), pick(5), pick(6)]}  # 1 kept, 3 dropped: neither again
    res = writer.calibration_picks({}, ScriptedLlm("tune_in", [first, second]), targets={})
    assert [p["title"] for p in res["picks"]] == [f"Fresh Song {i}" for i in (1, 2, 4, 5, 6)]


def test_no_llm_never_retries():
    res = writer.calibration_picks({}, None, top_tracks=[])
    assert res["by"] == "fallback" and res["attempts"] == 1


# ── resolver: album titles ─────────────────────────────────────────────────────────────────────

def test_resolver_names_an_album_title_but_still_resolves_a_title_track():
    fake = FakeSpotify(LIB + NEW + [ALBUM_TRACK, cat_track("Rumours", "New Artist 78", "al02", album="Rumours")])
    resolver = curate.Resolver(client(fake))
    assert resolver.resolve("Night Garden", "New Artist 77") == (None, "album_title")
    track, why = resolver.resolve("Rumours", "New Artist 78")
    assert why == "" and track["name"] == "Rumours"
    assert resolver.resolve("No Such Thing", "New Artist 77") == (None, "not_found")


def test_propose_rejects_meta_reasons_without_spending_a_search(db):
    fake = fake_spotify()
    before = fake.searches()
    out = ob.propose(client(fake), db, [pick(1, RUN1), pick(2, RUN2_IGNORE), pick(3)], NOW)
    assert out["ok"] and [t["title"] for t in out["calibration"]["tracks"]] == ["Fresh Song 3"]
    assert {r["why"] for r in out["rejected"]} == {"meta_reason"}
    assert fake.searches() - before <= 2  # only pick 3 was searched (strict, plus free at most)


# ── retry: flow level (too few picks survive resolution) ───────────────────────────────────────

def tune_in_ready(db, fake):
    finished_first_run(db, fake, FakeLlm())
    return NOW + 12 * DAY


def test_tune_in_probe_run1_recovers_with_one_retry(db):
    fake = fake_spotify()
    later = tune_in_ready(db, fake)
    llm = ScriptedLlm("tune_in", [{"picks": [pick(41, RUN1)]}, {"picks": [pick(i) for i in range(50, 60)]}])
    out = flow.tune_in_start(client(fake), db, llm, later, 10)
    assert out["ok"] and out["round"]["total"] == 5 and out["attempts"] == 2 and llm.asked() == 2
    assert not any(is_meta_reason(t["reason"]) for t in out["round"]["tracks"])


def test_tune_in_album_titles_count_as_missing_and_trigger_the_retry(db):
    fake = fake_spotify()
    fake.catalog.append(ALBUM_TRACK)
    later = tune_in_ready(db, fake)
    first = {"picks": [pick(41), pick(42), pick(43),
                       pick(77, "a lush, nocturnal record", "near", title="Night Garden", artist="New Artist 77")]}
    llm = ScriptedLlm("tune_in", [first, {"picks": [pick(i) for i in range(60, 66)]}])
    out = flow.tune_in_start(client(fake), db, llm, later, 10)
    assert out["ok"] and out["attempts"] == 2 and llm.asked() == 2  # 4 clean picks, 3 resolve → retry
    titles = [t["title"] for t in out["round"]["tracks"]]
    assert len(titles) == 5 and "Night Garden" not in titles
    assert {"Fresh Song 41", "Fresh Song 42", "Fresh Song 43"} <= set(titles)  # the first round is kept


def test_tune_in_clean_round_asks_once(db):
    fake = fake_spotify()
    later = tune_in_ready(db, fake)
    llm = ScriptedLlm("tune_in", [{"picks": [pick(i) for i in range(40, 50)]}])
    out = flow.tune_in_start(client(fake), db, llm, later, 10)
    assert out["ok"] and out["attempts"] == 1 and llm.asked() == 1 and "tune_in_retry" not in out["timing"]


def test_tune_in_gives_up_after_one_retry_without_faking_a_round(db):
    fake = fake_spotify()
    later = tune_in_ready(db, fake)
    llm = ScriptedLlm("tune_in", [{"picks": [pick(41, RUN1)]}, {"picks": [pick(42, RUN2_IGNORE)]},
                                  {"picks": [pick(i) for i in range(50, 60)]}])
    out = flow.tune_in_start(client(fake), db, llm, later, 10)
    assert out["ok"] is False and "curator" in out["error"] and llm.asked() == 2
    assert ob.calibration_view(db, "tune_in_round") is None


def test_first_calibration_with_only_bad_picks_falls_back_to_own_hits(db):
    fake = fake_spotify()
    llm = ScriptedLlm("calibration", [{"picks": [pick(1, RUN1)]}, {"picks": [pick(2, RUN2_ALBUM)]}])
    met = flow.meet(client(fake), db, llm, NOW)
    assert met["calibration_by"] == "fallback" and llm.asked() == 2
    assert met["calibration"]["total"] >= 1
    assert all(t["title"].startswith("Old Song") for t in met["calibration"]["tracks"])


# ── rotation candidates ────────────────────────────────────────────────────────────────────────

def test_rotation_candidates_drop_meta_reasons():
    spec = {"slots": [{"name": "Fresh", "intent": "new", "mode": "discover"}]}
    songs = [{"title": f"Fresh Song {i}", "artist": f"New Artist {i}", "reason": r}
             for i, r in enumerate(PROBE_BAD + ["a new voice in your lane", "placeholder"])]
    llm = ScriptedLlm("rotation", [{"slots": [{"name": "Fresh", "songs": songs}]}])
    out = writer.slot_candidates(spec, {}, llm)
    assert [c["title"] for c in out["candidates"]["Fresh"]] == ["Fresh Song 3"]
    assert "Final answers only" in llm.instructions[0]


def test_curation_rejects_meta_reasons_from_the_chat_path(db):
    fake = fake_spotify()
    cands = [{"title": "Fresh Song 1", "artist": "New Artist 1", "reason": RUN2_IGNORE},
             {"title": "Fresh Song 2", "artist": "New Artist 2", "reason": "a new voice in your lane"},
             {"title": "Fresh Song 3", "artist": "New Artist 3", "reason": ""}]
    picks, _unresolved, rejected = curate.from_candidates(cands, curate.Resolver(client(fake)),
                                                           ex=curate.exclusions(db, NOW), size=5, known=set(),
                                                           familiar_quota=False)
    assert [p.track["name"] for p in picks] == ["Fresh Song 2", "Fresh Song 3"]  # an empty reason stays legal
    assert {"title": "Fresh Song 1", "artist": "New Artist 1", "why": "meta_reason"} in rejected
