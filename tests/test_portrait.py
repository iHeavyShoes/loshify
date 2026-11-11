"""Generic portrait inference on synthetic listeners (nobody's real data, no per-user tuning)."""

from core import portrait as pt
from core import store as st
from core import writer

NOW = 1_794_268_800  # 2026-11-09


def A(name, *genres):
    return {"name": name, "genres": list(genres)}


def T(title, artist, album="LP"):
    return {"name": title, "artists": [{"name": artist}], "album": {"name": album}}


def S(year, title, artist, album=None, month=5):
    """A saved track; by default each on its own single (no accidental album binge)."""
    return {"added_at": f"{year}-{month:02d}-10T00:00:00Z", "track": T(title, artist, album or f"{title} (single)")}


def R(title, artist):
    return {"played_at": "2026-11-08T10:00:00Z", "track": T(title, artist)}


def claim(facts, kind):
    return next((c for c in facts["claims"] if c["kind"] == kind), None)


# ── 1. a save-burst user: one year with hundreds of saves, current listening elsewhere ─────────

def burst_user():
    saved = [S(2018, f"Blues {i}", "Muddy Waters" if i % 2 else "Jimi Hendrix", "Folk Singer", month=3) for i in range(240)]
    for y in (2017, 2019, 2020, 2021, 2022, 2023, 2024, 2025, 2026):
        saved += [S(y, f"Song {y}-{i}", f"Artist {y}-{i % 3}") for i in range(20)]
    saved += [S(2026, f"Now {i}", "Floating Points", "Cascade", month=9) for i in range(10)]
    top = {"short_term": [A("Floating Points", "electronica"), A("Four Tet", "electronica"), A("Bonobo", "electronica")],
           "medium_term": [A("Four Tet", "electronica"), A("Floating Points", "electronica"), A("Jon Hopkins", "ambient")],
           "long_term": [A("Radiohead", "art rock"), A("Muddy Waters", "blues"), A("Four Tet", "electronica")]}
    tracks = {"short_term": [T("Birth4000", "Floating Points"), T("Baby", "Four Tet")], "medium_term": [], "long_term": []}
    recent = [R("Birth4000", "Floating Points")] * 6 + [R("Baby", "Four Tet")] * 4 + [R("Kiara", "Bonobo")] * 2
    return top, tracks, saved, recent


def test_save_burst_is_a_phase_not_the_identity():
    facts = pt.portrait_facts(*burst_user(), now=NOW)
    years = facts["save_years"]
    assert "burst" in years["2018"] and "year_spike" in years["2018"]["burst"]
    assert years["2018"]["counted"] < years["2018"]["saves"] / 5  # capped and damped
    assert facts["phases"][0]["year"] == 2018
    lead = [x["artist"] for x in facts["lead"]]
    assert lead[0] in ("Floating Points", "Four Tet")
    assert "Muddy Waters" not in lead[:3] and "Jimi Hendrix" not in lead[:3]
    phase = claim(facts, "phase")
    assert phase and phase["confidence"] == "low" and phase["hedge"] == "looks like"
    assert claim(facts, "lead")["confidence"] == "high"
    # The same listener without the cap would have been led by 2018: proves the cap is doing the work.
    roots = [x["artist"] for x in facts["roots"]]
    assert "Muddy Waters" in roots or "Radiohead" in roots
    assert "phase" in facts["guidance"]


def test_month_import_and_album_binge_are_flagged():
    saved = [S(2021, f"Imp {i}", f"Band {i % 9}", month=4) for i in range(60)]  # one-month import
    saved += [S(2022, f"Track {i}", "Solo", "The Album") for i in range(12)] + [S(2022, f"x{i}", f"Y{i}") for i in range(8)]
    saved += [S(2023, f"z{i}", f"Z{i}") for i in range(15)]
    periods = pt.save_periods(saved)
    assert "month_import" in periods["years"]["2021"]["burst"]
    assert "album_binge" in periods["years"]["2022"]["burst"]
    assert "burst" not in periods["years"]["2023"]


# ── 2. a heavy listener with few saves ─────────────────────────────────────────────────────────

def heavy_listener():
    names = [f"Artist {i}" for i in range(20)]
    lanes = ("indie rock", "dream pop", "post-punk")
    top = {rng: [A(n, lanes[i % 3]) for i, n in enumerate(names)] for rng in pt.RANGES}
    tracks = {rng: [T(f"T{i}", names[i]) for i in range(20)] for rng in pt.RANGES}
    recent = [R(f"T{i % 10}", names[i % 10]) for i in range(50)]
    saved = [S(2024, "One", "Artist 3"), S(2025, "Two", "Artist 7")]
    return top, tracks, saved, recent


def test_heavy_listener_with_few_saves_is_led_by_listening_with_confidence():
    facts = pt.portrait_facts(*heavy_listener(), now=NOW)
    assert facts["evidence"]["strength"] == "rich"
    assert facts["lead"][0]["artist"] == "Artist 0" and facts["lead"][0]["confidence"] == "high"
    assert facts["phases"] == [] and claim(facts, "phase") is None
    assert claim(facts, "lead")["hedge"] == ""
    assert claim(facts, "range") is not None and claim(facts, "focus") is None  # three lanes, none dominant


