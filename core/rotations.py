"""Rotations: the user's standing arrangement of Loshify playlists, refreshed on a cadence.

A fixed template is a product; a rotation is how *this* listener organizes music. A rotation spec is
a short list of slots (each one playlist Loshify owns), a cadence and an archive policy:

    {"name": "Rhythmic / Quiet / Mixed", "intent": "...", "cadence": "weekly", "archive": "archive",
     "slots": [{"name": "Rhythmic", "intent": "beat-forward ...", "mode": "mixed"}, ...],
     "preset": "rhythm_quiet_mixed", "source_text": "<the user's own words, if custom>"}

- ``PRESETS`` are generic starting points (no user data). A custom spec comes from the user's own
  words: the agent turns them into a spec, ``preview`` validates and shows it, and ``set`` stores it
  only after the user confirmed. ``validate_spec`` is the single gate (<= 6 slots, cadence
  daily/weekly/manual, archive archive/replace, unique slot names).
- ``refresh`` rolls the rotation: one new *edition* per slot. The agent supplies candidates per slot
  (resolved and filtered exactly like ``deep_dig``/``mood``); a ``familiar``/``mixed`` slot without
  candidates is filled deterministically from the user's own library (the Time Capsule rules).
  Slots never share a track within an edition. With ``archive: "archive"`` the previous edition's
  tracks are moved (prepended) into ONE managed Archive playlist before the slot is replaced; the
  copy happens first, so a failed archive never loses the old edition.
- The Archive is curated, not a pile: ❤️ tracks (incl. 3×👌) are *keepers* and stay; tracks that
  finished an edition with 👌 or no reaction enter on *probation* and age out after
  ``probation_days`` unless loved since; 👎 tracks never enter and are removed; above ``archive_cap``
  the oldest non-keepers leave first (keepers are never evicted). Pruning happens during ``refresh``
  only (no scheduler) and only ever removes tracks Loshify itself archived (per-track records in
  ``rotation_state.archive_tracks``).
- Only playlists Loshify created are ever touched (``Store.is_managed``); the spec and the rotation
  state live in the store's ``meta`` table (no schema change).
"""

from __future__ import annotations

import json
import time
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional

from . import taste
from .curate import (PLAYLIST_URL, Exclusions, Pick, Resolver, clamp_size, exclusions, from_candidates,
                     known_artists, time_capsule, time_capsule_sources, write_playlist)
from .spotify import SpotifyClient, SpotifyError
from .store import DAY, Store
from .text import clip

SPEC_KEY = "rotation"
STATE_KEY = "rotation_state"

MAX_SLOTS = 6
CADENCES = ("daily", "weekly", "manual")
ARCHIVE_POLICIES = ("archive", "replace")
MODES = ("familiar", "mixed", "discover")
MODE_HELP = {
    "familiar": "the user's own music (no candidates needed: filled from their library)",
    "mixed": "their loves plus some new music (candidates; falls back to their library)",
    "discover": "mostly artists new to them (candidates required; >= 80% new artists)",
}
NAME_MAX, SLOT_NAME_MAX, INTENT_MAX, SLOT_INTENT_MAX, SOURCE_TEXT_MAX = 60, 40, 200, 140, 300
RESERVED_SLOT_NAMES = frozenset({"archive"})

SLOT_SIZE_DEFAULT = 15
MAX_CANDIDATES_PER_SLOT = 30
MAX_SEARCHES = 90           # whole refresh; ≈ 0.3 s each
TOP_UP_WINDOW_S = 6 * 3600  # a named-slot refresh this soon after the slot was built is a retry (top-up)
PROBATION_DAYS_DEFAULT = 90  # a non-loved archived track ages out after this many days
ARCHIVE_CAP_DEFAULT = 200    # hard cap on tracks in the Archive (keepers are never evicted by it)
PROBATION_DAYS_RANGE = (1, 3650)
ARCHIVE_CAP_RANGE = (10, 2000)
KEEPER, PROBATION = "keeper", "probation"
DUE_AFTER_S = {"daily": 20 * 3600, "weekly": 6 * DAY + 12 * 3600}

