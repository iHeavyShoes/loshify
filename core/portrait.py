"""Portrait inference: what the listening data supports about a listener, with weights and confidence.

Generic by design (no per-user tuning). Principles:

- **Evidence weight.** Actual listening (top artists/tracks for short/medium/long term, recently
  played) outweighs saves. A save is a single click; a top-artist rank is weeks of plays.
- **Saves are bursty.** Library imports and one-time binges put hundreds of saves into one year or
  month. Saves are normalised per period: each year's influence is capped (relative to the median
  year), and *bursts* (a year with > ``BURST_RATIO`` x the median year, a month with a disproportionate
  share, or one album dominating a year) are flagged as a *phase*, never as the identity.
- **Recency.** Short/medium term lead ("now"); long term and old saves describe "where you came
  from" ("roots"). Old saves decay with a half-life.
- **Only what the data supports.** Every claim carries ``confidence`` (high / medium / low), the
  independent ``sources`` behind it and a ``hedge`` phrase ("", "seems", "looks like"). A claim from
  a single source is never ``high``; a saves-only claim is at most ``low``.
- **Correction loop.** ``corrections`` (the portrait card's "Not quite" plus anchors and the
  calibration / tune-in reactions) feed the next portrait: corrected-up artists are boosted,
  👎 artists are dropped from the lead, and a "Not quite" lowers confidence of untouched claims.

``portrait_facts`` is pure; ``corrections_from_store`` reads the stored feedback.
"""

from __future__ import annotations

import json
import statistics
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional

from .text import artist_key

DAY = 86400
RANGES = ("short_term", "medium_term", "long_term")

# Evidence weights per signal (per artist occurrence). Listening >> saves.
RANGE_WEIGHT = {"short_term": 3.0, "medium_term": 2.0, "long_term": 1.0}
TOP_TRACK_FACTOR = 0.5        # a top track adds half its range weight to its primary artist
RANK_FLOOR = 0.4              # rank decay: #1 counts 1.0, the last of 50 counts ~RANK_FLOOR
RECENT_PER_PLAY = 0.4
RECENT_ARTIST_CAP = 3.0       # one artist on repeat in the last 50 plays cannot dominate
SAVE_WEIGHT = 0.15            # per (normalised) saved track
SAVE_HALF_LIFE_DAYS = 2 * 365  # old saves fade toward "roots"
YEAR_CAP_FACTOR = 2.0         # a year contributes at most this x the median year's saves
YEAR_CAP_MIN = 25             # ... but never caps below this (small libraries are left alone)
BURST_RATIO = 3.0             # a year with > 3x the median year's saves is a burst
BURST_MIN = 30                # ... and at least this many saves
MONTH_BURST_SHARE = 0.5       # one month holding half of a year's saves (an import/binge)
MONTH_BURST_MIN = 25
ALBUM_DOMINANCE = 0.35        # one album with >= 35% of a year's saves
ALBUM_DOMINANCE_MIN = 8
BURST_DAMP = 0.35             # a burst period's saves count this much on top of the cap
CORRECTION_LOVE = 4.0         # an anchor / ❤️ after the portrait
CORRECTION_OKAY = 1.0

THIN_SIGNALS = 12             # fewer listening data points than this: "thin" evidence
RICH_SIGNALS = 60
SINGLE_LANE_SHARE = 0.6       # one genre >= 60% of the weighted genre mass: a focused listener
LEAD_N = 6
ROOTS_N = 6

HEDGE = {"high": "", "medium": "seems", "low": "looks like"}
LISTENING_SOURCES = ("top_short", "top_medium", "top_long", "top_tracks", "recent")


def _when(iso: Any) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")) if iso else None
    except ValueError:
        return None


def _primary(track: Mapping) -> str:
    artists = (track or {}).get("artists") or []
    first = artists[0] if artists else {}
    return str((first.get("name") if isinstance(first, Mapping) else first) or "").strip()


def _rank_factor(i: int, n: int) -> float:
    return 1.0 if n <= 1 else 1.0 - (1.0 - RANK_FLOOR) * (i / (n - 1))


