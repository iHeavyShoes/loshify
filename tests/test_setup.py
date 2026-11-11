import pytest

from core import setup as su
from core import spotify as sp
from core import store as st
from fakes import AuthError, FakeSpotify, ProviderNotSignedIn, cat_track, reply

NOW = 1_800_000_000


@pytest.fixture
def db():
    s = st.open_store(":memory:")
    yield s
    s.close()


def client(fake):
    return sp.SpotifyClient(fake, sleep=lambda s: None)


@pytest.mark.parametrize("exc,step", [
    (ProviderNotSignedIn("no grant"), "signin"),
    (AuthError("x", code="oauth_client_id_missing"), "client_id"),
    (AuthError("x", code="invalid_client"), "app"),
])
def test_probe_maps_hermes_errors_to_next_steps(exc, step):
    fake = FakeSpotify([])
    fake.routes[("GET", "/v1/me")] = [exc]
    out = su.probe(client(fake))
    assert out["ready"] is False and out["step"] == step and out["next_step"] == su.NEXT_STEP[step]


def test_probe_403_and_ready(db):
    fake = FakeSpotify([])
    fake.routes[("GET", "/v1/me")] = [reply(403, {"error": {"message": "user not registered"}})]
    assert su.probe(client(fake))["step"] == "premium_or_users"
    fake.routes[("GET", "/v1/me")] = [reply(200, {"id": "u1", "country": "IL", "product": "free"})]
    out = su.probe(client(fake), db)
    assert out["ready"] and "Premium" in out["note"]
    assert db.get_meta("market") == "IL" and db.get_meta("user_id") == "u1"


def test_next_steps_name_the_redirect_and_signin_command_but_no_secret():
    assert su.REDIRECT_URI in su.NEXT_STEP["client_id"] and su.SIGNIN_COMMAND in su.NEXT_STEP["signin"]
    for text in su.NEXT_STEP.values():
        assert "token" not in text.lower() and "secret" not in text.lower()


def test_redirect_uri_has_the_fixed_port_spotify_requires():
    # Spotify refuses a port-less 127.0.0.1 ("This redirect URI is not secure"); 43827 is the official plugin's.
    assert su.REDIRECT_URI == "http://127.0.0.1:43828/callback" and su.REDIRECT_PORT == 43828


@pytest.mark.parametrize("exc,step,problem", [
    (AuthError("x", code="invalid_client"), "app", "refused"),
    (reply(403, {"error": {"message": "user not registered"}}), "premium_or_users", "not_listed"),
    (reply(503, {}), "retry", "no_answer"),
    (ProviderNotSignedIn("no grant"), "signin", None),          # a step to do, not a problem
    (AuthError("x", code="oauth_client_id_missing"), "client_id", None),
])
def test_probe_names_the_problem_and_its_plain_fix(exc, step, problem):
    fake = FakeSpotify([])
    fake.routes[("GET", "/v1/me")] = [exc] * 5
    out = su.probe(client(fake))
    assert out["step"] == step
    assert out.get("problem") == problem
    if problem:
        assert out["fix"] == su.FIXES[problem]


def test_probe_without_premium_is_ready_with_the_playback_note(db):
    fake = FakeSpotify([])
    fake.routes[("GET", "/v1/me")] = [reply(200, {"id": "u1", "product": "free"})]
    out = su.probe(client(fake), db)
    assert out["ready"] and out["problem"] == "no_premium" and out["note"] == su.FIXES["no_premium"]
    fake.routes[("GET", "/v1/me")] = [reply(200, {"id": "u1", "product": "premium"})]
    out = su.probe(client(fake), db)
    assert out["ready"] and "problem" not in out and "note" not in out


def test_each_known_failure_has_a_plain_language_fix():
    f = su.FIXES
    assert "exactly http://127.0.0.1:43828/callback" in f["redirect"] and 'not "localhost"' in f["redirect"]
    assert "not secure" in f["redirect"]
    assert "User Management" in f["not_listed"]
    assert "port on this computer is busy" in f["port_busy"] and "run the command again" in f["port_busy"]
    assert "no Premium" in f["no_premium"] and "play, pause and skip don't" in f["no_premium"]
    assert "Client ID" in f["refused"]
    assert "internet" in f["no_answer"]
    assert "Update Hermes" in f["hermes"]
    jargon = ("pkce", "oauth", "loopback", "grant", "token", "403", "401", " env")
    for key, text in f.items():
        assert not any(word in text.lower() for word in jargon), key