TEMPLATE = "rotation"
ARCHIVE_TEMPLATE = "rotation_archive"


def _now(now: Optional[int]) -> int:
    return int(now if now is not None else time.time())


def _slot(name: str, intent: str, mode: str) -> dict:
    return {"name": name, "intent": intent, "mode": mode}


# Generic presets: names + one-line intent per slot + cadence + archive policy. No user data.
PRESETS: dict[str, dict] = {
    "rhythm_quiet_mixed": {
        "name": "Rhythmic / Quiet / Mixed",
        "intent": "Three weekly playlists by energy; last week's editions move to one Archive playlist.",
        "cadence": "weekly", "archive": "archive",
        "slots": [
            _slot("Rhythmic", "Beat-forward: grooves, drums and momentum for moving, working out, cleaning.", "mixed"),
            _slot("Quiet", "Low and spacious: soft, slow, instrumental-leaning, for reading and late hours.", "mixed"),
            _slot("Mixed", "A bit of everything you love, with a few new ones slipped in.", "familiar"),
        ],
    },
    "day_parts": {
        "name": "Morning / Focus / Night",
        "intent": "One playlist per part of the day, refreshed weekly; old editions go to the Archive.",
        "cadence": "weekly", "archive": "archive",
        "slots": [
            _slot("Morning", "Bright, warm and easy to wake up to; nothing abrasive.", "mixed"),
            _slot("Focus", "Steady and unobtrusive: few vocals, no sudden turns, long attention spans.", "mixed"),
            _slot("Night", "Late, dim and intimate: slow tempos, space, a little melancholy.", "mixed"),
        ],
    },
    "discovery_dial": {
        "name": "Comfort / Edge / Wild",
        "intent": "Three playlists by how far from home they go; weekly, old editions archived.",
        "cadence": "weekly", "archive": "archive",
        "slots": [
            _slot("Comfort", "Your own favourites and forgotten loves; no surprises.", "familiar"),
            _slot("Edge", "Half familiar, half new: neighbours of what you already love.", "mixed"),
            _slot("Wild", "Almost all new artists, one confident step outside your lanes.", "discover"),
        ],
    },
    "then_now_next": {
        "name": "Then / Now / Next",
        "intent": "Your music by era: where you came from, where you are, where you are heading. On demand.",
        "cadence": "manual", "archive": "replace",
        "slots": [
            _slot("Then", "Older loves from your library you have not heard in a long while.", "familiar"),
            _slot("Now", "What you are into lately, deepened with close neighbours.", "mixed"),
            _slot("Next", "New artists that continue your recent direction.", "discover"),
        ],
    },
}


def presets_view() -> list[dict]:
    """Compact preset list for the agent (onboarding question) and the Desktop."""
    return [{"id": pid, "name": p["name"], "intent": p["intent"], "cadence": p["cadence"], "archive": p["archive"],
             "slots": [{"name": s["name"], "intent": s["intent"], "mode": s["mode"]} for s in p["slots"]]}
            for pid, p in PRESETS.items()]


# ── validation ─────────────────────────────────────────────────────────────────────────────────

def _text(value: Any, limit: int) -> str:
    return clip(" ".join(str(value or "").split()), limit)


