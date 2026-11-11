"""Spotify Web API request layer.

Every request goes through Hermes's ``credentialed_provider_request("spotify", ...)``: Hermes holds,
refreshes and attaches the user's OAuth grant (declared under ``oauth:`` in ``plugin.yaml``) and sends
it only to ``api.spotify.com``. This module never sees a token.

Hermes identifies the calling plugin by walking the stack for a frame inside the installed plugin
directory, so the call into Hermes must stay in this file (``_hermes_transport``), not in a lambda
defined elsewhere.

The transport is injectable so the whole layer is unit-testable offline (``tests/fakes.py``).
Only endpoints that are still open to Development-Mode apps after Spotify's February 2026 changes
are used (``/me/playlists``, ``/playlists/{id}/items``, ``/me/library``, search limit <= 10).
"""

from __future__ import annotations

import json as _json
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Iterator, Mapping, Optional
from urllib.parse import quote, urlencode, urlparse

API_BASE = "https://api.spotify.com/v1"
API_HOST = "api.spotify.com"
PROVIDER_ID = "spotify"  # the ``id`` of our ``oauth:`` entry in plugin.yaml

SEARCH_LIMIT_MAX = 10  # Feb-2026 dev-mode cap
PAGE_LIMIT = 50
PLAYLIST_WRITE_CHUNK = 100
LIBRARY_CHUNK = 40

MAX_429_RETRIES = 2
MAX_RETRY_WAIT_S = 8.0  # total sleep budget per request
DEFAULT_TIMEOUT_S = 20.0

# Setup states a caller can turn into a next step for the user.
SETUP_CLIENT_ID = "client_id"          # LOSHIFY_SPOTIFY_CLIENT_ID is not set in this profile
SETUP_SIGNIN = "signin"                # no (valid) grant: `hermes auth add loshify:spotify`
SETUP_APP = "app"                      # Spotify refused the client: redirect URI / app settings
SETUP_PREMIUM_OR_USERS = "premium_or_users"  # 403: not on the app's user list, or Premium needed
SETUP_INTERNAL = "internal"            # Hermes refused the call itself (caller/host checks)

_CLIENT_CODES = frozenset({"invalid_client", "unauthorized_client", "invalid_scope", "oauth_scope_not_granted"})
_IDEMPOTENT = frozenset({"GET", "PUT", "DELETE"})