@pytest.mark.parametrize("raw,ok,error", [
    ("0123456789abcdef0123456789ABCDEF", True, None),
    ("  0123456789abcdef0123456789abcdef\n", True, None),
    ("", False, "empty"),
    ("   ", False, "empty"),
    (None, False, "empty"),
    (12345, False, "empty"),
    ("0123456789abcdef", False, "shape"),
    ("0123456789abcdef0123456789abcdeg", False, "shape"),
    ("LOSHIFY_SPOTIFY_CLIENT_ID=0123456789abcdef0123456789abcdef", False, "shape"),
])
def test_check_client_id(raw, ok, error):
    value, problem = su.check_client_id(raw)
    assert (value is not None) is ok
    assert problem == (su.CLIENT_ID_ERRORS[error] if error else None)
    if ok:
        assert value == raw.strip()


def test_chat_guide_walks_the_same_steps_and_starts_where_the_user_is():
    g = su.guide("client_id")
    assert [s["step"] for s in g["steps"]] == ["dashboard", "app", "users", "client_id", "connect", "check"]
    assert g["start_at"] == "dashboard"
    assert su.guide("signin")["start_at"] == "connect"
    assert su.guide("app")["start_at"] == "check"
    text = " ".join(s["do"] for s in g["steps"])
    for must in (su.DASHBOARD_URL, "App name Loshify", su.REDIRECT_URI, "Tick only Web API", su.WHY_LINE,
                 "User Management", su.USERS_WHY, "not the Client secret", su.SIGNIN_COMMAND):
        assert must in text, must
    assert set(g["fixes"]) == set(su.FIXES)


def test_client_id_env_matches_the_manifest():
    import yaml
    from pathlib import Path
    manifest = yaml.safe_load((Path(__file__).resolve().parents[1] / "plugin.yaml").read_text(encoding="utf-8"))
    assert manifest["oauth"][0]["client_id_env"] == su.CLIENT_ID_ENV


def history_fake(*, top_closed=False):
    a, b, c = cat_track("Old", "Tinariwen", "h01"), cat_track("New", "Khruangbin", "h02"), cat_track("X", "Mdou", "h03")
    fake = FakeSpotify([a, b, c])
    if top_closed:
        fake.on("GET", "/v1/me/top/artists", reply(403, {"error": {"message": "closed"}}))
        fake.on("GET", "/v1/me/top/tracks", reply(403, {"error": {"message": "closed"}}))
    else:
        fake.on("GET", "/v1/me/top/artists", lambda call: reply(200, {"items": [
            {"name": "Khruangbin"} if call.query["time_range"] == "short_term" else {"name": "Tinariwen"}]}))
        fake.on("GET", "/v1/me/top/tracks", reply(200, {"items": [b]}))
    saved = [{"added_at": "2019-05-01T00:00:00Z", "item": a}] * 3 + [{"added_at": "2026-10-01T00:00:00Z", "item": b}]
    fake.on("GET", "/v1/me/tracks", reply(200, {"items": saved, "next": None}))
    fake.on("GET", "/v1/me/player/recently-played", reply(200, {"items": [{"played_at": "x", "track": b}]}))
    return fake


def test_onboard_seeds_taste_within_budget_and_returns_portrait_facts(db):
    fake = history_fake()
    out = su.onboard(client(fake), db, NOW)
    assert out["spotify_calls"] <= su.CALL_BUDGET and out["skipped_sources"] == []
    assert db.onboarded_at == NOW and out["seeded_artists"] == 2
    facts = out["portrait_facts"]
    assert facts["eras"]["lately"] == ["Khruangbin"] and facts["eras"]["all_time"] == ["Tinariwen"]
    assert facts["recurring_albums"][0] == {"album": "Old LP", "artist": "Tinariwen", "saved_tracks": 3}
    assert facts["forgotten_loves"]["count"] == 3 and facts["forgotten_loves"]["examples"][0]["saved"] == 2019
    assert facts["listening_span"] == {"first_saved": 2019, "last_saved": 2026}


def test_onboard_survives_closed_top_endpoints(db):
    out = su.onboard(client(history_fake(top_closed=True)), db, NOW)
    assert len(out["skipped_sources"]) == 6 and db.onboarded_at == NOW
    assert out["seeded_artists"] == 2  # saved + recent still seed


def test_onboard_raises_on_signin_problem(db):
    fake = history_fake()
    fake.routes[("GET", "/v1/me/top/artists")] = [ProviderNotSignedIn("gone")]
    with pytest.raises(sp.SpotifyError):
        su.onboard(client(fake), db, NOW)
