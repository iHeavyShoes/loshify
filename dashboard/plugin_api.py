"""Loshify Desktop backend (mounted by Hermes at ``/api/plugins/loshify/``).

The Desktop half (``desktop/plugin.js``) is a Spotify Connect remote with one-tap reactions plus the
first-run setup card. This router serves it, and every route is a thin shell over the same core the
agent tools use (``core/``):

* Spotify traffic goes through ``core.spotify.SpotifyClient`` → Hermes's
  ``credentialed_provider_request("spotify", …)``. Hermes holds and refreshes the sign-in; this module
  never reads, stores, logs or returns a token.
* Reactions go into the profile's Loshify store (``core.store``), so the curator sees Desktop and
  chat reactions alike. 👎 removes a track **only from playlists Loshify created**
  (``core.reactions.remove_from_managed``); every other playlist is skip + record only. Undo puts the
  track back at its old positions.
* A reaction always describes exactly the track the card showed: the client sends only ``track_uri``,
  and title/artists/context come from the snapshot this backend served (a URI it never served is
  refused).

Hermes imports this file standalone (no package), so the core is loaded from ``../core`` by path under
a private module name. Settings are read from ``plugins.entries.loshify.settings`` through the
documented ``hermes_cli.config.load_config_readonly``.
"""

from __future__ import annotations

import importlib.util
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from fastapi import APIRouter, Body

router = APIRouter()

PLUGIN_NAME = "loshify"
NOTE_MAX_CHARS = 500                   # = core.store.NOTE_MAX
SEARCH_LIMIT_MAX = 10                 # Spotify Development Mode cap (Feb 2026)
SNAPSHOT_TTL_S = 6 * 3600             # a reaction may come a while after the card last refreshed
SETTING_DEFAULTS: Dict[str, Any] = {
    "poll_interval_open": 3,
    "poll_interval_closed": 5,
    "dislike_skips": True,
    "dislike_removes_from_playlist": True,
    "love_saves_to_liked": False,
    "tune_in_days": 10,
    "demo_mode": False,
}
LLM_TIMEOUT_S = 60
LLM_MAX_TOKENS = 6000                 # rotation candidates: ~30 songs × up to 3 slots, each with a reason

_PLUGIN_DIR = Path(__file__).resolve().parents[1]
_CORE_PKG = "_loshify_core_" + format(abs(hash(str(_PLUGIN_DIR))), "x")


