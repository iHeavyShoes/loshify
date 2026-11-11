"""Offline fakes: a scripted ``credentialed_provider_request`` and Spotify object builders."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urlparse


@dataclass
class FakeResponse:
    status: int
    headers: dict = field(default_factory=dict)
    body: bytes = b""


def reply(status: int = 200, payload: Any = None, headers: Optional[dict] = None) -> FakeResponse:
    body = b"" if payload is None else json.dumps(payload).encode()
    return FakeResponse(status, dict(headers or {}), body)


class ProviderNotSignedIn(RuntimeError):
    """Same class name as Hermes's, which is what ``map_exception`` matches on."""


class AuthError(RuntimeError):
    def __init__(self, message: str, code: str = "") -> None:
        super().__init__(message)
        self.code = code


@dataclass
class Call:
    method: str
    url: str
    json: Any
    headers: dict

    @property
    def path(self) -> str:
        return urlparse(self.url).path

    @property
    def query(self) -> dict:
        return {k: v[0] for k, v in parse_qs(urlparse(self.url).query).items()}


class FakeTransport:
    """Routes ``(METHOD, path)`` to a queue of responses (or a callable). Records every call."""

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], list] = {}
        self.calls: list[Call] = []

    def on(self, method: str, path: str, *responses: Any) -> "FakeTransport":
        self.routes.setdefault((method.upper(), path), []).extend(responses)
        return self

    def __call__(self, method: str, url: str, *, json: Any = None, headers: Optional[dict] = None,
                 timeout: float = 0) -> FakeResponse:
        call = Call(method, url, json, dict(headers or {}))
        self.calls.append(call)
        queue = self.routes.get((method.upper(), call.path))
        if not queue:
            raise AssertionError(f"unexpected call {method} {url}")
        item = queue[0] if len(queue) == 1 else queue.pop(0)  # the last response repeats
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            return item(call)
        return item


def artist(name: str) -> dict:
    return {"name": name, "type": "artist", "id": name.lower().replace(" ", "")[:22]}


def track(title: str, artist_name: str, n: int = 0) -> dict:
    tid = (f"{abs(hash((title, artist_name, n))):022d}")[:22]
    return {"type": "track", "uri": f"spotify:track:{tid}", "id": tid, "name": title,
            "artists": [{"name": artist_name}], "album": {"name": f"{title} LP"}}


def uri(n: int) -> str:
    return "spotify:track:" + f"{n:022d}"


# ── catalog-backed fake Spotify (search + playlists + player) ─────────────────────────────────

import re as _re
import unicodedata as _ud

_STRICT = _re.compile(r'track:"(?P<t>[^"]*)"\s+artist:"(?P<a>[^"]*)"')


def _n(text: str) -> str:
    text = _ud.normalize("NFKD", str(text).casefold())
    text = "".join(c for c in text if not _ud.combining(c))
    return " ".join(_re.sub(r"[^\w\s]", " ", text).split())


def cat_track(title: str, artist_name: str, tid: str, *more_artists: str, album: str = "") -> dict:
    """A catalog track with a stable 22-char id."""
    tid = (tid + "0" * 22)[:22]
    return {"type": "track", "uri": f"spotify:track:{tid}", "id": tid, "name": title,
            "artists": [{"name": artist_name}] + [{"name": a} for a in more_artists],
            "album": {"name": album or f"{title} LP"}}


class FakeSpotify(FakeTransport):
    """A tiny Spotify: a track catalog with crude search, owned playlists, and a player state."""

    def __init__(self, catalog: list, *, me: Optional[dict] = None) -> None:
        super().__init__()
        self.catalog = list(catalog)
        self.playlists: dict[str, list] = {}
        self.player: Optional[dict] = None
        self._n_playlists = 0
        self.on("GET", "/v1/me", reply(200, me or {"id": "listener", "country": "IL", "product": "premium"}))
        self.on("GET", "/v1/search", self._search)
        self.on("POST", "/v1/me/playlists", self._create)
        self.on("GET", "/v1/me/player", lambda c: reply(200, self.player) if self.player else reply(204))
        self.on("POST", "/v1/me/player/next", reply(204))
        self.on("PUT", "/v1/me/player/play", reply(204))

    def _search(self, call):
        q = call.query.get("q", "")
        limit = int(call.query.get("limit", 10))
        m = _STRICT.fullmatch(q.strip())
        out = []
        if m:
            t, a = _n(m.group("t")), _n(m.group("a"))
            for tr in self.catalog:
                names = [_n(x["name"]) for x in tr["artists"]]
                if any(a == nm for nm in names) and t and t in _n(tr["name"]):
                    out.append(tr)
        else:
            words = set(_n(q).split())
            scored = []
            for i, tr in enumerate(self.catalog):
                hay = set(_n(tr["name"] + " " + " ".join(x["name"] for x in tr["artists"])).split())
                overlap = len(words & hay)
                if overlap:
                    scored.append((-overlap, i, tr))
            out = [tr for _, _, tr in sorted(scored, key=lambda x: (x[0], x[1]))]
        return reply(200, {"tracks": {"items": out[:limit]}})

    def _create(self, call):
        self._n_playlists += 1
        pid = f"pl{self._n_playlists:020d}"
        self.playlists[pid] = []
        self.on("PUT", f"/v1/playlists/{pid}/items", lambda c, p=pid: self._replace(p, c))
        self.on("POST", f"/v1/playlists/{pid}/items", lambda c, p=pid: self._add(p, c))
        self.on("DELETE", f"/v1/playlists/{pid}/items", lambda c, p=pid: self._remove(p, c))
        self.on("GET", f"/v1/playlists/{pid}/items", lambda c, p=pid: self._items(p))
        return reply(201, {"id": pid, "name": call.json["name"],
                           "external_urls": {"spotify": f"https://open.spotify.com/playlist/{pid}"}})

    def add_foreign_playlist(self, pid: str, uris: list) -> None:
        self.playlists[pid] = list(uris)
        self.on("GET", f"/v1/playlists/{pid}/items", lambda c, p=pid: self._items(p))
        self.on("DELETE", f"/v1/playlists/{pid}/items", lambda c, p=pid: self._remove(p, c))

    def _replace(self, pid, call):
        self.playlists[pid] = list(call.json["uris"])
        return reply(200, {"snapshot_id": f"s{len(self.calls)}"})

    def _add(self, pid, call):
        pos = call.json.get("position")
        uris = list(call.json["uris"])
        if pos is None:
            self.playlists[pid].extend(uris)
        else:
            self.playlists[pid][pos:pos] = uris
        return reply(201, {"snapshot_id": f"s{len(self.calls)}"})

    def _remove(self, pid, call):
        gone = {i["uri"] for i in call.json["items"]}
        self.playlists[pid] = [u for u in self.playlists[pid] if u not in gone]
        return reply(200, {"snapshot_id": f"s{len(self.calls)}"})

    def _items(self, pid):
        by_uri = {t["uri"]: t for t in self.catalog}
        items = [{"item": by_uri.get(u, {"type": "track", "uri": u, "name": "", "artists": []})}
                 for u in self.playlists[pid]]
        return reply(200, {"items": items, "next": None, "total": len(items)})

    def play(self, track: dict, playlist_id: Optional[str] = None) -> None:
        self.player = {"is_playing": True, "item": track, "device": {"id": "dev1"},
                       "context": {"uri": f"spotify:playlist:{playlist_id}"} if playlist_id else None}

    def searches(self) -> int:
        return sum(1 for c in self.calls if c.path == "/v1/search")
