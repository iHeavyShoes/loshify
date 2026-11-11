import pytest

from core import spotify as sp
from fakes import AuthError, FakeTransport, ProviderNotSignedIn, reply, track


def client(t: FakeTransport, sleeps: list | None = None) -> sp.SpotifyClient:
    return sp.SpotifyClient(t, sleep=(sleeps.append if sleeps is not None else (lambda s: None)))


# ── URLs ─────────────────────────────────────────────────────────────────────────────────────────

def test_build_url_encodes_params_without_params_kwarg():
    url = sp.build_url("/search", {"q": 'track:"Hey Jude" artist:"The Beatles"', "type": "track", "limit": 5,
                                   "market": None})
    assert url.startswith("https://api.spotify.com/v1/search?")
    assert "market" not in url
    assert "q=track%3A%22Hey%20Jude%22%20artist%3A%22The%20Beatles%22" in url


def test_build_url_hebrew_and_list_params():
    url = sp.build_url("me/library", {"uris": ["spotify:track:a", "spotify:track:b"], "q": "שיר"})
    assert "uris=spotify%3Atrack%3Aa%2Cspotify%3Atrack%3Ab" in url
    assert "%D7%A9%D7%99%D7%A8" in url


@pytest.mark.parametrize("bad", ["https://evil.example/v1/me", "http://api.spotify.com/v1/me",
                                 "https://api.spotify.com:8443/v1/me", "https://api.spotify.com.evil.io/x"])
def test_build_url_refuses_foreign_next_links(bad):
    with pytest.raises(sp.SpotifyError) as err:
        sp.build_url(bad)
    assert err.value.setup == sp.SETUP_INTERNAL


def test_track_of_prefers_renamed_item_field():
    t = track("A", "B")
    assert sp.track_of({"item": t, "track": {"uri": "old"}}) is t
    assert sp.track_of({"track": t}) is t
    assert sp.track_of(t) is t
    assert sp.track_of({"item": None}) is None


# ── error mapping ────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("exc,setup", [
    (ProviderNotSignedIn("Run `hermes auth add loshify:spotify`"), sp.SETUP_SIGNIN),
    (AuthError("missing", code="oauth_client_id_missing"), sp.SETUP_CLIENT_ID),
    (AuthError("bad client", code="invalid_client"), sp.SETUP_APP),
    (AuthError("bad client", code="unauthorized_client"), sp.SETUP_APP),
    (AuthError("scopes", code="oauth_scope_not_granted"), sp.SETUP_APP),
    (AuthError("refresh", code="oauth_refresh_failed"), sp.SETUP_SIGNIN),
    (PermissionError("must be called from an installed plugin's code"), sp.SETUP_INTERNAL),
])
def test_exception_mapping(exc, setup):
    t = FakeTransport().on("GET", "/v1/me", exc)
    with pytest.raises(sp.SpotifyError) as err:
        client(t).me()
    assert err.value.setup == setup
    assert err.value.__cause__ is None and err.value.__context__ is None  # no chain back into Hermes frames


def test_network_error_has_no_setup_hint():
    t = FakeTransport().on("GET", "/v1/me", TimeoutError("slow"))
    with pytest.raises(sp.SpotifyError) as err:
        client(t).me()
    assert err.value.setup is None and err.value.reason == "TimeoutError"


@pytest.mark.parametrize("status,setup", [(401, sp.SETUP_SIGNIN), (403, sp.SETUP_PREMIUM_OR_USERS), (404, None)])
def test_status_mapping(status, setup):
    t = FakeTransport().on("GET", "/v1/me", reply(status, {"error": {"status": status, "message": "nope"}}))
    with pytest.raises(sp.SpotifyError) as err:
        client(t).me()
    assert err.value.status == status and err.value.setup == setup
    assert "nope" not in str(err.value)
    assert err.value.to_dict()["ok"] is False


# ── retries ──────────────────────────────────────────────────────────────────────────────────────

def test_429_honours_retry_after_then_succeeds():
    sleeps: list = []
    t = FakeTransport().on("GET", "/v1/me", reply(429, headers={"Retry-After": "2"}), reply(200, {"id": "u"}))
    assert client(t, sleeps).me() == {"id": "u"}
    assert sleeps == [2.0] and len(t.calls) == 2


def test_429_gives_up_after_two_retries():
    sleeps: list = []
    t = FakeTransport().on("GET", "/v1/me", reply(429, headers={"retry-after": "1"}))
    with pytest.raises(sp.SpotifyError) as err:
        client(t, sleeps).me()
    assert err.value.status == 429 and err.value.retry_after == 1.0
    assert sleeps == [1.0, 1.0] and len(t.calls) == 3


def test_429_wait_beyond_budget_is_not_slept():
    sleeps: list = []
    t = FakeTransport().on("GET", "/v1/me", reply(429, headers={"Retry-After": "30"}))
    with pytest.raises(sp.SpotifyError):
        client(t, sleeps).me()
    assert sleeps == [] and len(t.calls) == 1


def test_5xx_retried_once_for_idempotent_put():
    t = FakeTransport().on("PUT", "/v1/playlists/p1/items", reply(502), reply(200, {"snapshot_id": "s"}))
    assert client(t).replace_items("p1", ["spotify:track:1"]) == "s"
    assert len(t.calls) == 2