class _Acc:
    """Per-artist accumulator: weight per bucket (now / roots) and the sources behind it."""

    def __init__(self) -> None:
        self.display: dict[str, str] = {}
        self.now: Counter = Counter()
        self.roots: Counter = Counter()
        self.sources: dict[str, set] = defaultdict(set)
        self.genres: dict[str, list] = {}

    def add(self, name: str, *, now: float = 0.0, roots: float = 0.0, source: str) -> Optional[str]:
        name = str(name or "").strip()
        key = artist_key(name)
        if not key:
            return None
        self.display.setdefault(key, name)
        self.now[key] += now
        self.roots[key] += roots
        self.sources[key].add(source)
        return key


def save_periods(saved: Iterable[Mapping]) -> dict:
    """Per-year save stats, the per-year cap and the burst flags (pure; exposed for tests)."""
    by_year: Counter = Counter()
    by_month: Counter = Counter()
    albums_by_year: dict[int, Counter] = defaultdict(Counter)
    album_artist: dict[str, str] = {}
    artists_by_year: dict[int, Counter] = defaultdict(Counter)
    for row in saved or []:
        when = _when(row.get("added_at"))
        if not when:
            continue
        track = row.get("track") or {}
        by_year[when.year] += 1
        by_month[(when.year, when.month)] += 1
        album = str((track.get("album") or {}).get("name") or "")
        artist = _primary(track)
        if album:
            albums_by_year[when.year][album] += 1
            album_artist.setdefault(album, artist)
        if artist:
            artists_by_year[when.year][artist] += 1
    counts = [by_year[y] for y in sorted(by_year)]
    median = statistics.median(counts) if counts else 0
    cap = max(YEAR_CAP_MIN, YEAR_CAP_FACTOR * median)
    years: dict[str, dict] = {}
    bursts: list[dict] = []
    for year in sorted(by_year):
        n = by_year[year]
        flags: list[str] = []
        ratio = (n / median) if median else 0.0
        if len(counts) >= 3 and n >= BURST_MIN and ratio > BURST_RATIO:
            flags.append("year_spike")
        month_n = max((c for (y, _m), c in by_month.items() if y == year), default=0)
        if n >= MONTH_BURST_MIN and month_n >= MONTH_BURST_MIN and month_n / n >= MONTH_BURST_SHARE:
            flags.append("month_import")
        top_album, album_n = (albums_by_year[year].most_common(1) or [("", 0)])[0]
        if album_n >= ALBUM_DOMINANCE_MIN and album_n / n >= ALBUM_DOMINANCE:
            flags.append("album_binge")
        top_artist = (artists_by_year[year].most_common(1) or [("", 0)])[0][0]
        counted = min(n, cap) * (BURST_DAMP if flags else 1.0)
        info = {"saves": n, "counted": round(counted, 1), "vs_median": round(ratio, 1), "top_artist": top_artist}
        if flags:
            info["burst"] = flags
            phase = {"year": year, "saves": n, "why": flags, "top_artist": top_artist,
                     "vs_median": round(ratio, 1)}
            if "album_binge" in flags:
                phase["album"] = {"name": top_album, "artist": album_artist.get(top_album, ""), "saves": album_n}
            bursts.append(phase)
        years[str(year)] = info
    return {"years": years, "median_per_year": median, "year_cap": cap, "bursts": bursts,
            "albums": {a: (album_artist.get(a, ""), sum(c[a] for c in albums_by_year.values()))
                       for a in {a for c in albums_by_year.values() for a in c}}}


def _confidence(sources: set, share: float) -> str:
    listening = sources & set(LISTENING_SOURCES)
    if len(listening) >= 3 or (len(listening) >= 2 and share >= 0.08):
        return "high"
    if listening and len(sources) >= 2:
        return "medium"
    if listening and share >= 0.15:
        return "medium"
    return "low"


def _claim(kind: str, text: str, confidence: str, sources: Iterable[str], **extra: Any) -> dict:
    return {"kind": kind, "claim": text, "confidence": confidence, "hedge": HEDGE[confidence],
            "sources": sorted(set(sources)), **extra}