def validate_spec(raw: Any) -> dict:
    """Normalize and validate a rotation spec. Raises ``ValueError`` listing every problem."""
    if not isinstance(raw, Mapping):
        raise ValueError("rotation spec must be an object {name, slots[], cadence, archive}")
    problems: list[str] = []
    slots_raw = raw.get("slots")
    if not isinstance(slots_raw, list) or not slots_raw:
        problems.append("slots must be a non-empty list of {name, intent, mode}")
        slots_raw = []
    elif len(slots_raw) > MAX_SLOTS:
        problems.append(f"at most {MAX_SLOTS} slots (got {len(slots_raw)})")
    slots: list[dict] = []
    seen: set[str] = set()
    for i, s in enumerate(slots_raw[:MAX_SLOTS], 1):
        if isinstance(s, str):
            s = {"name": s}
        if not isinstance(s, Mapping):
            problems.append(f"slot {i} must be an object")
            continue
        name = _text(s.get("name"), SLOT_NAME_MAX)
        key = name.casefold()
        mode = str(s.get("mode") or "mixed").strip().lower()
        if not name:
            problems.append(f"slot {i} needs a name")
        elif key in RESERVED_SLOT_NAMES:
            problems.append(f"slot name '{name}' is reserved (the archive is automatic)")
        elif key in seen:
            problems.append(f"slot names must be unique ('{name}' twice)")
        if mode not in MODES:
            problems.append(f"slot '{name or i}' mode must be one of {', '.join(MODES)}")
        seen.add(key)
        slots.append({"name": name, "intent": _text(s.get("intent"), SLOT_INTENT_MAX), "mode": mode})
    cadence = str(raw.get("cadence") or "weekly").strip().lower()
    if cadence not in CADENCES:
        problems.append(f"cadence must be one of {', '.join(CADENCES)}")
    archive = str(raw.get("archive") or "archive").strip().lower()
    if archive not in ARCHIVE_POLICIES:
        problems.append(f"archive must be one of {', '.join(ARCHIVE_POLICIES)}")
    preset = raw.get("preset") or None
    if preset is not None and preset not in PRESETS:
        problems.append(f"unknown preset '{preset}'")
    if problems:
        raise ValueError("invalid rotation: " + "; ".join(problems))
    name = _text(raw.get("name"), NAME_MAX) or clip(" / ".join(s["name"] for s in slots), NAME_MAX)
    spec = {"name": name, "intent": _text(raw.get("intent"), INTENT_MAX), "cadence": cadence, "archive": archive,
            "slots": slots, "preset": preset}
    source = _text(raw.get("source_text"), SOURCE_TEXT_MAX)
    if source:
        spec["source_text"] = source
    return spec


def from_preset(preset_id: str, *, cadence: Optional[str] = None, archive: Optional[str] = None) -> dict:
    if preset_id not in PRESETS:
        raise ValueError(f"preset must be one of {', '.join(PRESETS)}")
    p = PRESETS[preset_id]
    return validate_spec({**p, "slots": [dict(s) for s in p["slots"]], "preset": preset_id,
                          "cadence": cadence or p["cadence"], "archive": archive or p["archive"]})


def describe(spec: Mapping) -> str:
    """One readable block the agent can show the user before they confirm."""
    head = f"{spec['name']} · {spec['cadence']} · " + (
        "previous editions move to one Archive playlist" if spec["archive"] == "archive"
        else "each refresh replaces the previous edition")
    lines = [head] + [f"- {s['name']} ({s['mode']}): {s['intent'] or 'no intent given'}" for s in spec["slots"]]
    return "\n".join(lines)


# ── stored spec + state ────────────────────────────────────────────────────────────────────────

def _load(store: Store, key: str) -> Optional[dict]:
    raw = store.get_meta(key)
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def get_spec(store: Store) -> Optional[dict]:
    raw = _load(store, SPEC_KEY)
    if raw is None:
        return None
    try:
        return validate_spec(raw)
    except ValueError:
        return None  # a damaged spec is treated as none; the user sets a new one


def _clamp(value: Any, default: int, bounds: tuple[int, int]) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(bounds[0], min(bounds[1], n))


def _archive_records(state: Mapping) -> dict[str, dict]:
    """``{uri: {status, added, expires}}``. A pre-policy state (``archived_uris`` only) becomes probation."""
    out: dict[str, dict] = {}
    raw = state.get("archive_tracks")
    if isinstance(raw, Mapping):
        for uri, rec in raw.items():
            if not str(uri).startswith("spotify:track:") or not isinstance(rec, Mapping):
                continue
            keeper = rec.get("status") == KEEPER
            added = int(rec.get("added") or 0)
            expires = None if keeper else int(rec.get("expires") or added + PROBATION_DAYS_DEFAULT * DAY)
            out[str(uri)] = {"status": KEEPER if keeper else PROBATION, "added": added, "expires": expires}
        return out
    since = int(state.get("last_refresh_ts") or 0)  # best guess for when the legacy uris were archived
    for uri in state.get("archived_uris") or []:
        if str(uri).startswith("spotify:track:"):
            out[str(uri)] = {"status": PROBATION, "added": since, "expires": since + PROBATION_DAYS_DEFAULT * DAY}
    return out


