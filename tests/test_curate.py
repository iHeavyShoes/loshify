from datetime import datetime, timezone

import pytest

from core import curate as cu
from core import spotify as sp
from core import store as st
from core import taste as ts
from core.store import DAY
from fakes import FakeSpotify, cat_track, reply

NOW = 1_800_000_000


def iso(ts_: int) -> str:
    return datetime.fromtimestamp(ts_, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── 40-candidate resolver fixture ──────────────────────────────────────────────────────────────
# (candidate title, candidate artist, expected catalog id or None). The catalog holds the real
# release names, with decorations and decoys around them.
CATALOG = [
    cat_track("Imagine - Remastered 2010", "John Lennon", "a01"),
    cat_track("Imagine", "Imagine Dragons Tribute Band", "a01x"),               # decoy artist
    cat_track("Amassakoul 'n' Ténéré", "Tinariwen", "a02"),
    cat_track("Maria Também", "Khruangbin", "a03"),
    cat_track("Texas Sun", "Khruangbin", "a04", "Leon Bridges"),
    cat_track("Get Lucky (feat. Pharrell Williams & Nile Rodgers)", "Daft Punk", "a05"),
    cat_track("Hallelujah", "Jeff Buckley", "a06"),
    cat_track("Hallelujah - Live", "Jeff Buckley", "a06l"),
    cat_track("Hallelujah", "Leonard Cohen", "a07"),
    cat_track("Journey in Satchidananda", "Alice Coltrane", "a08"),
    cat_track("הכל עובר חביבי", "מאיר אריאל", "a09"),
    cat_track("שיר לשלום", "להקת הנח\"ל", "a10"),
    cat_track("Yo Soy Tu Padre", "Bomba Estéreo", "a11"),
    cat_track("Beyoncé Interlude", "Beyoncé", "a12"),
    cat_track("Halo", "Beyoncé", "a13"),
    cat_track("Paranoid Android", "Radiohead", "a14"),
    cat_track("Karma Police", "Radiohead", "a15"),
    cat_track("Bloodbuzz Ohio", "The National", "a16"),
    cat_track("Heroes - 2017 Remaster", "David Bowie", "a17"),
    cat_track("Space Oddity - 2015 Remaster", "David Bowie", "a18"),
    cat_track("Nights", "Frank Ocean", "a19"),
    cat_track("Pink + White", "Frank Ocean", "a20"),
    cat_track("Redbone", "Childish Gambino", "a21"),
    cat_track("Motion Sickness", "Phoebe Bridgers", "a22"),
    cat_track("Cranes in the Sky", "Solange", "a23"),
    cat_track("Lua", "Bright Eyes", "a24"),
    cat_track("Ain't No Sunshine", "Bill Withers", "a25"),
    cat_track("Chan Chan", "Buena Vista Social Club", "a26"),
    cat_track("Agua de Beber", "Astrud Gilberto", "a27"),
    cat_track("Désolé", "Ali Farka Touré", "a28"),
    cat_track("Diaraby", "Ali Farka Touré", "a29", "Ry Cooder"),
    cat_track("Ya Rayah", "Rachid Taha", "a30"),
    cat_track("Lmuima", "Mdou Moctar", "a31"),
    cat_track("Tarhatazed", "Tinariwen", "a32"),
    cat_track("Strangers", "Ethel Cain", "a33"),
    cat_track("Kids", "MGMT", "a34"),
    cat_track("Dreams - 2004 Remaster", "Fleetwood Mac", "a35"),
    cat_track("Teardrop", "Massive Attack", "a36"),
    cat_track("Roygbiv", "Boards of Canada", "a37"),
]
CANDIDATES = [
    ("Imagine", "John Lennon", "a01"),                     # remaster suffix
    ("Amassakoul n Tenere", "Tinariwen", "a02"),           # accents + apostrophes
    ("Maria Tambem", "Khruangbin", "a03"),
    ("Texas Sun", "Leon Bridges", "a04"),                  # featured artist credited second
    ("Get Lucky", "Daft Punk", "a05"),                     # (feat. …)
    ("Hallelujah", "Jeff Buckley", "a06"),                 # studio beats live
    ("Hallelujah", "Leonard Cohen", "a07"),
    ("Journey In Satchidananda", "Alice Coltrane", "a08"),
    ("הכל עובר חביבי", "מאיר אריאל", "a09"),               # Hebrew
    ("שיר לשלום", "להקת הנחל", "a10"),                     # Hebrew, gershayim dropped
    ("Yo Soy Tu Padre", "Bomba Estereo", "a11"),
    ("Halo", "Beyonce", "a13"),
    ("Paranoid Android", "Radiohead", "a14"),
    ("Karma Police", "Radiohead", "a15"),
    ("Bloodbuzz Ohio", "National", "a16"),                 # "The" dropped
    ("Heroes", "David Bowie", "a17"),
    ("Space Oddity", "David Bowie", "a18"),
    ("Nights", "Frank Ocean", "a19"),
    ("Pink + White", "Frank Ocean", "a20"),
    ("Redbone", "Childish Gambino", "a21"),
    ("Motion Sickness", "Phoebe Bridgers", "a22"),
    ("Cranes In The Sky", "Solange", "a23"),
    ("Lua", "Bright Eyes", "a24"),
    ("Aint No Sunshine", "Bill Withers", "a25"),
    ("Chan Chan", "Buena Vista Social Club", "a26"),
    ("Agua de Beber", "Astrud Gilberto", "a27"),
    ("Desole", "Ali Farka Toure", "a28"),
    ("Diaraby", "Ali Farka Touré", "a29"),
    ("Ya Rayah", "Rachid Taha", "a30"),
    ("Lmuima", "Mdou Moctar", "a31"),
    ("Tarhatazed", "Tinariwen", "a32"),
    ("Strangers", "Ethel Cain", "a33"),
    ("Kids", "MGMT", "a34"),
    ("Dreams", "Fleetwood Mac", "a35"),
    ("Teardrop", "Massive Attack", "a36"),
    ("Roygbiv", "Boards of Canada", "a37"),
    # decoys: invented songs / wrong artist — must NOT resolve
    ("Imagine", "Imagine Dragons", None),
    ("Desert Rain Over Kidal", "Tinariwen", None),
    ("Hallelujah", "Rufus Wainwright", None),
    ("Karma Police", "Radiohead Tribute Orchestra", None),
]


def client(fake):
    return sp.SpotifyClient(fake, sleep=lambda s: None)


def test_fixture_has_40_candidates():
    assert len(CANDIDATES) == 40


def test_resolver_precision_and_hit_rate_on_40_candidates():
    fake = FakeSpotify(CATALOG)
    resolver = cu.Resolver(client(fake), max_searches=200)
    hits = wrong = 0
    real = [c for c in CANDIDATES if c[2]]
    for title, artist, expected in CANDIDATES:
        track, why = resolver.resolve(title, artist)
        got = track["id"] if track else None
        expected = (expected + "0" * 22)[:22] if expected else None
        if expected is None:
            assert track is None, f"decoy {title!r}/{artist!r} resolved to {track and track['name']}"
            assert why == "not_found"
        elif got == expected:
            hits += 1
        else:
            wrong += 1
    assert wrong == 0  # precision: never a different song
    assert hits / len(real) >= 0.85
    assert hits == len(real)  # current fixture: every real candidate resolves


def test_resolver_caches_and_respects_budget():
    fake = FakeSpotify(CATALOG)
    resolver = cu.Resolver(client(fake), max_searches=3)
    assert resolver.resolve("Halo", "Beyoncé")[0]
    assert resolver.resolve("halo", "BEYONCE")[0]  # cache hit (same song key)
    assert fake.searches() == 1
    assert resolver.resolve("Nope", "Nobody")[1] == "not_found"  # strict + free = 2 searches
    assert resolver.resolve("Kids", "MGMT") == (None, "budget")
    assert fake.searches() == 3


def test_resolver_stops_on_429_but_raises_setup_errors():
    fake = FakeSpotify(CATALOG)
    fake.routes[("GET", "/v1/search")] = [reply(429, {}, {"Retry-After": "30"})]
    resolver = cu.Resolver(client(fake))
    assert resolver.resolve("Halo", "Beyoncé") == (None, "rate_limited")
    assert resolver.exhausted
    fake.routes[("GET", "/v1/search")] = [reply(401, {"error": {"message": "expired"}})]
    with pytest.raises(sp.SpotifyError) as err:
        cu.Resolver(client(fake)).resolve("Halo", "Beyoncé")
    assert err.value.setup == "signin"


def test_best_match_prefers_plain_title_over_live_version():
    live = cat_track("Song - Live", "Band", "x1")
    studio = cat_track("Song", "Band", "x2")
    assert cu.best_match("Song", "Band", [live, studio]).track["id"].startswith("x2")


# ── selection rules ────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def db():
    s = st.open_store(":memory:")
    yield s
    s.close()


def onboard(db, artists):
    ts.apply_seed(db, {a.lower(): (a, 5.0) for a in artists}, NOW - DAY)


def test_deep_dig_enforces_new_artist_share_blocked_avoid_and_artist_cap(db):
    catalog = [cat_track(f"New {i}", f"Newcomer {i // 2}", f"n{i:02d}") for i in range(20)]
    catalog += [cat_track(f"Known {i}", "Khruangbin", f"k{i:02d}") for i in range(5)]
    catalog += [cat_track("Blocked Song", "Newcomer 9", "b01"), cat_track("Avoid Me", "Nickelback", "v01")]
    fake = FakeSpotify(catalog)
    onboard(db, ["Khruangbin"])
    db.add_reaction(catalog[25]["uri"], "down", title="Blocked Song", artists=["Newcomer 9"], ts=NOW - 10)
    for _ in range(3):  # make Nickelback an avoid-artist (down ≥ 4, no love)
        db.add_reaction(f"spotify:track:{'9' * 21}{_}", "down", artists=["Nickelback"], ts=NOW - 10)
    # familiar candidates first, so the quota (not just "playlist full") has to hold them back
    ordered = catalog[20:] + catalog[:20]
    cands = [{"title": t["name"], "artist": t["artists"][0]["name"], "reason": "r"} for t in ordered]
    cands.insert(8, {"title": "New 0", "artist": "Newcomer 0", "reason": "dup"})
    out = cu.curate(client(fake), db, "deep_dig", candidates=cands, size=10, now=NOW)
    assert out["ok"], out
    tracks = out["tracks"]
    assert len(tracks) == 10
    artists = [t["artist"] for t in tracks]
    assert all(artists.count(a) <= 2 for a in artists)
    assert sum(a == "Khruangbin" for a in artists) / len(tracks) <= 0.2
    assert "Nickelback" not in artists and "Blocked Song" not in [t["title"] for t in tracks]
    whys = [r["why"] for r in out["rejected"]]
    # 5 Khruangbin offered first; new artists filled all 10 slots, so every familiar one is held out
    assert whys.count("familiar_quota") == 5 and "Khruangbin" not in artists
    assert "blocked" in whys and "avoid_artist" in whys and "duplicate" in whys
    assert all(t["reason"] for t in tracks)


def test_deep_dig_admits_familiar_only_within_20_percent(db):
    catalog = [cat_track(f"New {i}", f"Newcomer {i}", f"n{i:02d}") for i in range(4)]
    catalog += [cat_track(f"Known {i}", f"Old Fave {i}", f"k{i:02d}") for i in range(4)]
    fake = FakeSpotify(catalog)
    onboard(db, [f"Old Fave {i}" for i in range(4)])
    cands = [{"title": t["name"], "artist": t["artists"][0]["name"]} for t in catalog]
    out = cu.curate(client(fake), db, "deep_dig", candidates=cands, size=10, now=NOW)
    familiar = [t for t in out["tracks"] if t["artist"].startswith("Old Fave")]
    assert len(out["tracks"]) == 5 and len(familiar) == 1  # 1/5 = 20%
    assert out["short"] is True


def test_mood_has_no_familiar_quota(db):
    catalog = [cat_track(f"Known {i}", f"Old Fave {i}", f"k{i:02d}") for i in range(6)]
    onboard(db, [f"Old Fave {i}" for i in range(6)])
    out = cu.curate(client(FakeSpotify(catalog)), db, "mood", mood="rainy",
                    candidates=[{"title": t["name"], "artist": t["artists"][0]["name"]} for t in catalog],
                    size=5, now=NOW)
    assert out["ok"] and len(out["tracks"]) == 5
    assert db.playlists()[0]["params"]["mood"] == "rainy"


def test_candidate_templates_need_candidates_and_unknown_template_fails(db):
    fake = FakeSpotify([])
    assert cu.curate(client(fake), db, "deep_dig", candidates=[], now=NOW)["ok"] is False
    assert cu.curate(client(fake), db, "nope", now=NOW)["ok"] is False
    assert cu.curate(client(fake), db, "deep_dig", candidates=[{"title": "x", "artist": "y"}], mode="x",
                     now=NOW)["ok"] is False


def test_nothing_resolved_writes_nothing(db):
    fake = FakeSpotify([])
    out = cu.curate(client(fake), db, "deep_dig", candidates=[{"title": "Ghost", "artist": "Nobody"}], now=NOW)
    assert out["ok"] is False and out["unresolved"][0]["why"] == "not_found"
    assert not fake.playlists and db.playlists() == []


# ── Time Capsule ───────────────────────────────────────────────────────────────────────────────

def capsule_fake(saved_rows, top_long=(), top_short=(), recent=()):
    fake = FakeSpotify([r["track"] for r in saved_rows])
    fake.on("GET", "/v1/me/tracks", reply(200, {"items": [{"added_at": r["added_at"], "item": r["track"]}
                                                          for r in saved_rows], "next": None}))

    def top(call):
        rng = call.query["time_range"]
        return reply(200, {"items": list(top_long if rng == "long_term" else top_short)})
    fake.on("GET", "/v1/me/top/tracks", top)
    fake.on("GET", "/v1/me/player/recently-played",
            reply(200, {"items": [{"played_at": iso(NOW), "track": t} for t in recent]}))
    return fake


def test_time_capsule_is_deterministic_spread_and_excludes_recent(db):
    rows = []
    for year in (2016, 2018, 2020, 2024):
        for i in range(6):
            t = cat_track(f"Song {year}-{i}", f"Artist {year}-{i % 3}", f"c{year}{i}")
            rows.append({"added_at": f"{year}-04-0{i + 1}T10:00:00Z", "track": t})
    heard = rows[0]["track"]  # 2016 song played lately
    blocked = rows[1]["track"]
    db.add_reaction(blocked["uri"], "down", title=blocked["name"], artists=[blocked["artists"][0]["name"]],
                    ts=NOW - 5)
    fake = capsule_fake(rows, top_long=[rows[7]["track"]], recent=[heard])
    onboard(db, ["Artist 2016-0"])
    out = cu.curate(client(fake), db, "time_capsule", size=8, now=NOW)
    assert out["ok"], out
    uris = [t["uri"] for t in out["tracks"]]
    assert heard["uri"] not in uris and blocked["uri"] not in uris
    years = [t["title"].split()[1][:4] for t in out["tracks"]]
    assert set(years) == {"2016", "2018", "2020", "2024"}  # every era represented
    assert years[:4] == ["2016", "2018", "2020", "2024"]   # round-robin, oldest first
    assert rows[7]["track"]["uri"] == uris[1]               # all-time favourite leads its year
    assert "all-time" in out["tracks"][1]["reason"]
    assert all(t["source"] == "library" for t in out["tracks"])
    assert out["searches"] == 0 and fake.searches() == 0
    # same inputs → same playlist
    db2 = st.open_store(":memory:")
    db2.add_reaction(blocked["uri"], "down", title=blocked["name"], artists=[blocked["artists"][0]["name"]],
                     ts=NOW - 5)
    onboard(db2, ["Artist 2016-0"])
    again = cu.curate(client(capsule_fake(rows, top_long=[rows[7]["track"]], recent=[heard])), db2,
                      "time_capsule", size=8, now=NOW)
    db2.close()
    assert [t["uri"] for t in again["tracks"]] == uris


def test_time_capsule_skips_recently_served_and_blocked_rereleases(db):
    a = cat_track("Old Love", "Band", "s01")
    remaster = cat_track("Old Love - 2011 Remaster", "Band", "s02")
    other = cat_track("Other", "Band", "s03")
    served = cat_track("Served", "Band2", "s04")
    rows = [{"added_at": "2015-01-01T00:00:00Z", "track": t} for t in (a, remaster, other, served)]
    db.add_reaction(a["uri"], "down", title="Old Love", artists=["Band"], ts=NOW - 5)
    db.record_served("plX", [(served["uri"], "")], ts=NOW - 10 * DAY)
    onboard(db, ["Band"])
    out = cu.curate(client(capsule_fake(rows)), db, "time_capsule", size=5, now=NOW)
    assert [t["title"] for t in out["tracks"]] == ["Other"]
    assert {r["why"] for r in out["rejected"]} == {"blocked", "served_recently"}


def test_time_capsule_survives_closed_top_endpoints(db):
    t = cat_track("Only", "Solo", "o01")
    fake = capsule_fake([{"added_at": "2019-01-01T00:00:00Z", "track": t}])
    fake.routes[("GET", "/v1/me/top/tracks")] = [reply(403, {"error": {"message": "closed"}})]
    onboard(db, ["Solo"])
    out = cu.curate(client(fake), db, "time_capsule", size=5, now=NOW)
    assert out["ok"] and out["tracks"][0]["title"] == "Only"


# ── writer ─────────────────────────────────────────────────────────────────────────────────────

def test_writer_creates_then_refresh_replaces_same_playlist(db):
    catalog = [cat_track(f"T{i}", f"A{i}", f"w{i:02d}") for i in range(12)]
    fake = FakeSpotify(catalog)
    onboard(db, ["Zed"])
    first = cu.curate(client(fake), db, "deep_dig", size=5, now=NOW,
                      candidates=[{"title": f"T{i}", "artist": f"A{i}", "reason": "x"} for i in range(6)])
    pid = first["playlist"]["id"]
    assert first["playlist"]["action"] == "created" and db.is_managed(pid)
    assert first["playlist"]["url"].endswith(pid)
    assert fake.playlists[pid] == [t["uri"] for t in first["tracks"]]
    create = next(c for c in fake.calls if c.path == "/v1/me/playlists")
    assert create.json["public"] is False and "Curated by Loshify" in create.json["description"]
    second = cu.curate(client(fake), db, "deep_dig", mode="refresh", size=5, now=NOW + 60,
                       candidates=[{"title": f"T{i}", "artist": f"A{i}"} for i in range(12)])
    assert second["playlist"]["id"] == pid and second["playlist"]["action"] == "refreshed"
    assert len(fake.playlists) == 1
    # served in the last 60 days are not served again
    assert not set(fake.playlists[pid]) & {t["uri"] for t in first["tracks"]}
    assert db.count_served() == 10


def test_refresh_of_unmanaged_playlist_is_refused(db):
    catalog = [cat_track("T", "A", "u01")]
    fake = FakeSpotify(catalog)
    fake.add_foreign_playlist("userplaylist0000000000", [])
    onboard(db, ["Zed"])
    with pytest.raises(sp.SpotifyError) as err:
        cu.curate(client(fake), db, "deep_dig", mode="refresh", playlist_id="userplaylist0000000000",
                  candidates=[{"title": "T", "artist": "A"}], now=NOW)
    assert err.value.reason == "not_managed"
    assert not any(c.method in ("PUT", "POST", "DELETE") for c in fake.calls)


def test_clamp_size():
    assert cu.clamp_size(None) == 25 and cu.clamp_size(1) == 5 and cu.clamp_size(500) == 50


def test_prefetch_propagates_caller_context(monkeypatch):
    import contextvars
    scope = contextvars.ContextVar("scope")
    scope.set("profile")
    seen = []
    resolver = cu.Resolver(None, workers=2)
    monkeypatch.setattr(resolver, "resolve", lambda title, artist: seen.append(scope.get(None)))
    resolver.prefetch([("one", "artist"), ("two", "artist")])
    assert seen == ["profile", "profile"]
