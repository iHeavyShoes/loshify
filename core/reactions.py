"""Reactions: record 👎/👌/❤️, the 👎 actions (skip, remove from a managed playlist) and undo.

Generalized from v0.3's Desktop backend. The changes:
- The JSONL feedback file is gone. Reactions go into the store (``core/store.py``), which the curator reads.
- "Which playlists may be edited" is no longer a hard-coded name list. The rule is now
  ``store.is_managed``: only playlists Loshify itself created. The check runs before any Spotify call.
- One code path for every front door: the Desktop buttons (``source="desktop"``) and chat/voice
  (``loshify_react``, ``source="chat"``).

Order matters (same as v0.3): the reaction is durably stored first, and only then is Spotify touched,
so a failed skip or removal never loses the user's signal. A 👎 leaves an undo record (positions it was
removed from) for ``UNDO_TTL_S``.
"""

from __future__ import annotations

import re
import time
from typing import Any, Mapping, Optional

from .spotify import SpotifyClient, SpotifyError
from .store import Store

TRACK_URI_RE = re.compile(r"^spotify:track:[A-Za-z0-9]{22}$")
PLAYLIST_URI_RE = re.compile(r"^spotify:playlist:[A-Za-z0-9]{22}$")
REACTION_ALIASES = {
    "love": "love", "heart": "love", "❤️": "love", "❤": "love", "loved": "love",
    "okay": "okay", "ok": "okay", "up": "okay", "like": "okay", "👌": "okay", "good": "okay",
    "down": "down", "dislike": "down", "never": "down", "👎": "down", "no": "down",
}
PLAYLIST_SCAN_MAX = 5000

DEFAULT_SETTINGS = {"dislike_skips": True, "dislike_removes_from_playlist": True, "love_saves_to_liked": False}


def normalize_reaction(raw: Any) -> str:
    key = str(raw or "").strip().lower()
    if key not in REACTION_ALIASES:
        raise ValueError("reaction must be love, okay or down")
    return REACTION_ALIASES[key]


def _now(now: Optional[int]) -> int:
    return int(now if now is not None else time.time())


def track_snapshot(state: Any) -> Optional[dict]:
    """Reduce ``/me/player`` to what a reaction records. ``None`` when no music track is playing."""
    if not isinstance(state, Mapping):
        return None
    item = state.get("item")
    if not isinstance(item, Mapping) or item.get("type") != "track":
        return None
    uri = item.get("uri")
    if not isinstance(uri, str) or not TRACK_URI_RE.match(uri):
        return None
    album = item.get("album") if isinstance(item.get("album"), Mapping) else {}
    context = state.get("context") if isinstance(state.get("context"), Mapping) else {}
    ctx_uri = context.get("uri")
    device = state.get("device") if isinstance(state.get("device"), Mapping) else {}
    return {
        "track_uri": uri,
        "title": str(item.get("name") or ""),
        "artists": [str(a.get("name")) for a in item.get("artists") or [] if isinstance(a, Mapping) and a.get("name")],
        "album": str(album.get("name") or ""),
        "playlist_uri": ctx_uri if isinstance(ctx_uri, str) and PLAYLIST_URI_RE.match(ctx_uri) else None,
        "device_id": device.get("id") or None,
    }


# ── 👎 actions ─────────────────────────────────────────────────────────────────────────────────

def skip_if_current(client: SpotifyClient, track_uri: str) -> dict:
    """Skip only if *track_uri* is still the one playing (never skip a song the user moved on to)."""
    try:
        snap = track_snapshot(client.playback())
        if not snap or snap["track_uri"] != track_uri:
            return {"status": "track_changed"}
        client.request("POST", "/me/player/next", {"device_id": snap["device_id"]} if snap["device_id"] else None)
        return {"status": "skipped"}
    except SpotifyError as exc:
        return {"status": "error", "error": str(exc), **({"setup": exc.setup} if exc.setup else {})}


def find_positions(client: SpotifyClient, playlist_id: str, track_uri: str) -> list[int]:
    items = client.playlist_items(playlist_id, max_items=PLAYLIST_SCAN_MAX)
    return [i for i, t in enumerate(items) if t.get("uri") == track_uri]


def remove_from_managed(client: SpotifyClient, store: Store, playlist_uri: Optional[str], track_uri: str) -> dict:
    """Remove *track_uri* from *playlist_uri*, but only if Loshify created that playlist.

    The managed check comes first, before any Spotify call, so no code path can edit a user's own
    playlist. Returns the positions it was removed from so undo can put it back exactly.
    """
    if not playlist_uri:
        return {"status": "not_playlist"}
    if not store.is_managed(playlist_uri):
        return {"status": "not_managed", "playlist_uri": playlist_uri}
    pid = playlist_uri.rsplit(":", 1)[-1]
    try:
        positions = find_positions(client, pid, track_uri)
        if not positions:
            return {"status": "not_in_playlist", "playlist_uri": playlist_uri}
        snapshot = client.remove_items(pid, [track_uri])
    except SpotifyError as exc:
        return {"status": "error", "playlist_uri": playlist_uri, "error": str(exc)}
    return {"status": "removed", "playlist_uri": playlist_uri, "positions": positions, "snapshot_id": snapshot}