def get_state(store: Store) -> dict:
    state = _load(store, STATE_KEY) or {}
    return {"edition": int(state.get("edition") or 0), "last_refresh_ts": int(state.get("last_refresh_ts") or 0),
            "slots": dict(state.get("slots") or {}), "slot_built": dict(state.get("slot_built") or {}),
            "archive_playlist_id": state.get("archive_playlist_id"),
            "archive_tracks": _archive_records(state),
            **({"last_archive": dict(state["last_archive"])} if isinstance(state.get("last_archive"), Mapping) else {})}


LAST_ARCHIVE_FIELDS = ("added", "expired", "evicted", "disliked", "promoted", "keepers", "kept")


def last_archive(store: Store) -> Optional[dict]:
    """The counts of the last refresh that curated the Archive, as they were reported then, with
    ``line`` and ``ts``; ``None`` when no refresh has curated an Archive yet (never made up)."""
    raw = get_state(store).get("last_archive")
    if not raw:
        return None
    try:
        counts = {k: int(raw.get(k) or 0) for k in LAST_ARCHIVE_FIELDS}
        ts = int(raw.get("ts") or 0)
    except (TypeError, ValueError):
        return None
    return {**counts, "ts": ts, "line": archive_line(counts)}


def _save_state(store: Store, state: dict) -> None:
    state = {k: v for k, v in state.items() if k != "archived_uris"}
    store.set_meta(STATE_KEY, json.dumps(state, ensure_ascii=False, separators=(",", ":")))


def set_spec(store: Store, spec: Mapping, now: Optional[int] = None) -> dict:
    spec = validate_spec(spec)
    spec["set_at"] = _now(now)
    store.set_meta(SPEC_KEY, json.dumps(spec, ensure_ascii=False, separators=(",", ":")))
    spec.pop("set_at")
    return spec


def clear_spec(store: Store) -> bool:
    """Forget the arrangement. Its playlists stay on Spotify and stay managed (👎 can still edit them)."""
    had = store.get_meta(SPEC_KEY) is not None
    store.set_meta(SPEC_KEY, None)
    store.set_meta(STATE_KEY, None)
    return had


def _slot_key(name: str) -> str:
    return " ".join(str(name).split()).casefold()


def is_due(spec: Optional[Mapping], state: Mapping, now: Optional[int] = None) -> bool:
    if not spec:
        return False
    if not state.get("last_refresh_ts"):
        return True  # never built: the first edition is always due
    after = DUE_AFTER_S.get(spec["cadence"])
    return bool(after) and _now(now) - int(state["last_refresh_ts"]) >= after