def portrait_facts(top_artists: Mapping[str, list], top_tracks: Mapping[str, list], saved: list, recent: list,
                   now: Optional[int] = None, corrections: Optional[Mapping] = None) -> dict:
    """Weighted, flagged, confidence-scored facts for a portrait. Pure; every list is short.

    Keeps the legacy keys (``eras``, ``recurring_albums``, ``saved_per_year``,
    ``top_saved_artist_per_year``, ``forgotten_loves``, ``listening_span``, ``counts``) and adds
    ``evidence``, ``weights``, ``lead``, ``roots``, ``phases``, ``claims``, ``genres``,
    ``corrections`` and ``guidance``.
    """
    now = int(time.time()) if now is None else int(now)
    top_artists = top_artists or {}
    top_tracks = top_tracks or {}
    saved = list(saved or [])
    recent = list(recent or [])
    acc = _Acc()
    genre_mass: Counter = Counter()
    genre_total_w = [0.0]  # weight of genre-tagged artist entries (the coverage denominator)

    # ── listening (the lead) ───────────────────────────────────────────────────────────────
    for rng in RANGES:
        artists = [a for a in (top_artists.get(rng) or []) if a.get("name")]
        src = {"short_term": "top_short", "medium_term": "top_medium", "long_term": "top_long"}[rng]
        for i, artist in enumerate(artists):
            w = RANGE_WEIGHT[rng] * _rank_factor(i, len(artists))
            key = acc.add(artist["name"], now=w if rng != "long_term" else 0.0,
                          roots=w if rng == "long_term" else 0.0, source=src)
            g = [str(x) for x in (artist.get("genres") or []) if x]
            if key and g:
                acc.genres.setdefault(key, g)
                for genre in dict.fromkeys(g):  # coverage, in listed order (deterministic ties)
                    genre_mass[genre] += w
                genre_total_w[0] += w
        tracks = [t for t in (top_tracks.get(rng) or []) if _primary(t)]
        for i, track in enumerate(tracks):
            w = RANGE_WEIGHT[rng] * TOP_TRACK_FACTOR * _rank_factor(i, len(tracks))
            acc.add(_primary(track), now=w if rng != "long_term" else 0.0,
                    roots=w if rng == "long_term" else 0.0, source="top_tracks")
    recent_counts = Counter(_primary(r.get("track") or {}) for r in recent)
    for name, n in recent_counts.items():
        if name:
            acc.add(name, now=min(RECENT_ARTIST_CAP, n * RECENT_PER_PLAY), source="recent")

    # ── saves (normalised per period, bursts damped, old ones fade into roots) ─────────────
    periods = save_periods(saved)
    cap, burst_years = periods["year_cap"], {b["year"] for b in periods["bursts"]}
    year_n = Counter()
    for row in saved:
        when = _when(row.get("added_at"))
        if when:
            year_n[when.year] += 1
    for row in saved:
        when = _when(row.get("added_at"))
        name = _primary(row.get("track") or {})
        if not when or not name:
            continue
        n = year_n[when.year]
        scale = min(1.0, cap / n) if n else 1.0
        if when.year in burst_years:
            scale *= BURST_DAMP
        age_days = max(0.0, (now - when.timestamp()) / DAY)
        fade = 0.5 ** (age_days / SAVE_HALF_LIFE_DAYS)
        w = SAVE_WEIGHT * scale
        acc.add(name, now=w * fade, roots=w * (1.0 - fade), source="saves")

    # ── corrections (the "Not quite" + calibration loop) ───────────────────────────────────
    corr = dict(corrections or {})
    downs = {artist_key(a) for a in corr.get("down_artists") or []}
    for name in corr.get("love_artists") or []:
        acc.add(name, now=CORRECTION_LOVE, source="your_feedback")
    for name in corr.get("okay_artists") or []:
        acc.add(name, now=CORRECTION_OKAY, source="your_feedback")
    not_quite = bool(corr.get("not_quite"))

    total_now = sum(v for k, v in acc.now.items() if k not in downs) or 1.0
    total_roots = sum(acc.roots.values()) or 1.0
    # Corrected-up artists (anchors, ❤️ in calibration / tune-in) are pinned into the lead: the user
    # told us directly, which outranks any amount of inference. At most half the lead.
    pinned = [artist_key(a) for a in corr.get("love_artists") or [] if artist_key(a) and artist_key(a) not in downs]
    pinned = list(dict.fromkeys(pinned))[:LEAD_N // 2]
    ranked = [k for k, v in acc.now.most_common() if v > 0 and k not in downs and k not in pinned]
    lead_keys = (ranked[:LEAD_N - len(pinned)][:2] + pinned + ranked[2:LEAD_N - len(pinned)])[:LEAD_N]
    roots_keys = [k for k, v in acc.roots.most_common() if v > 0 and k not in lead_keys][:ROOTS_N]

    def view(key: str, bucket: Counter, total: float) -> dict:
        share = bucket[key] / total
        conf = _confidence(acc.sources[key], share)
        if not_quite and "your_feedback" not in acc.sources[key] and conf == "high":
            conf = "medium"
        return {"artist": acc.display[key], "weight": round(bucket[key], 2), "share": round(share, 3),
                "confidence": conf, "sources": sorted(acc.sources[key])}

    lead = [view(k, acc.now, total_now) for k in lead_keys]
    roots = [view(k, acc.roots, total_roots) for k in roots_keys]

    # ── evidence strength ──────────────────────────────────────────────────────────────────
    present = {
        "top_short": bool(top_artists.get("short_term")), "top_medium": bool(top_artists.get("medium_term")),
        "top_long": bool(top_artists.get("long_term")),
        "top_tracks": any(top_tracks.get(r) for r in RANGES), "recent": bool(recent), "saves": bool(saved),
    }
    listening_points = (sum(len(top_artists.get(r) or []) for r in RANGES)
                        + sum(len(top_tracks.get(r) or []) for r in RANGES) + len(recent))
    strength = ("thin" if listening_points < THIN_SIGNALS
                else "rich" if listening_points >= RICH_SIGNALS and sum(present.values()) >= 4 else "some")

    # ── genres ─────────────────────────────────────────────────────────────────────────────
    genre_total = genre_total_w[0]
    genres = [{"genre": g, "share": round(min(1.0, m / genre_total), 3)}
              for g, m in genre_mass.most_common(5)] if genre_total else []

    # ── claims ─────────────────────────────────────────────────────────────────────────────
    claims: list[dict] = []
    if lead:
        names = [x["artist"] for x in lead[:3]]
        srcs = set().union(*(set(x["sources"]) for x in lead[:3]))
        conf = lead[0]["confidence"] if strength != "thin" else "low"
        claims.append(_claim("lead", f"Right now: {', '.join(names)}", conf, srcs, artists=names))
    if roots:
        names = [x["artist"] for x in roots[:3]]
        srcs = set().union(*(set(x["sources"]) for x in roots[:3]))
        claims.append(_claim("roots", f"Where you came from: {', '.join(names)}",
                             "medium" if "top_long" in srcs else "low", srcs, artists=names))
    for phase in periods["bursts"][:3]:
        who = phase["album"]["name"] if "album" in phase else (phase["top_artist"] or "saving")
        claims.append(_claim("phase", f"{phase['year']}: a {who} phase", "low", {"saves"}, year=phase["year"],
                             who=who, why=phase["why"]))
    if genres and genres[0]["share"] >= SINGLE_LANE_SHARE:
        top = genres[0]
        conf = "high" if strength == "rich" and top["share"] >= 0.75 else "medium"
        claims.append(_claim("focus", f"Mostly {top['genre']}", conf, {"genres"}, share=top["share"]))
    elif len(genres) >= 3:
        claims.append(_claim("range", "Wide range: " + ", ".join(g["genre"] for g in genres[:3]),
                             "medium" if strength != "thin" else "low", {"genres"}))
    lately = {artist_key(x) for x in (a.get("name") for a in top_artists.get("short_term") or []) if x}
    lately |= {artist_key(n) for n in recent_counts if n}
    cutoff = now - 365 * DAY
    forgotten = []
    for row in saved:
        when = _when(row.get("added_at"))
        track = row.get("track") or {}
        artist = _primary(track)
        if when and when.timestamp() < cutoff and artist and artist_key(artist) not in lately:
            forgotten.append({"title": track.get("name", ""), "artist": artist, "saved": when.year})
    if forgotten:
        claims.append(_claim("forgotten", f"{len(forgotten)} saved songs you have not played lately",
                             "medium" if present["recent"] or present["top_short"] else "low",
                             {"saves", "recent"} if present["recent"] else {"saves"}))

    # ── legacy shape (kept for the chat path and existing readers) ─────────────────────────
    eras = {rng: [a.get("name") for a in (top_artists.get(rng) or [])[:6] if a.get("name")] for rng in RANGES}
    if not any(eras.values()):
        eras = {rng: list(dict.fromkeys(_primary(t) for t in (top_tracks.get(rng) or []) if _primary(t)))[:6]
                for rng in RANGES}
    albums_sorted = sorted(periods["albums"].items(), key=lambda kv: -kv[1][1])
    span = sorted(int(y) for y in periods["years"])
    return {
        "eras": {"lately": eras["short_term"], "last_months": eras["medium_term"], "all_time": eras["long_term"]},
        "recurring_albums": [{"album": a, "artist": art, "saved_tracks": n} for a, (art, n) in albums_sorted[:5] if n >= 3],
        "saved_per_year": {y: v["saves"] for y, v in periods["years"].items()},
        "top_saved_artist_per_year": {y: v["top_artist"] for y, v in periods["years"].items() if v["top_artist"]},
        "forgotten_loves": {"count": len(forgotten), "examples": forgotten[:5]},
        "listening_span": {"first_saved": span[0], "last_saved": span[-1]} if span else {},
        "counts": {"saved_tracks": len(saved), "recent_plays": len(recent),
                   "top_artists": sum(len(v or []) for v in top_artists.values())},
        "evidence": {"strength": strength, "listening_points": listening_points, "sources": present},
        "weights": {"lead": "short/medium-term top artists+tracks, recent plays (capped per artist), recent saves",
                    "roots": "long-term top, old saves (faded with a 2-year half-life)",
                    "saves": {"per_track": SAVE_WEIGHT, "year_cap": periods["year_cap"],
                              "median_per_year": periods["median_per_year"], "burst_damp": BURST_DAMP}},
        "save_years": periods["years"],
        "lead": lead,
        "roots": roots,
        "phases": periods["bursts"],
        "genres": genres,
        "claims": claims,
        "corrections": {"not_quite": not_quite, "applied": sorted(set(corr.get("love_artists") or [])
                                                                 | set(corr.get("okay_artists") or [])),
                        "dropped": sorted(acc.display.get(k, k) for k in downs)},
        "guidance": _guidance(strength, bool(periods["bursts"]), not_quite),
    }


def _guidance(strength: str, has_bursts: bool, not_quite: bool) -> str:
    parts = ["Lead with `lead` (what they play now). `roots` and `phases` are 'where you came from', never the "
             "headline. Use each claim's `hedge` word; never state a `low` claim as fact."]
    if strength == "thin":
        parts.append("Evidence is thin: 2 short sentences, all hedged ('looks like…'), and say the picture "
                     "sharpens with reactions.")
    if has_bursts:
        parts.append("A burst year/month is a phase (an import or a binge): one clause at most.")
    if not_quite:
        parts.append("They said 'Not quite' last time: lean on `corrections.applied`, avoid `corrections.dropped`.")
    return " ".join(parts)


def _meta_json(store: Any, key: str, default: Any) -> Any:
    try:
        value = json.loads(store.get_meta(key) or "null")
    except (TypeError, ValueError):
        return default
    return default if value is None else value


def corrections_from_store(store: Any) -> dict:
    """The feedback the next portrait must honour: "Not quite", anchors, calibration / tune-in reactions.

    The newest word per artist wins (a ❤️ after a 👎 lifts the 👎, and the other way round).
    """
    fb = _meta_json(store, "portrait_feedback", {})
    last: dict[str, tuple[int, str, str]] = {}  # artist_key -> (ts, reaction, display)
    for a in _meta_json(store, "anchors", []):
        name = str((a or {}).get("artist") or "").strip() if isinstance(a, Mapping) else ""
        if name:
            last[artist_key(name)] = (0, "love", name)
    log = _meta_json(store, "calibration_log", [])
    for row in sorted((r for r in log if isinstance(r, Mapping)), key=lambda r: int(r.get("ts") or 0)):
        name = str(row.get("artist") or "").strip()
        if name and row.get("reaction") in ("love", "okay", "down"):
            last[artist_key(name)] = (int(row.get("ts") or 0), str(row["reaction"]), name)
    out: dict[str, Any] = {"not_quite": isinstance(fb, Mapping) and fb.get("verdict") == "not_quite",
                           "love_artists": [], "okay_artists": [], "down_artists": []}
    for _ts, reaction, name in sorted(last.values(), key=lambda v: v[0]):
        out[f"{reaction}_artists"].append(name)
    return out


__all__ = ["HEDGE", "corrections_from_store", "portrait_facts", "save_periods"]