def dislike_actions(client: SpotifyClient, store: Store, snap: Mapping, settings: Mapping) -> dict:
    out: dict[str, Any] = {"skip": {"status": "off"}, "removal": {"status": "off"}}
    if settings.get("dislike_skips", True):
        out["skip"] = skip_if_current(client, snap["track_uri"])  # first: instant relief for the listener
    if settings.get("dislike_removes_from_playlist", True):
        out["removal"] = remove_from_managed(client, store, snap.get("playlist_uri"), snap["track_uri"])
    elif snap.get("playlist_uri"):
        out["removal"] = {"status": "off", "playlist_uri": snap.get("playlist_uri")}
    return out


# ── record / undo ──────────────────────────────────────────────────────────────────────────────

def record(store: Store, snap: Mapping, reaction: str, *, source: str, client: Optional[SpotifyClient] = None,
           settings: Optional[Mapping] = None, note: Optional[str] = None, act: bool = True,
           now: Optional[int] = None) -> dict:
    """Store a reaction to *snap* (a :func:`track_snapshot`) and, with *client* and *act*, run its actions.

    Returns ``{ok, id, reaction, track, actions?, undoable}``.
    """
    now = _now(now)
    reaction = normalize_reaction(reaction)
    settings = {**DEFAULT_SETTINGS, **(settings or {})}
    uri = snap.get("track_uri")
    if not isinstance(uri, str) or not TRACK_URI_RE.match(uri):
        raise ValueError("track_uri must be a spotify:track: URI")
    rid = store.add_reaction(uri, reaction, title=snap.get("title", ""), artists=snap.get("artists") or [],
                             album=snap.get("album") or "", source=source, playlist_uri=snap.get("playlist_uri"),
                             note=note, ts=now)
    out: dict[str, Any] = {"ok": True, "id": rid, "reaction": reaction, "undoable": False,
                           "track": {"title": snap.get("title", ""), "artists": list(snap.get("artists") or []),
                                     "uri": uri}}
    if client is None or not act:
        return out
    if reaction == "down":
        actions = dislike_actions(client, store, snap, settings)
        out["actions"] = actions
        store.put_undo(rid, {"track_uri": uri, "removal": actions["removal"]}, ts=now)
        out["undoable"] = True
    elif reaction == "love" and settings.get("love_saves_to_liked"):
        try:
            client.library_save([uri])
            out["actions"] = {"saved_to_liked": {"status": "saved"}}
        except SpotifyError as exc:
            out["actions"] = {"saved_to_liked": {"status": "error", "error": str(exc)}}
    return out


def undo(store: Store, reaction_id: str, *, client: Optional[SpotifyClient] = None,
         now: Optional[int] = None) -> dict:
    """Retract a reaction. For a 👎 also put the track back where it was removed from."""
    now = _now(now)
    rec = store.pop_undo(reaction_id, ts=now)
    restored: dict[str, Any] = {"status": "nothing_removed"}
    if rec and (rec.get("removal") or {}).get("status") == "removed":
        removal = rec["removal"]
        if client is None:
            store.put_undo(reaction_id, rec, ts=int(rec.get("ts", now)))
            return {"ok": False, "error": "Spotify is needed to put the track back."}
        pid = removal["playlist_uri"].rsplit(":", 1)[-1]
        if not store.is_managed(pid):  # forgotten since: never write to it
            restored = {"status": "not_managed"}
        else:
            try:
                for position in sorted(removal.get("positions") or []):  # ascending restores every index
                    client.add_items(pid, [rec["track_uri"]], position=position)
                if not removal.get("positions"):
                    client.add_items(pid, [rec["track_uri"]])
                restored = {"status": "restored", "playlist_uri": removal["playlist_uri"],
                            "positions": removal.get("positions")}
            except SpotifyError as exc:
                store.put_undo(reaction_id, rec, ts=int(rec.get("ts", now)))  # keep it: the user may retry
                return {"ok": False, "error": f"Could not put the track back: {exc}"}
    retracted = store.retract_reaction(reaction_id)
    if not retracted and rec:
        store.unblock(rec["track_uri"])  # digested already: at least lift the block
    if not retracted and not rec:
        return {"ok": False, "error": "Nothing to undo for that reaction (too old, or already undone)."}
    return {"ok": True, "retracted": retracted, "restored": restored}


def recent_feed(store: Store, since_ts: int, limit: int = 50) -> list[dict]:
    """Compact reaction rows for the agent (no ids it cannot use except for undo)."""
    return [{"id": r["id"], "ts": r["ts"], "reaction": r["reaction"], "title": r["title"],
             "artists": r["artists"], "source": r["source"], **({"note": r["note"]} if r.get("note") else {}),
             **({"from_loshify_playlist": True} if r.get("playlist_uri") and store.is_managed(r["playlist_uri"])
                else {})}
            for r in store.reactions_since(since_ts, limit)]


__all__ = ["DEFAULT_SETTINGS", "dislike_actions", "find_positions", "normalize_reaction", "recent_feed", "record",
           "remove_from_managed", "skip_if_current", "track_snapshot", "undo"]