def test_5xx_never_retried_for_post_create_or_append():
    t = FakeTransport().on("POST", "/v1/me/playlists", reply(500))
    with pytest.raises(sp.SpotifyError):
        client(t).create_playlist("x")
    assert len(t.calls) == 1
    t2 = FakeTransport().on("POST", "/v1/playlists/p/items", reply(503))
    with pytest.raises(sp.SpotifyError):
        client(t2).add_items("p", ["spotify:track:1"])
    assert len(t2.calls) == 1


# ── endpoints ────────────────────────────────────────────────────────────────────────────────────

def test_search_caps_limit_at_ten():
    t = FakeTransport().on("GET", "/v1/search", reply(200, {"tracks": {"items": [track("a", "b")]}}))
    out = client(t).search_tracks('track:"a"', limit=50)
    assert t.calls[0].query["limit"] == "10" and t.calls[0].query["type"] == "track"
    assert len(out) == 1


def test_create_playlist_uses_me_playlists_private_by_default():
    t = FakeTransport().on("POST", "/v1/me/playlists", reply(201, {"id": "p1"}))
    assert client(t).create_playlist("Mix", "desc")["id"] == "p1"
    assert t.calls[0].json == {"name": "Mix", "description": "desc", "public": False}


def test_replace_items_chunks_over_100():
    uris = [f"spotify:track:{i}" for i in range(230)]
    t = (FakeTransport().on("PUT", "/v1/playlists/p/items", reply(200, {"snapshot_id": "s1"}))
         .on("POST", "/v1/playlists/p/items", reply(201, {"snapshot_id": "s2"}), reply(201, {"snapshot_id": "s3"})))
    assert client(t).replace_items("p", uris) == "s3"
    assert [len(c.json["uris"]) for c in t.calls] == [100, 100, 30]
    assert [c.method for c in t.calls] == ["PUT", "POST", "POST"]


def test_remove_items_uses_items_key():
    t = FakeTransport().on("DELETE", "/v1/playlists/p/items", reply(200, {"snapshot_id": "s"}))
    client(t).remove_items("p", ["spotify:track:1"], snapshot_id="old")
    assert t.calls[0].json == {"items": [{"uri": "spotify:track:1"}], "snapshot_id": "old"}


def test_library_calls_chunk_at_40_and_use_query_uris():
    t = FakeTransport().on("DELETE", "/v1/me/library", reply(200))
    client(t).library_remove([f"spotify:track:{i}" for i in range(41)])
    assert len(t.calls) == 2
    assert t.calls[0].query["uris"].count(",") == 39


def test_paginate_follows_next_on_spotify_only_and_caps():
    first = {"items": [{"item": track(str(i), "A", i)} for i in range(50)],
             "next": "https://api.spotify.com/v1/me/tracks?offset=50&limit=50"}
    second = {"items": [{"track": track(str(i), "A", i)} for i in range(50, 100)], "next": None}
    t = FakeTransport().on("GET", "/v1/me/tracks", reply(200, first), reply(200, second))
    rows = client(t).saved_tracks(max_items=80)
    assert len(rows) == 80 and all(r["track"]["type"] == "track" for r in rows)
    assert t.calls[1].query["offset"] == "50"


def test_paginate_refuses_foreign_next():
    page = {"items": [], "next": "https://evil.example/steal"}
    t = FakeTransport().on("GET", "/v1/me/tracks", reply(200, page))
    with pytest.raises(sp.SpotifyError):
        client(t).saved_tracks()


def test_playback_204_is_none():
    t = FakeTransport().on("GET", "/v1/me/player", reply(204))
    assert client(t).playback() is None


def test_top_validates_arguments():
    with pytest.raises(ValueError):
        client(FakeTransport()).top("albums")
    with pytest.raises(ValueError):
        client(FakeTransport()).top("artists", "forever")


def test_call_counter():
    t = FakeTransport().on("GET", "/v1/me", reply(200, {"id": "u"}))
    c = client(t)
    c.me()
    c.me()
    assert c.calls == 2


def test_default_transport_lives_in_this_plugin_file():
    """Hermes attributes the call to the plugin by the file of the frame that calls it."""
    import inspect
    from pathlib import Path

    assert Path(inspect.getsourcefile(sp._hermes_transport)).resolve() == Path(sp.__file__).resolve()
    assert sp.SpotifyClient()._transport is sp._hermes_transport


@pytest.mark.parametrize("status,path,detail,expected", [
    (403, "/v1/me/player/play", "Player command failed: Premium required", "Premium"),
    (403, "/v1/me/player/next", "Player command failed: Restriction violated", "didn't allow"),
    (404, "/v1/me/player/play", "NO_ACTIVE_DEVICE", "press play"),
    (401, "/v1/me/player", "", "hermes auth add loshify:spotify"),
    (429, "/v1/me/player", "", "wait a moment"),
    (500, "/v1/me/player", "", "didn't accept"),
])
def test_http_errors_are_friendly(status, path, detail, expected):
    error = sp._status_error("PUT", path, status, {"error": {"message": detail}}, None)
    assert expected.lower() in str(error).lower()
    assert "HTTP" not in str(error) and path not in str(error)