def status(store: Store, now: Optional[int] = None) -> dict:
    """Spec + state as the agent and the Desktop see them (no Spotify call)."""
    spec = get_spec(store)
    state = get_state(store)
    if not spec:
        return {"configured": False, "presets": [{"id": pid, "name": p["name"]} for pid, p in PRESETS.items()]}
    playlists = []
    for slot in spec["slots"]:
        pid = state["slots"].get(_slot_key(slot["name"]))
        managed = store.get_playlist(pid) if pid else None
        playlists.append({"slot": slot["name"], "mode": slot["mode"], "playlist_id": managed and managed["id"],
                          "url": managed and f"https://open.spotify.com/playlist/{managed['id']}"})
    archive_id = state.get("archive_playlist_id")
    archive_id = archive_id if archive_id and store.is_managed(archive_id) else None
    last = state["last_refresh_ts"]
    records = state["archive_tracks"]
    keepers = sum(1 for r in records.values() if r["status"] == KEEPER)
    return {
        "configured": True, "spec": spec, "description": describe(spec), "edition": state["edition"],
        "last_refresh": datetime.fromtimestamp(last, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if last else None,
        "due": is_due(spec, state, now), "playlists": playlists, "archive_playlist_id": archive_id,
        "archive": {"tracks": len(records), "keepers": keepers, "probation": len(records) - keepers}
        if archive_id else None,
    }


# ── refresh ────────────────────────────────────────────────────────────────────────────────────

def _slot_candidates(candidates: Any, slots: list[dict]) -> dict[str, list]:
    """``{slot name: [..]}`` (any case) → ``{slot key: [mapping, ..]}``. Unknown slot names raise."""
    if candidates in (None, ""):
        return {}
    if not isinstance(candidates, Mapping):
        raise ValueError("candidates must be an object {slot name: [{title, artist, reason}, ...]}")
    keys = {_slot_key(s["name"]) for s in slots}
    out: dict[str, list] = {}
    for name, items in candidates.items():
        key = _slot_key(name)
        if key not in keys:
            raise ValueError(f"no slot named '{name}' in this rotation")
        if not isinstance(items, list):
            raise ValueError(f"candidates for '{name}' must be a list")
        out[key] = [c for c in items if isinstance(c, Mapping)][:MAX_CANDIDATES_PER_SLOT]
    return out


def _extend(ex: Exclusions, picks: list[Pick]) -> Exclusions:
    """Exclusions for the next slot: nothing already picked in this edition."""
    return replace(ex, served_uris=ex.served_uris | {p.track["uri"] for p in picks})


def _is_top_up(state: Mapping, key: str, now: int) -> bool:
    """A named-slot refresh of a slot built in the *current* edition, shortly after: a retry, not a new edition."""
    built = (state.get("slot_built") or {}).get(key) or {}
    edition = int(state.get("edition") or 0)
    return bool(edition and int(built.get("edition") or -1) == edition
                and 0 <= now - int(built.get("ts") or 0) < TOP_UP_WINDOW_S)


def _current_picks(client: SpotifyClient, store: Store, playlist_id: str) -> list[Pick]:
    """The slot's tracks as they are now, with the reasons they were served with (kept on a top-up)."""
    reasons = store.served_reasons(playlist_id)
    out: list[Pick] = []
    for t in client.playlist_items(playlist_id):
        uri = str(t.get("uri") or "")
        if uri.startswith("spotify:track:"):
            out.append(Pick(dict(t), reasons.get(uri, ""), "kept"))
    return out


def _edition_description(spec: Mapping, slot: Mapping, edition: int, now: int) -> str:
    stamp = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d")
    base = f"Curated by Loshify for you · {spec['name']} · edition {edition} · {stamp}"
    return clip(f"{slot['intent']} · {base}" if slot.get("intent") else base, 300)


def _archive(client: SpotifyClient, store: Store, spec: Mapping, state: dict, uris: list[str], now: int,
             probation_days: int, tally: dict) -> dict:
    """Prepend *uris* to the rotation's one managed Archive playlist (created on first use).

    Admission follows the taste memory: 👎 never enters; ❤️ enters as a keeper; 👌 or no reaction
    enters on probation (expires after *probation_days*). A track already archived is not added again
    (a ❤️ since then promotes it in place).
    """
    records = state["archive_tracks"]
    verdicts = taste.track_verdicts(store, uris)
    fresh: list[str] = []
    for uri in dict.fromkeys(uris):
        verdict = verdicts.get(uri)
        if uri in records:
            if verdict == "love" and records[uri]["status"] != KEEPER:
                records[uri].update(status=KEEPER, expires=None)
            continue
        if verdict == "down":
            tally["refused"] += 1
            continue
        fresh.append(uri)
    if not fresh:
        return {"archived": 0}
    pid = state.get("archive_playlist_id")
    if not pid or not store.is_managed(pid):
        name = clip(f"Loshify · {spec['name']} · Archive", 100)
        created = client.create_playlist(name, "Earlier editions of your Loshify rotation, newest first.", public=False)
        pid = str(created.get("id") or "")
        if not pid:
            raise SpotifyError("Spotify did not return the archive playlist's id.", reason="no_playlist_id")
        store.add_playlist(pid, name, ARCHIVE_TEMPLATE, {"rotation": spec["name"]}, ts=now)
        state["archive_playlist_id"] = pid
        _save_state(store, state)  # the archive is managed and remembered from the moment it exists
    client.add_items(pid, fresh, position=0)
    store.touch_playlist(pid, ts=now)
    for uri in fresh:
        loved = verdicts.get(uri) == "love"
        records[uri] = {"status": KEEPER if loved else PROBATION, "added": now,
                        "expires": None if loved else now + probation_days * DAY}
    tally["added"] += len(fresh)
    _save_state(store, state)  # saved now: if the slot write fails next, a retry must not archive these again
    return {"archived": len(fresh), "archive_playlist_id": pid}


def _curate_archive(client: SpotifyClient, store: Store, state: dict, now: int, cap: int, tally: dict) -> dict:
    """Prune the managed Archive by the policy; return the counts the agent reports.

    👎 → removed; ❤️ since → promoted to keeper; expired probation → aged out; then, over *cap*, the
    oldest non-keepers are evicted (keepers never). Spotify is changed first and the records saved
    after, so a failed removal is retried on the next refresh. Only recorded (Loshify-archived) tracks
    are ever removed; anything the user added to the Archive by hand is left alone.
    """
    pid = state.get("archive_playlist_id")
    records: dict[str, dict] = state["archive_tracks"]
    if not pid or not store.is_managed(pid):
        return {}
    verdicts = taste.track_verdicts(store, records)
    disliked: list[str] = []
    expired: list[str] = []
    promoted = 0
    for uri, rec in records.items():
        verdict = verdicts.get(uri)
        if verdict == "down":
            disliked.append(uri)
        elif verdict == "love":
            if rec["status"] != KEEPER:
                rec.update(status=KEEPER, expires=None)
                promoted += 1
        elif rec["status"] == PROBATION and rec["expires"] is not None and rec["expires"] <= now:
            expired.append(uri)
    gone = set(disliked) | set(expired)
    remaining = [uri for uri in records if uri not in gone]
    keepers = sum(1 for uri in remaining if records[uri]["status"] == KEEPER)
    evicted: list[str] = []
    over = len(remaining) - cap
    if over > 0:
        oldest = sorted((records[uri]["added"], uri) for uri in remaining if records[uri]["status"] != KEEPER)
        evicted = [uri for _, uri in oldest[:over]]
    removals = disliked + expired + evicted
    out = {"kept": len(remaining) - len(evicted), "added": tally["added"], "expired": len(expired),
           "evicted": len(evicted), "disliked": len(disliked) + tally["refused"], "promoted": promoted,
           "keepers": keepers, "cap": cap, "over_cap": keepers > cap, "archive_playlist_id": pid}
    if removals:
        try:
            client.remove_items(pid, removals)
        except SpotifyError as exc:
            if exc.setup:
                raise  # sign-in/app problem: the tool maps it to the setup step
            _save_state(store, state)  # promotions stand; removals are retried next refresh
            return {**out, "kept": len(records), "expired": 0, "evicted": 0, "disliked": tally["refused"],
                    "error": "Could not prune the Archive this time; it will be retried on the next refresh."}
        store.touch_playlist(pid, ts=now)
        for uri in removals:
            records.pop(uri, None)
    _save_state(store, state)
    return out


def archive_line(report: Mapping) -> str:
    """One sentence for the agent, e.g. "Archive: 14 added, 9 aged out, 3 keepers (52 tracks)"."""
    keepers = int(report.get("keepers") or 0)
    parts = [f"{report.get('added', 0)} added", f"{report.get('expired', 0)} aged out"]
    if report.get("evicted"):
        parts.append(f"{report['evicted']} over the cap")
    if report.get("disliked"):
        parts.append(f"{report['disliked']} 👎 kept out")
    parts.append(f"{keepers} keeper{'' if keepers == 1 else 's'}")
    line = "Archive: " + ", ".join(parts) + f" ({report.get('kept', 0)} tracks)"
    if report.get("over_cap"):
        line += f"; keepers alone exceed the cap of {report.get('cap')}, all of them kept"
    return line


def refresh(client: SpotifyClient, store: Store, *, candidates: Any = None, size: Any = None,
            slots: Optional[list] = None, now: Optional[int] = None, probation_days: Any = PROBATION_DAYS_DEFAULT,
            archive_cap: Any = ARCHIVE_CAP_DEFAULT, top_up: Optional[bool] = None,
            on_slot: Optional[Callable[[int, int, str], None]] = None) -> dict:
    """Roll the stored rotation: one new edition per slot (or only the named *slots*).

    **Top-up** (a retry of a short slot): a named-slot refresh of a slot built in the current edition
    within ``TOP_UP_WINDOW_S`` keeps that slot's current tracks, adds new picks up to *size*, does not
    archive and does not start a new edition. The tracks it served in this edition do not count as
    "served recently" for it. A top-up never leaves the slot with fewer tracks than it had.
    ``top_up=False`` forces a new edition; ``True`` asks for a top-up (ignored for a slot never built).

    Returns ``{ok, edition, slots: [{slot, ok, playlist, tracks, unresolved, rejected, archived, skipped?,
    top_up?, kept?, added?, short, need?}], archive?: {…, line}}``.
    The Archive is curated (module doc) on every refresh that started a new edition.
    Raises ``SpotifyError`` only for sign-in/app problems (the tool maps them to the setup step).
    """
    now = _now(now)
    probation_days = _clamp(probation_days, PROBATION_DAYS_DEFAULT, PROBATION_DAYS_RANGE)
    archive_cap = _clamp(archive_cap, ARCHIVE_CAP_DEFAULT, ARCHIVE_CAP_RANGE)
    tally = {"added": 0, "refused": 0}
    spec = get_spec(store)
    if not spec:
        return {"ok": False, "error": "No rotation is set. Preview one (a preset or the user's own words), "
                                      "confirm it with the user, then set it."}
    size = clamp_size(size, SLOT_SIZE_DEFAULT)
    per_slot = _slot_candidates(candidates, spec["slots"])
    wanted = None
    if slots:
        wanted = {_slot_key(s) for s in slots}
        unknown = wanted - {_slot_key(s["name"]) for s in spec["slots"]}
        if unknown:
            raise ValueError(f"no slot named {', '.join(sorted(unknown))} in this rotation")
    state = get_state(store)
    ex = exclusions(store, now)
    known = known_artists(store, now)
    resolver = Resolver(client, market=store.get_meta("market"), max_searches=MAX_SEARCHES)
    library: Optional[dict] = None
    calls_before = client.calls
    report: list[dict] = []
    built = 0
    # A retry of named slots of the current edition tops them up instead of starting a new edition.
    topping = bool(wanted) and top_up is not False and all(
        _is_top_up(state, k, now) or (top_up and state["slots"].get(k)) for k in wanted)
    edition = state["edition"] if topping else state["edition"] + 1

    def library_fill(ex_now: Exclusions, n: int) -> tuple[list[Pick], list[dict]]:
        nonlocal library
        if library is None:
            library = time_capsule_sources(client, store)
        return time_capsule(**library, ex=ex_now, size=n)

    todo = [s for s in spec["slots"] if wanted is None or _slot_key(s["name"]) in wanted]
    for index, slot in enumerate(todo, 1):
        key = _slot_key(slot["name"])
        if on_slot is not None:  # progress for the Desktop card; never allowed to break the refresh
            try:
                on_slot(index, len(todo), slot["name"])
            except Exception:
                pass
        row: dict[str, Any] = {"slot": slot["name"], "mode": slot["mode"]}
        current = state["slots"].get(key)
        if current and not store.is_managed(current):
            current = None  # forgotten/not ours: start a fresh playlist, never touch the old one
        kept: list[Pick] = _current_picks(client, store, current) if topping and current else []
        slot_ex = ex
        if kept:  # this edition's own tracks are not "recently served" for this slot: they stay
            slot_ex = replace(ex, served_uris=ex.served_uris - {p.track["uri"] for p in kept})
        cands = per_slot.get(key) or []
        unresolved: list = []
        rejected: list = []
        if cands:
            picks, unresolved, rejected = from_candidates(cands, resolver, ex=slot_ex, size=size, known=known,
                                                          familiar_quota=(slot["mode"] == "discover"), seed=kept)
            row["filled_from"] = "candidates"
            if slot["mode"] == "mixed" and len(picks) < size:  # mixed: the user's own library fills the rest
                more, more_rejected = library_fill(_extend(slot_ex, picks), size - len(picks))
                picks += [p for p in more if p.track["uri"] not in {q.track["uri"] for q in picks}]
                rejected += more_rejected
                if more:
                    row["filled_from"] = "candidates+library"
        elif kept:
            picks = kept  # a top-up without new candidates keeps the slot as it is
            row["filled_from"] = "kept"
        elif slot["mode"] in ("familiar", "mixed"):
            picks, rejected = library_fill(slot_ex, size)
            row["filled_from"] = "library"
        else:
            row.update(ok=False, skipped="needs_candidates",
                       hint=f"'{slot['name']}' is a discover slot: pass candidates for it.")
            report.append(row)
            continue
        row.update(unresolved=unresolved, rejected=rejected[:20])
        if not picks:
            row.update(ok=False, skipped="nothing_survived", hint="The previous edition was kept as it is.")
            report.append(row)
            continue
        if kept and len(picks) <= len(kept):
            row.update(ok=True, top_up=True, kept=len(kept), added=0, tracks=[p.view() for p in kept],
                       short=len(kept) < size, playlist={"id": current, "url": PLAYLIST_URL + current,
                                                         "action": "unchanged", "tracks": len(kept)})
            if len(kept) < size:
                row["need"] = size - len(kept)
            ex = _extend(ex, kept)
            report.append(row)
            continue
        if current and not topping and spec["archive"] == "archive":
            previous = [t["uri"] for t in client.playlist_items(current) if str(t.get("uri", "")).startswith("spotify:track:")]
            row.update(_archive(client, store, spec, state, previous, now, probation_days, tally))
        def remember_created(pid):
            state["slots"][key] = pid
            _save_state(store, state)

        written = write_playlist(
            client, store, TEMPLATE, picks, mode="refresh" if current else "new",
            playlist_id=current, name=None if current else f"Loshify · {slot['name']}",
            params={"rotation": spec["name"], "slot": slot["name"], "edition": edition},
            description_text=_edition_description(spec, slot, edition, now), now=now, on_created=remember_created)
        state["slots"][key] = written["id"]
        state["slot_built"][key] = {"edition": edition, "ts": now}
        _save_state(store, state)
        ex = _extend(ex, picks)
        built += 1
        row.update(ok=True, playlist=written, tracks=[p.view() for p in picks], short=len(picks) < size)
        if topping:
            row.update(top_up=True, kept=len(kept), added=len(picks) - len(kept))
        if len(picks) < size:
            row["need"] = size - len(picks)
        report.append(row)

    out: dict[str, Any] = {"rotation": spec["name"], "slots": report, "searches": resolver.searches}
    if topping:
        ok = any(r.get("ok") for r in report)
        out.update(ok=ok, edition=edition, top_up=True)
        if not ok:
            out["error"] = "Nothing could be added; the slot was kept as it is."
        out["spotify_calls"] = client.calls - calls_before
        return out
    if built:
        state["edition"] = edition
        state["last_refresh_ts"] = now
        _save_state(store, state)
        out.update(ok=True, edition=edition)
        curated = _curate_archive(client, store, state, now, archive_cap, tally)
        if curated:
            out["archive"] = {**curated, "line": archive_line(curated)}
            state["last_archive"] = {**{k: int(curated.get(k) or 0) for k in LAST_ARCHIVE_FIELDS}, "ts": now}
            _save_state(store, state)  # the real counts, for the card (demo_mode) and the agent
    else:
        out.update(ok=False, error="No slot could be built; nothing was changed on Spotify.")
    out["spotify_calls"] = client.calls - calls_before
    return out


__all__ = ["ARCHIVE_CAP_DEFAULT", "ARCHIVE_POLICIES", "PROBATION_DAYS_DEFAULT", "archive_line", "last_archive", "CADENCES", "MAX_SLOTS", "MODES", "PRESETS", "clear_spec", "describe", "from_preset",
           "get_spec", "get_state", "is_due", "presets_view", "refresh", "set_spec", "status", "validate_spec"]