def _core(name: str):
    """``core.<name>`` of this plugin's own tree, imported once per process."""
    if _CORE_PKG not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            _CORE_PKG, _PLUGIN_DIR / "core" / "__init__.py", submodule_search_locations=[str(_PLUGIN_DIR / "core")])
        module = importlib.util.module_from_spec(spec)
        sys.modules[_CORE_PKG] = module
        spec.loader.exec_module(module)
    full = f"{_CORE_PKG}.{name}"
    if full not in sys.modules:
        spec = importlib.util.spec_from_file_location(full, _PLUGIN_DIR / "core" / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[full] = module
        spec.loader.exec_module(module)
    return sys.modules[full]


# ── seams (tests swap these for fakes) ──────────────────────────────────────────────────────────

def _default_client():
    return _core("spotify").SpotifyClient()


def _default_store():
    return _core("store").open_store()


def _default_raw_settings() -> Dict[str, Any]:
    try:
        from hermes_cli.config import load_config_readonly
        entries = ((load_config_readonly() or {}).get("plugins") or {}).get("entries") or {}
        entry = entries.get(PLUGIN_NAME) or {}
        for key in ("settings", "config"):  # ``config`` is the legacy spelling Hermes still reads
            value = entry.get(key) if isinstance(entry, dict) else None
            if isinstance(value, dict):
                return value
    except Exception:
        pass
    return {}


client_factory: Callable[[], Any] = _default_client
store_factory: Callable[[], Any] = _default_store
raw_settings: Callable[[], Dict[str, Any]] = _default_raw_settings


# ── settings / scope ────────────────────────────────────────────────────────────────────────────

def _as_bool(raw: Any, default: bool) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return bool(raw)
    if isinstance(raw, str):
        low = raw.strip().lower()
        if low in ("1", "true", "yes", "on"):
            return True
        if low in ("0", "false", "no", "off"):
            return False
    return default


def plugin_settings() -> Dict[str, Any]:
    """The Desktop-relevant settings merged over defaults. Never raises."""
    try:
        raw = raw_settings() or {}
    except Exception:
        raw = {}
    merged = dict(SETTING_DEFAULTS)
    for key in ("poll_interval_open", "poll_interval_closed"):
        try:
            if key in raw:
                merged[key] = max(1, int(float(raw[key])))
        except (TypeError, ValueError):
            pass
    for key in ("dislike_skips", "dislike_removes_from_playlist", "love_saves_to_liked", "demo_mode"):
        if key in raw:
            merged[key] = _as_bool(raw[key], SETTING_DEFAULTS[key])
    if "tune_in_days" in raw:
        merged["tune_in_days"] = _core("tunein").clamp_interval(raw["tune_in_days"])
    return merged


# ── the host-owned LLM (no chat): Hermes's public plugin LLM facade ─────────────────────────────

def _default_llm():
    """``llm(purpose, instructions, data) -> dict | None`` over ``agent.plugin_llm.PluginLlm``.

    The documented, host-owned completion for plugins (``ctx.llm``): the user's own main model and
    auth, no provider/model override (so no trust flags are needed), an audit line per call. Each
    purpose sends its JSON schema (``core.writer.SCHEMAS``) first. ``core.llm_json`` falls back to
    ``json_mode`` and then to plain text with tolerant JSON extraction when the provider rejects a mode.
    Every answer is validated in ``core.writer``. ``None`` when the facade is missing (an older Hermes):
    every step then falls back to the user's own data.
    """
    try:
        from agent.plugin_llm import PluginLlm, PluginLlmTextInput
    except Exception:
        return None
    return _schema_llm(PluginLlm(plugin_id=PLUGIN_NAME), lambda text: PluginLlmTextInput(text=text))


def _schema_llm(facade: Any, text_input: Callable[[str], Any]):
    """``llm(purpose, instructions, data)`` over *facade*: the purpose's schema → json_mode → plain text."""
    asker = _core("llm_json").JsonAsker(facade, text_input, plugin=PLUGIN_NAME, timeout=LLM_TIMEOUT_S,
                                        max_tokens=LLM_MAX_TOKENS)
    schemas = _core("writer").SCHEMAS

    def call(purpose: str, instructions: str, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        return asker(purpose, instructions, data, schemas.get(purpose))

    call.asker = asker  # type: ignore[attr-defined]  # for probes: asker.last / asker.mode
    return call


llm_factory: Callable[[], Any] = _default_llm


def _bind_profile_scope():
    """Bind the serving profile's secret scope when the server did not (current servers do: no-op).

    Hermes resolves the Client ID through this scope; Loshify never reads the value itself.
    """
    try:
        from agent.secret_scope import build_profile_secret_scope, current_secret_scope, set_secret_scope
        if current_secret_scope() is not None:
            return None
        from hermes_constants import get_hermes_home
        home = Path(get_hermes_home())
        return set_secret_scope(build_profile_secret_scope(home), profile_home=str(home))
    except Exception:
        return None


def _release_scope(token) -> None:
    if token is not None:
        try:
            from agent.secret_scope import reset_secret_scope
            reset_secret_scope(token)
        except Exception:
            pass


def _run(fn: Callable[..., Any], *, store: bool = False) -> Dict[str, Any]:
    """Call ``fn(client[, store])`` under the profile scope; map failures to ``{ok: false, …}``."""
    spotify = _core("spotify")
    token = _bind_profile_scope()
    db = None
    try:
        client = client_factory()
        if store:
            db = store_factory()
            data = fn(client, db)
        else:
            data = fn(client)
        return {"ok": True, "data": data}
    except spotify.SpotifyError as exc:
        return exc.to_dict()
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # never a traceback (or anything it holds) to the renderer
        return {"ok": False, "error": f"Loshify failed unexpectedly ({type(exc).__name__})."}
    finally:
        if db is not None:
            db.close()
        _release_scope(token)


# ── pure helpers ────────────────────────────────────────────────────────────────────────────────

def track_snapshot(state: Any) -> Optional[Dict[str, Any]]:
    """``core.reactions.track_snapshot`` plus what the card shows (progress, art, play state)."""
    snap = _core("reactions").track_snapshot(state)
    if not snap:
        return None
    item = state["item"]
    album = item.get("album") if isinstance(item.get("album"), dict) else {}
    images = [i for i in (album.get("images") or []) if isinstance(i, dict) and i.get("url")]
    try:
        progress = max(0, int(state.get("progress_ms") or 0))
    except (TypeError, ValueError):
        progress = 0
    return {**snap, "progress_ms": progress, "duration_ms": int(item.get("duration_ms") or 0),
            "is_playing": bool(state.get("is_playing")),
            "image": min(images, key=lambda i: i.get("width") or 10_000)["url"] if images else None}


def player_view(state: Any) -> Optional[Dict[str, Any]]:
    """Compact transport state for the remote (drops Spotify's large market lists)."""
    if not isinstance(state, dict):
        return None
    item = state.get("item") if isinstance(state.get("item"), dict) else None
    device = state.get("device") if isinstance(state.get("device"), dict) else {}
    album = (item or {}).get("album") if isinstance((item or {}).get("album"), dict) else {}
    sized = sorted((i for i in (album.get("images") or []) if isinstance(i, dict) and i.get("url")),
                   key=lambda i: i.get("width") or 0)
    art = next((i["url"] for i in sized if (i.get("width") or 0) >= 64), sized[-1]["url"] if sized else None)
    context = state.get("context") if isinstance(state.get("context"), dict) else {}
    return {
        "is_playing": bool(state.get("is_playing")),
        "progress_ms": int(state.get("progress_ms") or 0),
        "shuffle": bool(state.get("shuffle_state")),
        "repeat": state.get("repeat_state") or "off",
        "context_uri": context.get("uri"),
        "item": {
            "uri": item.get("uri"), "type": item.get("type"), "name": item.get("name"),
            "artists": [a.get("name") for a in (item.get("artists") or []) if isinstance(a, dict)],
            "duration_ms": int(item.get("duration_ms") or 0), "art": art,
        } if item else None,
        "device": {
            "id": device.get("id"), "name": device.get("name"), "type": device.get("type"),
            "is_active": bool(device.get("is_active")), "volume_percent": device.get("volume_percent"),
        } if device else None,
    }


def clamp_position(position_ms: Any, duration_ms: Any = None) -> int:
    """A seek target in whole ms, clamped to ``[0, duration_ms]`` (unknown duration: lower bound only)."""
    if isinstance(position_ms, bool):
        raise ValueError("position_ms must be a number")
    try:
        value = float(position_ms)
    except (TypeError, ValueError):
        raise ValueError("position_ms must be a number") from None
    if value != value or value in (float("inf"), float("-inf")):
        raise ValueError("position_ms must be a finite number")
    position = max(0, int(round(value)))
    try:
        duration = int(float(duration_ms or 0))
    except (TypeError, ValueError):
        duration = 0
    return min(position, duration) if duration > 0 else position


def clean_note(note: Any) -> Optional[str]:
    if note is None:
        return None
    if not isinstance(note, str):
        raise ValueError("note must be a string or null")
    note = note.replace("\r\n", "\n").strip()
    if len(note) > NOTE_MAX_CHARS:
        raise ValueError(f"note is longer than {NOTE_MAX_CHARS} characters")
    return note or None


def _device(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    return {"device_id": payload["device_id"]} if payload.get("device_id") else None


def _body(payload: Any) -> Dict[str, Any]:
    return payload if isinstance(payload, dict) else {}


# ── snapshot memory: only tracks this backend served may be reacted to ─────────────────────────

_snap_lock = threading.Lock()
_served: Dict[str, Dict[str, Any]] = {}
_last: Optional[Dict[str, Any]] = None


def remember(snap: Dict[str, Any]) -> None:
    global _last
    with _snap_lock:
        now = time.monotonic()
        _served[snap["track_uri"]] = {"snap": dict(snap), "at": now}
        _last = dict(snap)
        for uri in [u for u, v in _served.items() if now - v["at"] > SNAPSHOT_TTL_S]:
            _served.pop(uri, None)


def served_snapshot(track_uri: str) -> Optional[Dict[str, Any]]:
    with _snap_lock:
        hit = _served.get(track_uri)
        if not hit or time.monotonic() - hit["at"] > SNAPSHOT_TTL_S:
            return None
        return dict(hit["snap"])


def last_snapshot() -> Optional[Dict[str, Any]]:
    with _snap_lock:
        return dict(_last) if _last else None


def forget_snapshots() -> None:
    global _last
    with _snap_lock:
        _served.clear()
        _last = None


# ── setup ───────────────────────────────────────────────────────────────────────────────────────

def setup_state(client_id_saved: bool = False) -> Dict[str, Any]:
    """What the setup card shows: ``{ready, step, next_step?, status?, onboarded, redirect_uri, …}``.

    ``step`` is the user's next step: ``client_id`` (paste the Client ID), ``signin`` (run the sign-in
    command), ``app`` / ``premium_or_users`` (fix the Spotify app), ``retry`` or ``done``. The Client ID
    is checked (presence only) before Spotify is called; *client_id_saved* says this request just saved
    it, since a profile scope bound before the save may not show it yet.
    """
    su = _core("setup")
    token = _bind_profile_scope()
    db = None
    try:
        try:
            db = store_factory()
        except Exception:
            db = None  # no store yet must not hide the setup steps
        out = su.probe(client_factory(), db, (lambda: True) if client_id_saved else client_id_present)
        out["onboarded"] = bool(db is not None and db.onboarded_at)
    except Exception as exc:
        out = {"ready": False, "step": "retry", "next_step": su.NEXT_STEP["retry"], "onboarded": False,
               "reason": type(exc).__name__}
    finally:
        if db is not None:
            db.close()
        _release_scope(token)
    out.update({"redirect_uri": su.REDIRECT_URI, "signin_command": su.SIGNIN_COMMAND,
                "dashboard_url": su.DASHBOARD_URL, "app_name": su.APP_NAME, "api": su.API_NAME,
                "why": su.WHY_LINE, "users_why": su.USERS_WHY, "connect_where": su.CONNECT_WHERE,
                "fixes": dict(su.FIXES)})
    return out


@router.get("/setup")
def get_setup() -> Dict[str, Any]:
    return setup_state()


def _default_save_client_id(value: str) -> None:
    """Store the Client ID exactly like the Settings → Plugins form does for this ``type: secret`` field:
    Hermes's own writer, into the serving profile. Raises ``ValueError`` when the profile is locked."""
    from hermes_cli.config import save_env_value_secure
    save_env_value_secure(_core("setup").CLIENT_ID_ENV, value)


save_client_id: Callable[[str], None] = _default_save_client_id  # seam: tests swap in a recorder


def client_id_present() -> Optional[bool]:
    """Presence of the Client ID in the serving profile (``core.setup.client_id_present``). Seam for tests."""
    return _core("setup").client_id_present()


@router.post("/setup/client-id")
def setup_client_id(payload: Optional[Dict[str, Any]] = Body(default=None)) -> Dict[str, Any]:
    """Wizard step 5: paste the Client ID → saved into the plugin setting → re-probe. The value is never echoed."""
    su = _core("setup")
    client_id, problem = su.check_client_id(_body(payload).get("client_id"))
    if problem:
        return {"ok": False, "error": problem}
    token = _bind_profile_scope()
    try:
        save_client_id(client_id)
    except Exception as exc:
        detail = str(exc) if isinstance(exc, (ValueError, PermissionError)) else type(exc).__name__
        return {"ok": False, "error": f"Couldn't save the Client ID ({detail}). Paste it in Settings → Plugins → "
                                      "Loshify instead."}
    finally:
        _release_scope(token)
    return {"ok": True, "saved": True, "setup": setup_state(client_id_saved=True)}


# ── player ──────────────────────────────────────────────────────────────────────────────────────

@router.get("/player")
def player() -> Dict[str, Any]:
    """Transport state + the reaction snapshot + client settings, in one poll."""
    settings = plugin_settings()
    client_settings = {k: settings[k] for k in ("poll_interval_open", "poll_interval_closed")}

    def read(client, db):
        state = client.playback()
        snap = track_snapshot(state)
        if snap:
            snap["managed"] = bool(snap.get("playlist_uri") and db.is_managed(snap["playlist_uri"]))
        return state, snap

    res = _run(read, store=True)
    if not res["ok"]:
        return {**res, "last": last_snapshot(), "settings": client_settings}
    state, snap = res["data"]
    if snap:
        remember(snap)
    return {"ok": True, "data": player_view(state), "track": snap, "last": None if snap else last_snapshot(),
            "settings": client_settings}


@router.get("/devices")
def devices() -> Dict[str, Any]:
    def read(client):
        try:
            found = (client.request("GET", "/me/devices") or {}).get("devices") or []
        except _core("spotify").SpotifyError as exc:
            if exc.setup:  # a setup problem is the answer, not something to paper over
                raise
            found = []  # some accounts get 410 Gone here; fall through to the player's device
        if not found:  # /me/devices can be empty while /me/player still knows the active device
            device = ((client.playback() or {}).get("device") or {})
            if device.get("id"):
                found = [dict(device, is_active=True)]
        return {"devices": found}

    return _run(read)


@router.post("/play")
def play(payload: Optional[Dict[str, Any]] = Body(default=None)) -> Dict[str, Any]:
    """Resume, or start a context / a track. ``{uri, context_uri?}``: the track inside that context."""
    p = _body(payload)
    body: Dict[str, Any] = {}
    if p.get("context_uri"):
        body["context_uri"] = str(p["context_uri"])
    if p.get("uri"):
        if body.get("context_uri"):
            body["offset"] = {"uri": str(p["uri"])}  # album context + offset starts more reliably
        else:
            body["uris"] = [str(p["uri"])]
    if p.get("position_ms") not in (None, ""):
        try:
            body["position_ms"] = clamp_position(p["position_ms"])
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
    return _run(lambda c: c.request("PUT", "/me/player/play", _device(p), json=body or None))


@router.post("/pause")
def pause(payload: Optional[Dict[str, Any]] = Body(default=None)) -> Dict[str, Any]:
    return _run(lambda c: c.request("PUT", "/me/player/pause", _device(_body(payload))))


@router.post("/next")
def next_track(payload: Optional[Dict[str, Any]] = Body(default=None)) -> Dict[str, Any]:
    return _run(lambda c: c.request("POST", "/me/player/next", _device(_body(payload)), retry_5xx=False))


@router.post("/previous")
def previous_track(payload: Optional[Dict[str, Any]] = Body(default=None)) -> Dict[str, Any]:
    return _run(lambda c: c.request("POST", "/me/player/previous", _device(_body(payload)), retry_5xx=False))


@router.post("/seek")
def seek(payload: Optional[Dict[str, Any]] = Body(default=None)) -> Dict[str, Any]:
    """Jump inside the playing track. A seek is never a reaction and never a skip.

    Body: ``position_ms`` (required), ``track_uri`` (optional guard: refuse when a different track
    plays by now), ``device_id`` (optional). Clamped to ``[0, duration]`` from live playback state.
    """
    p = _body(payload)
    try:
        clamp_position(p.get("position_ms"))
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    track_uri = p.get("track_uri")
    if track_uri not in (None, "") and (not isinstance(track_uri, str)
                                        or not _core("reactions").TRACK_URI_RE.match(track_uri)):
        return {"ok": False, "error": "track_uri must be a spotify:track URI"}

    def run(client):
        state = client.playback() or {}
        item = state.get("item") if isinstance(state.get("item"), dict) else None
        if not item:
            return {"status": "no_track"}
        if track_uri and item.get("uri") != track_uri:
            return {"status": "track_changed"}
        duration = int(item.get("duration_ms") or 0)
        position = clamp_position(p.get("position_ms"), duration)
        device_id = p.get("device_id") or (state.get("device") or {}).get("id")
        params: Dict[str, Any] = {"position_ms": position}
        if device_id:
            params["device_id"] = device_id
        client.request("PUT", "/me/player/seek", params)
        return {"status": "moved", "position_ms": position, "duration_ms": duration}

    res = _run(run)
    if res["ok"] and res["data"]["status"] != "moved":
        reason = {"no_track": "nothing is playing", "track_changed": "the track changed before the seek; not moved"}
        return {"ok": False, "status": res["data"]["status"], "error": reason[res["data"]["status"]]}
    return res


@router.put("/volume")
def volume(payload: Optional[Dict[str, Any]] = Body(default=None)) -> Dict[str, Any]:
    p = _body(payload)
    try:
        percent = max(0, min(100, int(float(p.get("volume_percent", 0) or 0))))
    except (TypeError, ValueError):
        return {"ok": False, "error": "volume_percent must be a number"}
    return _run(lambda c: c.request("PUT", "/me/player/volume", {"volume_percent": percent, **(_device(p) or {})}))


@router.put("/transfer")
def transfer(payload: Optional[Dict[str, Any]] = Body(default=None)) -> Dict[str, Any]:
    p = _body(payload)
    if not p.get("device_id"):
        return {"ok": False, "error": "device_id is required"}
    play_now = _as_bool(p.get("play", True), True)
    return _run(lambda c: c.request("PUT", "/me/player", json={"device_ids": [p["device_id"]], "play": play_now}))


@router.get("/queue")
def queue() -> Dict[str, Any]:
    return _run(lambda c: c.request("GET", "/me/player/queue"))


@router.post("/queue")
def queue_add(payload: Optional[Dict[str, Any]] = Body(default=None)) -> Dict[str, Any]:
    p = _body(payload)
    if not p.get("uri"):
        return {"ok": False, "error": "uri is required"}
    return _run(lambda c: c.request("POST", "/me/player/queue", {"uri": p["uri"], **(_device(p) or {})},
                                    retry_5xx=False))


@router.put("/shuffle")
def shuffle(payload: Optional[Dict[str, Any]] = Body(default=None)) -> Dict[str, Any]:
    p = _body(payload)
    state = "true" if _as_bool(p.get("state", False), False) else "false"
    return _run(lambda c: c.request("PUT", "/me/player/shuffle", {"state": state, **(_device(p) or {})}))


@router.put("/repeat")
def repeat(payload: Optional[Dict[str, Any]] = Body(default=None)) -> Dict[str, Any]:
    p = _body(payload)
    state = p.get("state", "off")
    if state not in ("off", "context", "track"):
        return {"ok": False, "error": "state must be one of off/context/track"}
    return _run(lambda c: c.request("PUT", "/me/player/repeat", {"state": state, **(_device(p) or {})}))


@router.get("/search")
def search(q: str = "", limit: int = SEARCH_LIMIT_MAX) -> Dict[str, Any]:
    q = (q or "").strip()
    if len(q) < 2:
        return {"ok": True, "data": {"tracks": {"items": []}}}
    try:
        limit = max(1, min(SEARCH_LIMIT_MAX, int(limit)))
    except (TypeError, ValueError):
        limit = SEARCH_LIMIT_MAX
    return _run(lambda c: {"tracks": {"items": c.search_tracks(q, limit=limit)}})


@router.get("/playlists")
def playlists() -> Dict[str, Any]:
    """The user's playlists; the ones Loshify made come first and carry ``loshify: true``."""
    def read(client, db):
        items = (client.request("GET", "/me/playlists", {"limit": 50}) or {}).get("items") or []
        rows = [dict(i, loshify=bool(i.get("id") and db.is_managed(i["id"]))) for i in items if isinstance(i, dict)]
        rows.sort(key=lambda r: not r["loshify"])  # stable: Spotify's order within each group
        return {"items": rows}

    return _run(read, store=True)


@router.get("/rotation")
def rotation() -> Dict[str, Any]:
    """The user's rotation (spec, edition, playlists, due) and the presets. Read-only, no Spotify call.

    Building or refreshing a rotation needs the agent (it writes the candidates), so the Desktop asks
    for it through the chat instead of a write route here.
    """
    rot = _core("rotations")
    db = None
    try:
        db = store_factory()
        data = {**rot.status(db), "presets": rot.presets_view(), "max_slots": rot.MAX_SLOTS}
        return {"ok": True, "data": data}
    except Exception as exc:
        return {"ok": False, "error": f"Loshify could not read the rotation ({type(exc).__name__})."}
    finally:
        if db is not None:
            db.close()


# ── onboarding: calibration round, the user's note, skip ───────────────────────────────────────

def _with_store(fn: Callable[[Any], Dict[str, Any]], what: str) -> Dict[str, Any]:
    """Run *fn(store)* (no Spotify call); ``ValueError`` → its message, anything else → a generic error."""
    db = None
    try:
        db = store_factory()
        return {"ok": True, "data": fn(db)}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        return {"ok": False, "error": f"Loshify could not {what} ({type(exc).__name__})."}
    finally:
        if db is not None:
            db.close()


@router.get("/onboarding")
def onboarding() -> Dict[str, Any]:
    """Onboarding progress, anchors, the calibration round (with reactions) and the note. No Spotify call."""
    return _with_store(lambda db: _core("onboarding").status(db), "read the onboarding")


@router.post("/onboarding/react")
def onboarding_react(payload: Optional[Dict[str, Any]] = Body(default=None)) -> Dict[str, Any]:
    """👎/👌/❤️ on calibration track ``n`` (1-5) or ``track_uri``, through the normal reactions path.

    Works without playback (no Premium needed). A 👎 skips only if that track is what is playing; there
    is no playlist to remove it from. Reacting again to the same track changes the reaction.
    """
    body = _body(payload)
    ref = body.get("n") if body.get("n") not in (None, "") else body.get("track_uri")
    if ref in (None, ""):
        return {"ok": False, "error": "n (1-5) or track_uri is required"}
    try:
        reaction = _core("reactions").normalize_reaction(body.get("reaction"))
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    settings = {**plugin_settings(), "dislike_removes_from_playlist": False}
    return _run(lambda client, db: _core("onboarding").react(db, ref, reaction, source="desktop", client=client,
                                                           settings=settings), store=True)


@router.post("/note")
def note(payload: Optional[Dict[str, Any]] = Body(default=None)) -> Dict[str, Any]:
    """Save (or clear, with empty text) the user's taste note. Validated and length-capped; never decayed."""
    text = _body(payload).get("text")
    if text is not None and not isinstance(text, str):
        return {"ok": False, "error": "text must be a string"}
    return _with_store(lambda db: _core("onboarding").save_note(db, text), "save the note")


@router.post("/onboarding/skip")
def onboarding_skip(payload: Optional[Dict[str, Any]] = Body(default=None)) -> Dict[str, Any]:
    step = _body(payload).get("step")
    ob = _core("onboarding")
    if step not in ob.STEPS:
        return {"ok": False, "error": f"step must be one of {', '.join(ob.STEPS)}"}
    return _with_store(lambda db: ob.skip(db, step), "skip that step")


# ── "Get to know me": the click-through flow (no chat) ─────────────────────────────────────────

def _llm():
    try:
        return llm_factory()
    except Exception:
        return None


# The long steps run as background jobs (``core.jobs``) when the card sends a ``job`` id: the POST answers
# at once and the card polls ``GET /flow/job``. A client-side timeout or abort then only means "poll
# again", never a failure. Without ``job`` the route answers synchronously (the chat-free API stays simple
# for scripts and older cards).
FLOW_KINDS = ("meet", "verdict", "build", "tune-in")
_JOB_META = "flow_job:"            # + kind → the last finished job of that kind, for a status after a restart
JOB_RUN_INLINE = False             # tests: run the job in the request thread
_JOBS = None


def _jobs():
    global _JOBS
    if _JOBS is None:
        _JOBS = _core("jobs").Jobs()
    return _JOBS


def _job_scope() -> str:
    """One running job per kind *per profile* (one dashboard process can serve several profiles)."""
    try:
        from hermes_constants import get_hermes_home
        return str(get_hermes_home())
    except Exception:
        return ""


def _remember_job(kind: str, view: Dict[str, Any]) -> None:
    import json
    db = None
    try:
        db = store_factory()
        db.set_meta(_JOB_META + kind, json.dumps({k: view.get(k) for k in ("id", "kind", "state", "result",
                                                                            "message", "finished")},
                                                 ensure_ascii=False))
    except Exception:
        pass
    finally:
        if db is not None:
            db.close()


def _remembered_job(job_id: str) -> Optional[Dict[str, Any]]:
    import json
    db = None
    try:
        db = store_factory()
        for kind in FLOW_KINDS:
            raw = db.get_meta(_JOB_META + kind)
            if raw:
                view = json.loads(raw)
                if isinstance(view, dict) and view.get("id") == job_id:
                    return {**view, "progress": ""}
    except Exception:
        return None
    finally:
        if db is not None:
            db.close()
    return None


def _flow(kind: str, body: Dict[str, Any], step: Callable[..., Dict[str, Any]]) -> Dict[str, Any]:
    """Run *step(client, db, progress)* now, or as job ``body['job']`` and answer with the job's view."""
    job_id = body.get("job")
    if job_id in (None, ""):
        return _run(lambda client, db: step(client, db, None), store=True)
    jobs = _core("jobs")
    if not jobs.valid_id(job_id):
        return {"ok": False, "error": "job must be 8-64 letters, digits, '-' or '_'"}

    def work(progress):
        return _run(lambda client, db: step(client, db, progress), store=True)

    view = _jobs().start(job_id, kind, work, scope=_job_scope(), first=("Finding your picks…" if kind == "tune-in" else
        _core("flow").SAY.get({"meet": "learn", "verdict": "rewrite", "build": "arrangement"}[kind], "")),
        on_finish=lambda v: _remember_job(kind, v), run_inline=JOB_RUN_INLINE)
    return {"ok": True, "data": {"job": view}}


@router.get("/flow/job")
def flow_job(id: str = "") -> Dict[str, Any]:  # noqa: A002 - the query parameter is called id
    """``{id, kind, state: running|done|failed|unknown, progress, result?, message?}`` for the card's poll.

    ``result`` is the step's own answer (what the synchronous route returns). ``message`` is the step's
    plain-language reason when it failed, or null (the card then says it kindly). ``unknown``: no such job
    in this process and none remembered (e.g. it never arrived): the card offers "Try again".
    """
    jobs = _core("jobs")
    if not jobs.valid_id(id):
        return {"ok": False, "error": "id must be 8-64 letters, digits, '-' or '_'"}
    view = _jobs().get(id) or _remembered_job(id) or {"id": id, "state": jobs.UNKNOWN, "progress": ""}
    return {"ok": True, "data": view}


@router.post("/flow/meet")
def flow_meet(payload: Optional[Dict[str, Any]] = Body(default=None)) -> Dict[str, Any]:
    """After Connect: learn the taste, write the portrait (host LLM), prepare the 5 calibration picks."""
    llm = _llm()
    return _flow("meet", _body(payload), lambda client, db, progress: _core("flow").meet(
        client, db, llm, progress=progress))


@router.post("/flow/verdict")
def flow_verdict(payload: Optional[Dict[str, Any]] = Body(default=None)) -> Dict[str, Any]:
    """"Sounds like me" / "Not quite" (+ up to 3 optional anchors; then the portrait is rewritten)."""
    body = _body(payload)
    anchors = body.get("anchors")
    if anchors is not None and not (isinstance(anchors, list) and all(isinstance(a, str) for a in anchors)):
        return {"ok": False, "error": "anchors must be a list of short strings"}
    llm = _llm()
    return _flow("verdict", body, lambda client, db, progress: _core("flow").verdict(
        client, db, str(body.get("verdict") or ""), anchors, llm, progress=progress))


@router.post("/flow/build")
def flow_build(payload: Optional[Dict[str, Any]] = Body(default=None)) -> Dict[str, Any]:
    """A preset card or the user's own words, plus the optional note → rotation → first edition → links."""
    body = _body(payload)
    for key in ("preset", "words", "note"):
        if body.get(key) is not None and not isinstance(body.get(key), str):
            return {"ok": False, "error": f"{key} must be a string"}
    llm = _llm()
    return _flow("build", body, lambda client, db, progress: _core("flow").build(
        client, db, preset=body.get("preset") or None, words=body.get("words") or None, note=body.get("note"),
        llm=llm, progress=progress))


@router.post("/onboarding/restart")
def onboarding_restart() -> Dict[str, Any]:
    """"Start over": reset the first-run progress and the old calibration round. Reactions, anchors, the
    note, the taste and the playlists are kept; "Get to know me" can then run again from the top."""
    return _with_store(lambda db: _core("onboarding").restart(db), "start over")


# ── Tune in (optional, recurring calibration) ──────────────────────────────────────────────────

@router.get("/tune-in")
def tune_in() -> Dict[str, Any]:
    """``{available, next_in_days, line, round?}``. No Spotify call."""
    settings = plugin_settings()
    days, demo = settings["tune_in_days"], settings["demo_mode"]

    def read(db):
        out = _core("tunein").status(db, interval_days=days, demo=demo)
        rnd = _core("onboarding").calibration_view(db, _core("tunein").ROUND_KEY)
        if out.get("open_round") and rnd:
            out["round"] = rnd
        return out

    return _with_store(read, "read the tune-in")


@router.post("/tune-in/start")
def tune_in_start(payload: Optional[Dict[str, Any]] = Body(default=None)) -> Dict[str, Any]:
    settings = plugin_settings()
    days, demo = settings["tune_in_days"], settings["demo_mode"]
    llm = _llm()
    return _flow("tune-in", payload or {}, lambda client, db, progress:
                 _core("flow").tune_in_start(client, db, llm, interval_days=days, demo=demo))


@router.post("/tune-in/react")
def tune_in_react(payload: Optional[Dict[str, Any]] = Body(default=None)) -> Dict[str, Any]:
    """👎/👌/❤️ on tune-in track ``n``: record only (the normal reactions path; no skip, no removal)."""
    body = _body(payload)
    ref = body.get("n") if body.get("n") not in (None, "") else body.get("track_uri")
    if ref in (None, ""):
        return {"ok": False, "error": "n (1-5) or track_uri is required"}
    try:
        reaction = _core("reactions").normalize_reaction(body.get("reaction"))
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    key = _core("tunein").ROUND_KEY
    return _with_store(lambda db: _core("onboarding").react(db, ref, reaction, source="desktop", key=key),
                       "save that reaction")


@router.post("/tune-in/finish")
def tune_in_finish() -> Dict[str, Any]:
    settings = plugin_settings()
    days, demo = settings["tune_in_days"], settings["demo_mode"]
    return _with_store(lambda db: _core("flow").tune_in_finish(db, interval_days=days, demo=demo), "close the tune-in")


# ── demo_mode: recording / testing aids ────────────────────────────────────────────────────────

@router.get("/demo")
def demo() -> Dict[str, Any]:
    """``{on, archive?, taste?}``: with the ``demo_mode`` setting on, the last Archive refresh counts
    and the taste line for the card. Real values only: a part that has no data yet is left out.
    No Spotify call."""
    if not plugin_settings()["demo_mode"]:
        return {"ok": True, "data": {"on": False}}

    def read(db):
        out: Dict[str, Any] = {"on": True}
        archive = _core("rotations").last_archive(db)
        if archive:
            out["archive"] = archive
        summary = _core("taste").card_summary(db)
        if summary:
            out["taste"] = summary
        return out

    return _with_store(read, "read the demo summary")


# ── reactions ───────────────────────────────────────────────────────────────────────────────────

@router.post("/feedback")
def feedback(payload: Optional[Dict[str, Any]] = Body(default=None)) -> Dict[str, Any]:
    """Record 👎/👌/❤️ on the track the card showed; a 👎 then skips and edits only Loshify playlists.

    Body: ``track_uri``, ``reaction`` (love | okay | down), ``note?``, ``amends?`` (a reaction id: add the
    note to that earlier reaction instead of recording a new one).
    """
    rx = _core("reactions")
    body = _body(payload)
    track_uri = body.get("track_uri")
    if not isinstance(track_uri, str) or not rx.TRACK_URI_RE.match(track_uri):
        return {"ok": False, "error": "track_uri must be a spotify:track URI"}
    snap = served_snapshot(track_uri)
    if not snap:
        return {"ok": False, "error": "track was not shown on the card recently; refresh and try again"}
    try:
        note = clean_note(body.get("note"))
        reaction = rx.normalize_reaction(body.get("reaction"))
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    amends = body.get("amends") or None

    if amends is not None:
        if not note:
            return {"ok": False, "error": "a late note needs text"}

        def amend(_client, db):
            original = db.get_reaction(str(amends))
            if not original or original.get("track_uri") != track_uri:
                raise ValueError("amends must reference an earlier reaction to the same track")
            if not db.amend_reaction(str(amends), note=note):
                raise ValueError("that reaction was already folded into your taste; react again instead")
            return {"id": str(amends), "reaction": original["reaction"], "amends": str(amends)}

        return _run(amend, store=True)

    settings = plugin_settings()

    def record(client, db):
        out = rx.record(db, snap, reaction, source="desktop", client=client, settings=settings, note=note)
        removal = (out.get("actions") or {}).get("removal") or {}
        if removal.get("playlist_uri"):
            managed = db.get_playlist(removal["playlist_uri"])
            if managed:
                removal["playlist_name"] = managed.get("name")
        return out

    return _run(record, store=True)


@router.post("/undo")
def undo(payload: Optional[Dict[str, Any]] = Body(default=None)) -> Dict[str, Any]:
    """Undo a reaction; for a 👎 also put the track back where it was removed from."""
    event_id = _body(payload).get("event_id")
    if not isinstance(event_id, str) or not event_id:
        return {"ok": False, "error": "event_id is required"}
    res = _run(lambda client, db: _core("reactions").undo(db, event_id, client=client), store=True)
    if res["ok"] and not res["data"].get("ok", True):
        return {"ok": False, "error": res["data"].get("error") or "undo failed"}
    return res