class SpotifyError(RuntimeError):
    """A Spotify call that did not succeed. Carries no credential, by construction."""

    def __init__(self, message: str, *, status: Optional[int] = None, setup: Optional[str] = None,
                 retry_after: Optional[float] = None, reason: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.setup = setup
        self.retry_after = retry_after
        self.reason = reason

    def to_dict(self) -> dict:
        out: dict[str, Any] = {"ok": False, "error": str(self)}
        if self.status is not None:
            out["status"] = self.status
        if self.setup:
            out["setup"] = self.setup
        if self.retry_after is not None:
            out["retry_after"] = self.retry_after
        if self.reason:
            out["reason"] = self.reason
        return out


@dataclass(frozen=True)
class Response:
    """Transport-neutral response: what ``ProviderResponse`` exposes, minus anything else."""

    status: int
    headers: Mapping[str, str]
    body: bytes

    def json(self) -> Any:
        if not self.body:
            return None
        try:
            return _json.loads(self.body)
        except ValueError:
            return None


Transport = Callable[..., Any]  # (method, url, *, json=None, headers=None, timeout=...) -> has .status/.headers/.body


def _hermes_transport(method: str, url: str, *, json: Any = None, headers: Optional[Mapping[str, str]] = None,
                      timeout: float = DEFAULT_TIMEOUT_S) -> Any:
    """The only door to Hermes. Must live in a plugin file (caller detection walks the stack)."""
    from hermes_cli.plugin_provider_requests import credentialed_provider_request

    return credentialed_provider_request(PROVIDER_ID, method, url, json=json, headers=headers, timeout=timeout)


def map_exception(exc: BaseException) -> SpotifyError:
    """Translate a Hermes/transport exception into a ``SpotifyError`` with a setup hint.

    Matched by class name so this module imports nothing from Hermes at load time (the validate
    capability probe runs in a bare interpreter) and the mapping is testable without Hermes.
    """
    if isinstance(exc, SpotifyError):
        return exc
    name = type(exc).__name__
    text = str(exc)
    if name == "ProviderNotSignedIn":
        return SpotifyError(text or "Not signed in to Spotify.", setup=SETUP_SIGNIN, reason=name)
    if name == "AuthError":
        code = str(getattr(exc, "code", "") or "")
        if code == "oauth_client_id_missing":
            return SpotifyError("The Spotify Client ID is not set for this profile.", setup=SETUP_CLIENT_ID, reason=code)
        if code in _CLIENT_CODES:
            return SpotifyError("Spotify refused this app's sign-in (check the app's redirect URI and user list).",
                                setup=SETUP_APP, reason=code)
        return SpotifyError(f"Spotify sign-in failed ({code or 'unknown'}).", setup=SETUP_SIGNIN, reason=code)
    if isinstance(exc, PermissionError):
        return SpotifyError(f"Hermes refused the request: {text}", setup=SETUP_INTERNAL, reason=name)
    if type(exc).__module__.split(".")[0] in ("httpx", "httpcore") or isinstance(exc, (OSError, TimeoutError)):
        return SpotifyError(f"Network error talking to Spotify ({name}).", reason=name)
    return SpotifyError(f"Spotify request failed ({name}).", reason=name)


def _status_error(method: str, path: str, status: int, payload: Any, retry_after: Optional[float]) -> SpotifyError:
    detail = ""
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            detail = str(err.get("message") or "")
        elif isinstance(err, str):
            detail = str(payload.get("error_description") or err)
    setup = None
    if status == 401:
        setup = SETUP_SIGNIN
    elif status == 403:
        setup = SETUP_PREMIUM_OR_USERS
    logging.getLogger(__name__).warning("Spotify %s %s -> HTTP %s: %s", method, path, status, detail)
    if status == 401:
        msg = "Spotify sign-in expired. Run hermes auth add loshify:spotify again."
    elif status == 403 and "/me/player" in path:
        reason = str(payload.get("error", {}).get("reason") or "") if isinstance(payload, dict) and isinstance(payload.get("error"), dict) else ""
        if "PREMIUM" in (detail + " " + reason).upper():
            msg = "Playback control needs Spotify Premium."
        else:
            msg = "Spotify didn't allow that right now. Try again in a moment."
    elif status == 404 and ("NO_ACTIVE_DEVICE" in detail.upper() or "no device" in detail.lower() or "no active device" in detail.lower()):
        msg = "Open Spotify on any device and press play once."
    elif status == 429:
        msg = "Spotify asked us to wait a moment. Try again shortly."
    else:
        msg = "Spotify didn't accept that, try again."
    return SpotifyError(msg, status=status, setup=setup, retry_after=retry_after)


def _retry_after(headers: Mapping[str, str]) -> Optional[float]:
    for key, value in (headers or {}).items():
        if key.lower() == "retry-after":
            try:
                return max(0.0, float(value))
            except (TypeError, ValueError):
                return None
    return None


def build_url(path_or_url: str, params: Optional[Mapping[str, Any]] = None) -> str:
    """Absolute API URL. ``credentialed_provider_request`` has no ``params`` kwarg, so encode here.

    Absolute URLs (``next`` links from Spotify) are accepted only for ``https://api.spotify.com``.
    """
    if path_or_url.startswith("https://") or path_or_url.startswith("http://"):
        parsed = urlparse(path_or_url)
        if parsed.scheme != "https" or parsed.hostname != API_HOST or parsed.port not in (None, 443):
            raise SpotifyError(f"Refusing a non-Spotify URL: {parsed.hostname or path_or_url!r}",
                               setup=SETUP_INTERNAL)
        url = path_or_url
    else:
        url = API_BASE + ("" if path_or_url.startswith("/") else "/") + path_or_url
    clean = {k: (",".join(map(str, v)) if isinstance(v, (list, tuple)) else v)
             for k, v in (params or {}).items() if v is not None}
    if not clean:
        return url
    return url + ("&" if "?" in url else "?") + urlencode(clean, quote_via=quote)


def track_of(entry: Any) -> Optional[dict]:
    """The track inside a saved/playlist/recent item: ``item`` (Feb-2026 name), then legacy ``track``."""
    if not isinstance(entry, dict):
        return None
    for key in ("item", "track"):
        value = entry.get(key)
        if isinstance(value, dict):
            return value
    return entry if entry.get("type") == "track" or str(entry.get("uri", "")).startswith("spotify:track:") else None


def chunks(items: list, size: int) -> Iterator[list]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


class SpotifyClient:
    """Thin client. ``request`` returns parsed JSON (``None`` for empty bodies) or raises ``SpotifyError``."""

    def __init__(self, transport: Optional[Transport] = None, *, sleep: Callable[[float], None] = time.sleep,
                 timeout: float = DEFAULT_TIMEOUT_S) -> None:
        self._transport = transport or _hermes_transport
        self._sleep = sleep
        self._timeout = timeout
        self.calls = 0  # requests actually sent (budget accounting for callers)

    # ── core ─────────────────────────────────────────────────────────────────────────────────
    def request(self, method: str, path: str, params: Optional[Mapping[str, Any]] = None,
                json: Any = None, *, retry_5xx: Optional[bool] = None) -> Any:
        method = method.upper()
        url = build_url(path, params)
        headers = {"Accept": "application/json"}
        if json is not None:
            headers["Content-Type"] = "application/json"
        idempotent = method in _IDEMPOTENT if retry_5xx is None else retry_5xx
        waited = 0.0
        tries_429 = 0
        tried_5xx = False
        while True:
            failure: Optional[SpotifyError] = None
            try:
                self.calls += 1
                raw = self._transport(method, url, json=json, headers=headers, timeout=self._timeout)
            except Exception as exc:  # Hermes/transport errors
                failure = map_exception(exc)
            if failure is not None:
                # Raised outside the handler so neither __cause__ nor __context__ points back at the
                # original exception (and the frames it holds).
                failure.__cause__ = failure.__context__ = None
                raise failure.with_traceback(None)
            resp = Response(int(raw.status), dict(raw.headers or {}), bytes(raw.body or b""))
            if 200 <= resp.status < 300:
                return resp.json()
            retry_after = _retry_after(resp.headers)
            if resp.status == 429 and tries_429 < MAX_429_RETRIES:
                wait = 1.0 if retry_after is None else retry_after
                if waited + wait <= MAX_RETRY_WAIT_S:
                    tries_429 += 1
                    waited += wait
                    self._sleep(wait)
                    continue
            if resp.status >= 500 and idempotent and not tried_5xx and waited + 0.5 <= MAX_RETRY_WAIT_S:
                tried_5xx = True
                waited += 0.5
                self._sleep(0.5)
                continue
            raise _status_error(method, urlparse(url).path, resp.status, resp.json(), retry_after)

    def paginate(self, path: str, params: Optional[Mapping[str, Any]] = None, *, max_items: int = 500,
                 container: Optional[str] = None) -> list:
        """Follow ``next`` links (Spotify host only) until *max_items* items."""
        out: list = []
        page = self.request("GET", path, params)
        while True:
            body = page.get(container) if (container and isinstance(page, dict)) else page
            if not isinstance(body, dict):
                break
            out.extend(body.get("items") or [])
            nxt = body.get("next")
            if len(out) >= max_items or not nxt:
                break
            page = self.request("GET", nxt)
        return out[:max_items]

    # ── identity / taste sources ─────────────────────────────────────────────────────────────
    def me(self) -> dict:
        return self.request("GET", "/me") or {}

    def top(self, kind: str, time_range: str = "medium_term", limit: int = PAGE_LIMIT) -> list:
        if kind not in ("artists", "tracks"):
            raise ValueError("kind must be 'artists' or 'tracks'")
        if time_range not in ("short_term", "medium_term", "long_term"):
            raise ValueError("bad time_range")
        body = self.request("GET", f"/me/top/{kind}", {"time_range": time_range, "limit": min(limit, PAGE_LIMIT)})
        return list((body or {}).get("items") or [])

    def saved_tracks(self, max_items: int = 500) -> list:
        """Saved tracks as ``[{added_at, track}]`` (track normalized from ``item``/``track``)."""
        rows = self.paginate("/me/tracks", {"limit": PAGE_LIMIT}, max_items=max_items)
        return [{"added_at": r.get("added_at"), "track": t} for r in rows if (t := track_of(r))]

    def recently_played(self, limit: int = PAGE_LIMIT) -> list:
        body = self.request("GET", "/me/player/recently-played", {"limit": min(limit, PAGE_LIMIT)})
        return [{"played_at": r.get("played_at"), "track": t}
                for r in (body or {}).get("items") or [] if (t := track_of(r))]

    # ── search ───────────────────────────────────────────────────────────────────────────────
    def search_tracks(self, query: str, limit: int = 5, market: Optional[str] = None) -> list:
        limit = max(1, min(int(limit), SEARCH_LIMIT_MAX))
        body = self.request("GET", "/search", {"q": query, "type": "track", "limit": limit, "market": market})
        return list(((body or {}).get("tracks") or {}).get("items") or [])

    # ── playlists (own playlists only) ───────────────────────────────────────────────────────
    def create_playlist(self, name: str, description: str = "", public: bool = False) -> dict:
        # POST is not idempotent: never retried on 5xx (429 is safe: Spotify did not process it).
        return self.request("POST", "/me/playlists",
                            json={"name": name, "description": description, "public": bool(public)},
                            retry_5xx=False) or {}

    def replace_items(self, playlist_id: str, uris: list) -> Optional[str]:
        """Replace the whole playlist (idempotent). Returns the last snapshot id."""
        first, rest = uris[:PLAYLIST_WRITE_CHUNK], uris[PLAYLIST_WRITE_CHUNK:]
        body = self.request("PUT", f"/playlists/{playlist_id}/items", json={"uris": first})
        snapshot = (body or {}).get("snapshot_id")
        if rest:
            snapshot = self.add_items(playlist_id, rest) or snapshot
        return snapshot

    def add_items(self, playlist_id: str, uris: list, position: Optional[int] = None) -> Optional[str]:
        snapshot = None
        for i, part in enumerate(chunks(list(uris), PLAYLIST_WRITE_CHUNK)):
            payload: dict[str, Any] = {"uris": part}
            if position is not None:
                payload["position"] = position + i * PLAYLIST_WRITE_CHUNK
            body = self.request("POST", f"/playlists/{playlist_id}/items", json=payload, retry_5xx=False)
            snapshot = (body or {}).get("snapshot_id") or snapshot
        return snapshot

    def playlist_items(self, playlist_id: str, max_items: int = 500) -> list:
        rows = self.paginate(f"/playlists/{playlist_id}/items", {"limit": PAGE_LIMIT}, max_items=max_items)
        return [t for r in rows if (t := track_of(r))]

    def remove_items(self, playlist_id: str, uris: Iterable[str], snapshot_id: Optional[str] = None) -> Optional[str]:
        snapshot = None
        for part in chunks(list(uris), PLAYLIST_WRITE_CHUNK):
            payload: dict[str, Any] = {"items": [{"uri": u} for u in part]}
            if snapshot_id:
                payload["snapshot_id"] = snapshot_id
            body = self.request("DELETE", f"/playlists/{playlist_id}/items", json=payload)
            snapshot = (body or {}).get("snapshot_id") or snapshot
        return snapshot

    # ── library ──────────────────────────────────────────────────────────────────────────────
    def library_save(self, uris: Iterable[str]) -> None:
        for part in chunks(list(uris), LIBRARY_CHUNK):
            self.request("PUT", "/me/library", {"uris": part})

    def library_remove(self, uris: Iterable[str]) -> None:
        """Also unfollows playlists (``spotify:playlist:<id>``): Spotify has no playlist delete."""
        for part in chunks(list(uris), LIBRARY_CHUNK):
            self.request("DELETE", "/me/library", {"uris": part})

    # ── player (Premium) ─────────────────────────────────────────────────────────────────────
    def playback(self) -> Optional[dict]:
        return self.request("GET", "/me/player")  # None on 204 (nothing playing)


__all__ = [
    "API_BASE", "API_HOST", "PROVIDER_ID", "Response", "SETUP_APP", "SETUP_CLIENT_ID", "SETUP_INTERNAL",
    "SETUP_PREMIUM_OR_USERS", "SETUP_SIGNIN", "SpotifyClient", "SpotifyError", "build_url", "chunks",
    "map_exception", "track_of",
]
