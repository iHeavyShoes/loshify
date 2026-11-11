"""Tune in: the optional, recurring calibration round (5 picks, 👎 👌 ❤️).

After the first run a new round becomes available every ``interval_days`` (7–14, default 10;
``plugins.entries.loshify.settings.tune_in_days``). It is never pushed: the Desktop card shows a
small badge when it is available and "next tune-in in N days" otherwise.

The picks aim where the curator is least sure (``targets``):

- **new lanes**: artists the user met recently through Loshify with only one or two reactions;
- **edges**: artists with mixed signals (both ❤️/👌 and 👎) or a weak seed and no reaction yet;
- **recent 👎 areas**: artists turned down lately, to learn whether it was the track or the artist.

Reactions go through the normal path (``onboarding.react(..., key=ROUND_KEY)``: record only), so
they sharpen the next rotation refresh and the next portrait (the correction log).
"""

from __future__ import annotations

import json
import time
from typing import Any, Optional

from .store import DAY, Store

STATE_KEY = "tune_in"
ROUND_KEY = "tune_in_round"
INTERVAL_DEFAULT = 10
INTERVAL_RANGE = (7, 14)
RECENT_DAYS = 45
MAX_TARGETS = 8


def _now(now: Optional[int]) -> int:
    return int(time.time()) if now is None else int(now)


def clamp_interval(value: Any) -> int:
    try:
        days = int(float(value))
    except (TypeError, ValueError):
        return INTERVAL_DEFAULT
    return max(INTERVAL_RANGE[0], min(INTERVAL_RANGE[1], days))


def _state(store: Store) -> dict:
    try:
        value = json.loads(store.get_meta(STATE_KEY) or "{}")
    except (TypeError, ValueError):
        value = {}
    return value if isinstance(value, dict) else {}


def _first_run_ts(store: Store) -> int:
    """When the first run ended; 0 while it is still in progress (Tune in only comes after it).

    A user who onboarded before the guided first run existed has no onboarding state at all: for
    them the taste seed (``onboarded_at``) is the start.
    """
    raw = store.get_meta("onboarding")
    try:
        ob = json.loads(raw) if raw else None
    except (TypeError, ValueError):
        ob = None
    if isinstance(ob, dict) and ob:
        return int(ob.get("finished") or 0)
    return int(store.onboarded_at or 0)


def status(store: Store, now: Optional[int] = None, interval_days: Any = INTERVAL_DEFAULT, *,
           demo: bool = False) -> dict:
    """``{available, next_in_days, last_run, interval_days, line}``. No Spotify call.

    *demo* (the ``demo_mode`` setting, for recordings and testing): a round is available right away,
    without the 7–14-day wait. It still needs the first run (the picks aim at a known taste).
    """
    now = _now(now)
    interval = clamp_interval(interval_days)
    state = _state(store)
    last = int(state.get("last_run") or 0)
    base = last or _first_run_ts(store)
    if not base:
        return {"available": False, "eligible": False, "next_in_days": None, "last_run": None,
                "interval_days": interval, "line": "Tune in opens after your first playlists."}
    due = base + interval * DAY
    available = demo or now >= due
    days = 0 if available else max(1, -(-(due - now) // DAY))
    line = "Tune in: 5 quick picks to sharpen the next refresh" if available else \
        f"Next tune-in available in {days} day{'s' if days != 1 else ''}"
    out = {"available": available, "eligible": True, "next_in_days": days, "last_run": last or None,
           "interval_days": interval, "line": line}
    if demo:
        out["demo"] = True
    rnd = _round(store)
    if rnd.get("tracks") and int(rnd.get("created") or 0) >= base and not state.get("closed_round") == rnd.get("created"):
        out["open_round"] = True
    return out


def _round(store: Store) -> dict:
    try:
        value = json.loads(store.get_meta(ROUND_KEY) or "{}")
    except (TypeError, ValueError):
        value = {}
    return value if isinstance(value, dict) else {}


def mark_run(store: Store, now: Optional[int] = None) -> dict:
    """The user finished (or closed) a tune-in round: store the date; the next one waits a full interval."""
    now = _now(now)
    state = _state(store)
    state.update(last_run=now, closed_round=_round(store).get("created"), runs=int(state.get("runs") or 0) + 1)
    store.set_meta(STATE_KEY, json.dumps(state, separators=(",", ":")))
    return state


def targets(store: Store, now: Optional[int] = None) -> dict:
    """Where the curator is least sure: ``{new_lanes, edges, recent_downs}`` (artist names, short)."""
    now = _now(now)
    since = now - RECENT_DAYS * DAY
    per_artist: dict[str, dict] = {}
    for row in store.reactions_since(0, limit=2000):
        artists = row.get("artists") or []
        name = str(artists[0] if artists else "").strip()
        if not name:
            continue
        slot = per_artist.setdefault(name.lower(), {"name": name, "love": 0, "okay": 0, "down": 0,
                                                    "first": int(row.get("ts") or 0), "last_down": 0})
        reaction = row.get("reaction")
        if reaction in ("love", "okay", "down"):
            slot[reaction] += 1
        if reaction == "down":
            slot["last_down"] = max(slot["last_down"], int(row.get("ts") or 0))
    seeds = {r["artist"]: r for r in store.artist_rows()}
    new_lanes, edges, downs = [], [], []
    for key, a in per_artist.items():
        total = a["love"] + a["okay"] + a["down"]
        seed = float((seeds.get(key) or {}).get("seed") or 0.0)
        if a["last_down"] >= since:
            downs.append((a["last_down"], a["name"]))
        if (a["love"] or a["okay"]) and a["down"]:
            edges.append((total, a["name"]))
        elif a["first"] >= since and total <= 2 and seed < 0.5 and not a["down"]:
            new_lanes.append((a["first"], a["name"]))
    for key, row in seeds.items():  # weak seeds never reacted to: the edge of the known map
        if key not in per_artist and 0.0 < float(row.get("seed") or 0.0) < 0.5:
            edges.append((0, str(row.get("display") or key)))
    return {"new_lanes": [n for _t, n in sorted(new_lanes, reverse=True)][:MAX_TARGETS],
            "edges": [n for _t, n in sorted(edges, reverse=True)][:MAX_TARGETS],
            "recent_downs": [n for _t, n in sorted(downs, reverse=True)][:MAX_TARGETS]}


__all__ = ["INTERVAL_DEFAULT", "INTERVAL_RANGE", "ROUND_KEY", "STATE_KEY", "clamp_interval", "mark_run", "status",
           "targets"]