# ── 3. a new account with little history ───────────────────────────────────────────────────────

def test_new_account_is_hedged_and_says_little():
    top = {"short_term": [A("Phoebe Bridgers")], "medium_term": [], "long_term": []}
    tracks = {"short_term": [T("Kyoto", "Phoebe Bridgers")], "medium_term": [], "long_term": []}
    facts = pt.portrait_facts(top, tracks, [S(2026, "Kyoto", "Phoebe Bridgers")], [R("Kyoto", "Phoebe Bridgers")], now=NOW)
    assert facts["evidence"]["strength"] == "thin"
    assert all(c["confidence"] != "high" for c in facts["claims"])
    assert claim(facts, "lead")["hedge"] == "looks like"
    assert claim(facts, "focus") is None and claim(facts, "range") is None
    assert "thin" in facts["guidance"]
    text = writer.fallback_portrait(facts)
    assert text.startswith("It looks like") and "sharpen" in text


def test_empty_account_still_gets_a_true_sentence():
    facts = pt.portrait_facts({}, {}, [], [], now=NOW)
    assert facts["claims"] == [] and facts["lead"] == []
    assert "not much listening history" in writer.fallback_portrait(facts)


def test_saves_only_claims_are_never_confident():
    saved = [S(y, f"s{y}{i}", "Only Saves") for y in (2020, 2021, 2022) for i in range(10)]
    facts = pt.portrait_facts({}, {}, saved, [], now=NOW)
    assert all(x["confidence"] == "low" for x in facts["lead"] + facts["roots"])


# ── 4. a single-genre listener ─────────────────────────────────────────────────────────────────

def test_single_genre_listener_gets_a_focus_claim():
    names = ["Bill Evans", "Coltrane", "Monk", "Mingus", "Miles Davis", "Chet Baker"] * 3
    top = {rng: [A(f"{n} {i}", "jazz", "hard bop") for i, n in enumerate(names)] for rng in pt.RANGES}
    tracks = {rng: [T(f"T{i}", f"{names[i]} {i}") for i in range(18)] for rng in pt.RANGES}
    recent = [R(f"T{i}", f"{names[i]} {i}") for i in range(18)] * 2
    facts = pt.portrait_facts(top, tracks, [], recent, now=NOW)
    focus = claim(facts, "focus")
    assert focus and focus["claim"] == "Mostly jazz" and focus["confidence"] in ("high", "medium")
    assert facts["genres"][0]["genre"] == "jazz" and facts["genres"][0]["share"] >= 0.5
    assert claim(facts, "range") is None


# ── the correction loop ────────────────────────────────────────────────────────────────────────

def test_corrections_lift_loves_drop_downs_and_soften_after_not_quite():
    top, tracks, saved, recent = heavy_listener()
    base = pt.portrait_facts(top, tracks, saved, recent, now=NOW)
    corr = {"not_quite": True, "love_artists": ["Khruangbin"], "down_artists": ["Artist 0"]}
    facts = pt.portrait_facts(top, tracks, saved, recent, now=NOW, corrections=corr)
    lead = [x["artist"] for x in facts["lead"]]
    assert "Artist 0" not in lead and "Khruangbin" in lead
    assert base["lead"][1]["confidence"] == "high"
    assert next(x for x in facts["lead"] if x["artist"] == "Artist 1")["confidence"] == "medium"
    assert facts["corrections"]["dropped"] == ["Artist 0"] and "Not quite" in facts["guidance"]


def test_corrections_from_store_newest_word_wins(tmp_path):
    db = st.open_store(":memory:")
    try:
        import json
        db.set_meta("portrait_feedback", json.dumps({"verdict": "not_quite"}))
        db.set_meta("anchors", json.dumps([{"kind": "artist", "artist": "Tinariwen"}]))
        db.set_meta("calibration_log", json.dumps([
            {"uri": "u1", "artist": "Tinariwen", "reaction": "down", "ts": 10},
            {"uri": "u2", "artist": "Bonobo", "reaction": "down", "ts": 11},
            {"uri": "u3", "artist": "Bonobo", "reaction": "love", "ts": 12},
            {"uri": "u4", "artist": "Air", "reaction": "okay", "ts": 13}]))
        corr = pt.corrections_from_store(db)
    finally:
        db.close()
    assert corr["not_quite"] is True
    assert corr["love_artists"] == ["Bonobo"] and corr["down_artists"] == ["Tinariwen"]
    assert corr["okay_artists"] == ["Air"]


def test_writer_uses_llm_text_and_falls_back_on_garbage():
    facts = pt.portrait_facts(*heavy_listener(), now=NOW)
    seen = {}

    def llm(purpose, instructions, data):
        seen.update(purpose=purpose, data=data)
        return {"portrait": "You live in indie rock and dream pop right now, with Artist 0 on repeat."}

    out = writer.write_portrait(facts, llm)
    assert out["by"] == "llm" and "Artist 0" in out["text"]
    assert seen["purpose"] == "portrait" and "save_years" not in seen["data"]  # facts only, no raw library
    assert writer.write_portrait(facts, lambda *a: {"portrait": "ok"})["by"] == "fallback"
    assert writer.write_portrait(facts, lambda *a: 1 / 0)["by"] == "fallback"
