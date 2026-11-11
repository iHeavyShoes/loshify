"""First-run onboarding beyond listening history: anchors, a 5-track calibration round, the open note.

The order is fixed and every step is skippable (one tap or one line each):

    portrait → anchors → calibration → rotation → note → first_edition

- **portrait**: the agent's 3–4 sentences from ``setup.onboard``'s facts (saved with ``set_portrait``).
- **anchors** ("what did I miss?"): up to 3 artists or songs that are really the user. Each is resolved
  on Spotify (a song through the same ``Resolver`` as curation, an artist through a track search whose
  credits must match) and stored as a strong positive seed: the artist gets a ❤️'s weight in the
  ``love`` aggregate, so it decays exactly like a ❤️; a song anchor also joins the loved tracks.
  Re-saving the same anchor does not add weight twice. The anchors are kept in ``meta`` so the
  portrait refresh, the summary and the brief can name them.
- **calibration**: the agent proposes picks tagged ``familiar`` / ``near`` / ``stretch``; they are
  resolved and filtered like any curated track (👎, avoid-artists, duplicates, ≤ 2 per artist) and
  the round keeps 5: 2 familiar, 2 near, 1 stretch (backfilled from what resolved). The user reacts
  👎 👌 ❤️ in the Desktop *Taste* tab or in chat; every reaction goes through ``reactions.record``
  (the normal path), so the taste memory learns from minute one. A second tap on the same track
  changes that reaction instead of adding another. No playlist is written and playback is optional
  (without Premium the list and its buttons still work).
- **note**: one short free-text line ("a genre, a vibe inside a genre, things to avoid…"), stored by
  ``taste.set_user_note`` (validated, length-capped, never decayed, editable later).

Progress lives in ``meta['onboarding']``; only a flow started by ``start`` (first onboarding or
``refresh``) is tracked, so users who onboarded before this existed are never nagged.
"""

from __future__ import annotations

import json
import time
from typing import Any, Mapping, Optional

from . import reactions as rx
from . import taste
from .curate import Picker, Resolver, exclusions, known_artists, primary_artist
from .spotify import SpotifyClient
from .store import Store
from .text import artist_key, clip, is_meta_reason, similarity, title_key

STEPS = ("portrait", "anchors", "calibration", "rotation", "note", "first_edition")
DONE, SKIPPED, TODO = "done", "skipped", "todo"
STATE_KEY = "onboarding"
ANCHORS_KEY = "anchors"
CALIBRATION_KEY = "calibration"

MAX_ANCHORS = 3
ANCHOR_TEXT_MAX = 120
ANCHOR_SEARCHES = 12          # ≤ 3 anchors × (2 orders × 2 queries)
ANCHOR_WEIGHT = taste.WEIGHT["love"]

CAL_SIZE = 5
CAL_KINDS = ("familiar", "near", "stretch")
CAL_TARGET = {"familiar": 2, "near": 2, "stretch": 1}
CAL_MAX_CANDIDATES = 10       # 5 + backups, so one unresolved pick does not cost a round trip
CAL_SEARCHES = 20
CAL_REASON_MAX = 120

ASK = {
    "portrait": ("Write a 3-4 sentence portrait from portrait_facts and save its gist with "
                 "loshify_taste(action='set_portrait')."),
    "anchors": ("Ask: \"Did I miss anything? Name up to 3 artists or songs that are really you, or skip.\" "
                "Then loshify_onboard(action='anchors', anchors=[...]) and refresh the portrait in one line that "
                "mentions them (set_portrait)."),
    "calibration": ("Propose 5 quick picks, 2 familiar, 2 near, 1 stretch (you may add up to 5 backups): "
                    "loshify_onboard(action='calibrate', picks=[{title, artist, kind, reason}]). Show the list; the "
                    "user reacts 👎 👌 ❤️ in the Desktop Taste tab or here (action='react'). Playback is optional."),
    "rotation": ("Ask ONE question: how do they like their music organized? loshify_rotation(action='presets'), "
                 "offer the presets plus 'in your own words'; preview, confirm, set."),
    "note": ("Ask: \"Anything else I should take into account? A genre, a vibe inside a genre, things to avoid…\" "
             "One line, or skip. Save it with loshify_onboard(action='note', text=...)."),
    "first_edition": ("Build the first edition: loshify_rotation(action='refresh', candidates=...) honouring the "
                      "anchors, the calibration reactions and the note; or loshify_curate(template='time_capsule') "
                      "if they skipped the rotation."),
}


def _now(now: Optional[int]) -> int:
    return int(now if now is not None else time.time())


def _load(store: Store, key: str, default: Any) -> Any:
    raw = store.get_meta(key)
    if not raw:
        return default
    try:
        value = json.loads(raw)
    except ValueError:
        return default
    return value if isinstance(value, type(default)) else default


def _save(store: Store, key: str, value: Any) -> None:
    store.set_meta(key, json.dumps(value, ensure_ascii=False, separators=(",", ":")))


# ── progress ───────────────────────────────────────────────────────────────────────────────────

def start(store: Store, now: Optional[int] = None) -> dict:
    """Begin (or restart) the flow. Anchors, calibration history and the note are kept."""
    _save(store, STATE_KEY, {"started": _now(now), "steps": {}})
    return status(store, now)


def restart(store: Store, now: Optional[int] = None) -> dict:
    """"Start over": forget the first-run progress and the old calibration round, so "Get to know me"
    can run again from the top.

    Kept: every reaction (the reactions table, including those made on the old picks), the correction log
    the next portrait reads, the anchors, the note, the taste seed and the rotation with its playlists.
    """
    store.set_meta(STATE_KEY, None)
    store.set_meta(CALIBRATION_KEY, None)
    return status(store, now)


def _state(store: Store) -> dict:
    return _load(store, STATE_KEY, {})


def active(store: Store) -> bool:
    state = _state(store)
    return bool(state.get("started")) and not state.get("finished")


def mark(store: Store, step: str, outcome: str = DONE, now: Optional[int] = None) -> bool:
    """Record a step as done/skipped. A no-op (False) when no flow is running."""
    if step not in STEPS:
        raise ValueError(f"step must be one of {', '.join(STEPS)}")
    if outcome not in (DONE, SKIPPED):
        raise ValueError("outcome must be done or skipped")
    state = _state(store)
    if not state.get("started") or state.get("finished"):
        return False
    steps = dict(state.get("steps") or {})
    if steps.get(step) == DONE and outcome == SKIPPED:
        return False  # a done step stays done
    steps[step] = outcome
    state["steps"] = steps
    if all(steps.get(s) in (DONE, SKIPPED) for s in STEPS):
        state["finished"] = _now(now)
    _save(store, STATE_KEY, state)
    return True


def status(store: Store, now: Optional[int] = None) -> dict:
    """Where the flow is, what to ask next, and the onboarding data (no Spotify call)."""
    state = _state(store)
    steps = state.get("steps") or {}
    view = [{"step": s, "state": steps.get(s, TODO)} for s in STEPS]
    started = bool(state.get("started"))
    nxt = next((s for s in STEPS if steps.get(s) not in (DONE, SKIPPED)), None) if started else None
    out: dict[str, Any] = {
        "active": started and not state.get("finished"), "started": started,
        "finished": bool(state.get("finished")), "steps": view, "next_step": nxt,
        "anchors": anchors(store), "calibration": calibration_view(store),
        "note": taste.get_user_note(store), "note_max": taste.USER_NOTE_MAX,
    }
    if nxt:
        out["ask"] = ASK[nxt]
    return out


# ── anchors ────────────────────────────────────────────────────────────────────────────────────

def anchors(store: Store) -> list[dict]:
    return [a for a in _load(store, ANCHORS_KEY, []) if isinstance(a, dict)][:MAX_ANCHORS]


def _anchor_id(item: Mapping) -> str:
    return f"{item.get('kind')}|{artist_key(item.get('artist', ''))}|{title_key(item.get('title', ''))}"


def parse_anchor(raw: Any) -> dict:
    """``{title, artist}`` from a string or mapping. A string may be "Artist", "Song by Artist",
    "Song - Artist" or "Artist - Song" (both dash orders are tried when resolving)."""
    if isinstance(raw, Mapping):
        title, artist = str(raw.get("title") or "").strip(), str(raw.get("artist") or "").strip()
        if not artist and not title:
            raise ValueError("an anchor needs an artist (and optionally a title)")
        if not artist:  # a bare title is looked up as an artist-or-song line
            return parse_anchor(title)
        return {"title": clip(title, ANCHOR_TEXT_MAX), "artist": clip(artist, ANCHOR_TEXT_MAX), "orders": None}
    text = " ".join(str(raw or "").split())
    if not text:
        raise ValueError("an anchor cannot be empty")
    if len(text) > ANCHOR_TEXT_MAX:
        raise ValueError(f"an anchor is at most {ANCHOR_TEXT_MAX} characters")
    low = text.casefold()
    at = low.rfind(" by ")
    if at > 0:
        return {"title": text[:at].strip(), "artist": text[at + 4:].strip(), "orders": None}
    for sep in (" – ", " — ", " - "):
        if sep in text:
            left, right = (p.strip() for p in text.split(sep, 1))
            if left and right:
                return {"title": left, "artist": right, "orders": [(left, right), (right, left)]}
    return {"title": "", "artist": text, "orders": None}


def _credited(track: Mapping, wanted: str) -> str:
    names = [str(a.get("name")) for a in track.get("artists") or [] if isinstance(a, Mapping) and a.get("name")]
    key = artist_key(wanted)
    return max(names, key=lambda n: similarity(key, artist_key(n)), default=primary_artist(track))


def resolve_anchor(resolver: Resolver, parsed: Mapping) -> tuple[Optional[dict], str]:
    """``(anchor, "")`` or ``(None, why)``. Songs via the curation resolver; artists via credits."""
    if parsed.get("title"):
        orders = parsed.get("orders") or [(parsed["title"], parsed["artist"])]
        why = "not_found"
        for title, artist in orders:
            track, why = resolver.resolve(title, artist)
            if track:
                return {"kind": "song", "title": track.get("name", title), "artist": _credited(track, artist),
                        "uri": track["uri"]}, ""
            if why in ("budget", "rate_limited"):
                return None, why
        if parsed.get("orders"):  # "X - Y" may also be one artist's name with a dash in it
            hit, _ = _resolve_artist(resolver, f"{parsed['title']} - {parsed['artist']}")
            if hit:
                return hit, ""
        return None, why
    return _resolve_artist(resolver, parsed["artist"])


def _resolve_artist(resolver: Resolver, name: str) -> tuple[Optional[dict], str]:
    from .curate import ARTIST_MIN

    want = artist_key(name)
    if not want:
        return None, "bad_candidate"
    for query in (f'artist:"{name.replace(chr(34), " ")}"', name):
        results = resolver.search(query, 10)
        if results is None:
            return None, "rate_limited" if resolver.rate_limited else "budget"
        best, best_score = "", 0.0
        for track in results:
            for credit in track.get("artists") or []:
                credit_name = str((credit or {}).get("name") or "")
                score = similarity(want, artist_key(credit_name))
                if score >= ARTIST_MIN and score > best_score:
                    best, best_score = credit_name, score
        if best:
            return {"kind": "artist", "title": "", "artist": best}, ""
    return None, "not_found"


def set_anchors(client: SpotifyClient, store: Store, items: Any, now: Optional[int] = None) -> dict:
    """Resolve up to 3 anchors and store the ones found (replacing the previous list).

    Newly anchored artists get a ❤️'s weight (decaying like a ❤️); song anchors also become loved
    tracks. Returns ``{ok, anchors, unresolved, added, say}``. Nothing is stored if none resolved.
    """
    now = _now(now)
    if not isinstance(items, list) or not items:
        raise ValueError("anchors must be a list of 1-3 artists or songs")
    if len(items) > MAX_ANCHORS:
        raise ValueError(f"at most {MAX_ANCHORS} anchors")
    parsed = [parse_anchor(i) for i in items]
    resolver = Resolver(client, market=store.get_meta("market"), max_searches=ANCHOR_SEARCHES)
    found: list[dict] = []
    unresolved: list[dict] = []
    for raw, p in zip(items, parsed):
        hit, why = resolve_anchor(resolver, p)
        label = raw if isinstance(raw, str) else " – ".join(x for x in (p["title"], p["artist"]) if x)
        if hit is None:
            unresolved.append({"anchor": clip(str(label), ANCHOR_TEXT_MAX), "why": why})
        elif _anchor_id(hit) not in {_anchor_id(f) for f in found}:
            found.append(hit)
    if not found:
        return {"ok": False, "anchors": anchors(store), "unresolved": unresolved,
                "error": "None of those could be found on Spotify; check the spelling or name another."}
    previous = {_anchor_id(a): a for a in anchors(store)}
    added = 0
    for hit in found:
        prior = previous.get(_anchor_id(hit))
        hit["ts"] = int(prior["ts"]) if prior and prior.get("ts") else now
        if prior:
            continue  # weight was given when it was first anchored
        taste.add_artist_love(store, hit["artist"], ANCHOR_WEIGHT, now)
        if hit["kind"] == "song":
            store.unblock(hit["uri"])
            store.add_loved(hit["uri"], hit["title"], hit["artist"], now)
        added += 1
    _save(store, ANCHORS_KEY, found)
    mark(store, "anchors", DONE, now)
    names = "; ".join(anchor_label(a) for a in found)
    return {"ok": True, "anchors": found, "unresolved": unresolved, "added": added,
            "say": f"Refresh the portrait in one line that mentions: {names} (loshify_taste set_portrait)."}


def anchor_label(a: Mapping) -> str:
    return f"{a['title']} – {a['artist']}" if a.get("kind") == "song" and a.get("title") else str(a.get("artist", ""))


# ── calibration ────────────────────────────────────────────────────────────────────────────────

def _calibration(store: Store, key: str = CALIBRATION_KEY) -> dict:
    cal = _load(store, key, {})
    cal.setdefault("tracks", [])
    cal.setdefault("reactions", {})
    return cal


def calibration_view(store: Store, key: str = CALIBRATION_KEY) -> Optional[dict]:
    cal = _calibration(store, key)
    if not cal["tracks"]:
        return None
    tracks = []
    for i, t in enumerate(cal["tracks"], 1):
        r = cal["reactions"].get(t["uri"]) or {}
        tracks.append({"n": i, "title": t["title"], "artists": t["artists"], "uri": t["uri"], "kind": t["kind"],
                       "reason": t.get("reason", ""), "reaction": r.get("reaction")})
    reacted = sum(1 for t in tracks if t["reaction"])
    return {"tracks": tracks, "reacted": reacted, "total": len(tracks), "complete": reacted == len(tracks),
            "created": cal.get("created")}


def _compose(accepted: list[tuple[dict, str, str]]) -> list[tuple[dict, str, str]]:
    """Pick 5 from resolved ``(track, kind, reason)``: 2 familiar, 2 near, 1 stretch, backfilled."""
    chosen: list[tuple[dict, str, str]] = []
    for kind in CAL_KINDS:
        chosen += [a for a in accepted if a[1] == kind][:CAL_TARGET[kind]]
    leftovers = [a for a in accepted if a not in chosen]
    has_stretch = any(a[1] == "stretch" for a in chosen)
    for a in leftovers:  # backfill: never a second stretch while the round has one
        if len(chosen) >= CAL_SIZE:
            break
        if a[1] == "stretch" and has_stretch:
            continue
        chosen.append(a)
        has_stretch = has_stretch or a[1] == "stretch"
    order = {k: i for i, k in enumerate(CAL_KINDS)}
    return sorted(chosen[:CAL_SIZE], key=lambda a: order[a[1]])  # familiar → near → the stretch last


def propose(client: SpotifyClient, store: Store, picks: Any, now: Optional[int] = None, *,
            key: str = CALIBRATION_KEY) -> dict:
    """Resolve the agent's calibration picks and store the round of 5 (replacing any earlier round).

    *key*: ``calibration`` (first run) or ``tune_in_round`` (the recurring Tune-in round).
    """
    now = _now(now)
    if not isinstance(picks, list) or not picks:
        raise ValueError("picks must be a list of {title, artist, kind, reason}")
    if len(picks) > CAL_MAX_CANDIDATES:
        raise ValueError(f"at most {CAL_MAX_CANDIDATES} picks (5 plus backups)")
    resolver = Resolver(client, market=store.get_meta("market"), max_searches=CAL_SEARCHES)
    picker = Picker(exclusions(store, now), CAL_MAX_CANDIDATES, allow_avoid=False)
    known = known_artists(store, now)
    accepted: list[tuple[dict, str, str]] = []
    unresolved: list[dict] = []
    # The first CAL_SIZE picks are always resolved: look them up concurrently (same searches, less wait).
    resolver.prefetch([(str(p.get("title") or "").strip(), str(p.get("artist") or "").strip())
                       for p in picks[:CAL_SIZE] if isinstance(p, Mapping)
                       and artist_key(str(p.get("artist") or "")) not in picker.ex.avoid_artists
                       and not (str(p.get("reason") or "").strip() and is_meta_reason(str(p.get("reason"))))])
    for raw in picks:
        if not isinstance(raw, Mapping):
            raise ValueError("each pick must be {title, artist, kind, reason}")
        title, artist = str(raw.get("title") or "").strip(), str(raw.get("artist") or "").strip()
        kind = str(raw.get("kind") or "").strip().lower()
        if kind not in CAL_KINDS:
            raise ValueError(f"kind must be one of {', '.join(CAL_KINDS)}")
        if artist_key(artist) in picker.ex.avoid_artists:
            picker.reject(title, artist, "avoid_artist")
            continue
        if str(raw.get("reason") or "").strip() and is_meta_reason(str(raw.get("reason"))):
            picker.reject(title, artist, "meta_reason")  # the model's drafting notes, never shown to the user
            continue
        track, why = resolver.resolve(title, artist)
        if track is None:
            unresolved.append({"title": title, "artist": artist, "kind": kind, "why": why})
            continue
        problem = picker.check(track)
        if problem:
            picker.reject(title, artist, problem)
            continue
        reason = clip(str(raw.get("reason") or "").strip(), CAL_REASON_MAX)
        picker.add(track, reason, "agent")
        accepted.append((track, kind, reason))
    chosen = _compose(accepted)
    if not chosen:
        return {"ok": False, "unresolved": unresolved, "rejected": picker.rejected,
                "error": "No pick survived resolution and the filters; propose others."}
    tracks = [{"uri": t["uri"], "title": t.get("name", ""), "artists": [a["name"] for a in t.get("artists") or []
                                                                       if isinstance(a, Mapping) and a.get("name")],
               "album": str((t.get("album") or {}).get("name") or ""), "kind": kind, "reason": reason,
               "known_artist": any(artist_key(a.get("name", "")) in known for a in t.get("artists") or [])}
              for t, kind, reason in chosen]
    _save(store, key, {"created": now, "tracks": tracks, "reactions": {}})
    counts = {k: sum(1 for t in tracks if t["kind"] == k) for k in CAL_KINDS}
    out: dict[str, Any] = {"ok": True, "calibration": calibration_view(store, key), "composition": counts,
                           "unresolved": unresolved, "rejected": picker.rejected,
                           "searches": resolver.searches}
    warnings = []
    if len(tracks) < CAL_SIZE:
        warnings.append(f"only {len(tracks)} of {CAL_SIZE} picks survived; fine to go on, or propose more")
    if not counts["stretch"]:
        warnings.append("no stretch pick survived")
    if not counts["familiar"]:
        warnings.append("no familiar pick survived")
    if warnings:
        out["warnings"] = warnings
    return out


def _find(cal: Mapping, ref: Any) -> Optional[dict]:
    tracks = cal.get("tracks") or []
    if isinstance(ref, bool):
        return None
    if isinstance(ref, int) or (isinstance(ref, str) and ref.strip().isdigit()):
        i = int(ref)
        return tracks[i - 1] if 1 <= i <= len(tracks) else None
    return next((t for t in tracks if t["uri"] == ref), None)


def react(store: Store, ref: Any, reaction: str, *, source: str, client: Optional[SpotifyClient] = None,
          settings: Optional[Mapping] = None, now: Optional[int] = None, key: str = CALIBRATION_KEY) -> dict:
    """React to calibration track *ref* (1-based number or URI) through the normal reactions path.

    Record only: the round is not playing from a Loshify playlist, so nothing is skipped or removed.
    """
    now = _now(now)
    reaction = rx.normalize_reaction(reaction)
    cal = _calibration(store, key)
    track = _find(cal, ref)
    if track is None:
        raise ValueError("no such calibration track (use its number 1-5 or its URI)")
    previous = cal["reactions"].get(track["uri"]) or {}
    rid = previous.get("id")
    if rid and store.get_reaction(rid) and store.amend_reaction(rid, reaction=reaction):
        out = {"ok": True, "id": rid, "reaction": reaction, "changed": previous.get("reaction") != reaction,
               "track": {"title": track["title"], "artists": track["artists"], "uri": track["uri"]}}
    else:
        snap = {"track_uri": track["uri"], "title": track["title"], "artists": track["artists"],
                "album": track.get("album", ""), "playlist_uri": None}
        # Calibration only *records* taste: the track is not playing from a Loshify playlist, so a 👎
        # must never skip playback or remove anything (that is what the act path would do).
        out = rx.record(store, snap, reaction, source=source, client=None, settings=settings,
                        act=False, now=now)
        out["actions"] = {"recorded_only": True}
    cal["reactions"][track["uri"]] = {"id": out["id"], "reaction": reaction, "ts": now}
    _save(store, key, cal)
    log_reaction(store, track, reaction, "calibration" if key == CALIBRATION_KEY else "tune_in", now)
    view = calibration_view(store, key) or {}
    if view.get("complete") and key == CALIBRATION_KEY:
        mark(store, "calibration", DONE, now)
    out.update(reacted=view.get("reacted", 0), total=view.get("total", 0), complete=bool(view.get("complete")))
    return out


def finish_calibration(store: Store, now: Optional[int] = None) -> str:
    """User moved on: done if they reacted to anything, else skipped."""
    view = calibration_view(store)
    outcome = DONE if view and view["reacted"] else SKIPPED
    mark(store, "calibration", outcome, now)
    return outcome


CAL_LOG_KEY = "calibration_log"
CAL_LOG_MAX = 200
PORTRAIT_FEEDBACK_KEY = "portrait_feedback"


def log_reaction(store: Store, track: Mapping, reaction: str, source: str, now: Optional[int] = None) -> None:
    """Append ``{artist, reaction, source, ts}`` to the correction log the next portrait reads.

    A changed reaction to the same track replaces its earlier entry (the newest word counts).
    """
    artists = track.get("artists") or []
    artist = str(artists[0] if artists else "").strip()
    if not artist:
        return
    log = [r for r in _load(store, CAL_LOG_KEY, []) if isinstance(r, dict) and r.get("uri") != track.get("uri")]
    log.append({"uri": track.get("uri"), "artist": artist, "reaction": reaction, "source": source, "ts": _now(now)})
    _save(store, CAL_LOG_KEY, log[-CAL_LOG_MAX:])


def portrait_verdict(store: Store, verdict: str, now: Optional[int] = None) -> dict:
    """The portrait card's "Sounds like me" / "Not quite". Stored for the next portrait; marks the step."""
    verdict = str(verdict or "").strip().lower().replace(" ", "_")
    if verdict not in ("sounds_like_me", "not_quite"):
        raise ValueError("verdict must be sounds_like_me or not_quite")
    _save(store, PORTRAIT_FEEDBACK_KEY, {"verdict": verdict, "ts": _now(now)})
    mark(store, "portrait", DONE, now)
    return {"ok": True, "verdict": verdict}


# ── note ───────────────────────────────────────────────────────────────────────────────────────

def save_note(store: Store, text: Any, now: Optional[int] = None) -> dict:
    note = taste.set_user_note(store, text, now)
    mark(store, "note", DONE if note else SKIPPED, now)
    return {"ok": True, "note": note, "chars": len(note), "max": taste.USER_NOTE_MAX}


def first_edition_built(store: Store, now: Optional[int] = None) -> bool:
    """The first playlist exists: the flow is over; any step still open counts as skipped."""
    state = _state(store)
    if not state.get("started") or state.get("finished"):
        return False
    for step in STEPS[:-1]:
        if (state.get("steps") or {}).get(step) not in (DONE, SKIPPED):
            if step == "calibration":
                finish_calibration(store, now)
            else:
                mark(store, step, SKIPPED, now)
    return mark(store, "first_edition", DONE, now)


def taste_context(store: Store) -> dict:
    """What curation must honour beyond the scores: the anchors and the user's note (framed)."""
    out: dict[str, Any] = {}
    names = [anchor_label(a) for a in anchors(store)]
    if names:
        out["anchors"] = names
    block = taste.note_block(store)
    if block:
        out["user_note"] = block
    return out


def skip(store: Store, step: str, now: Optional[int] = None) -> dict:
    if step == "calibration":
        outcome = finish_calibration(store, now)
    else:
        mark(store, step, SKIPPED, now)
        outcome = SKIPPED
    return {"ok": True, "step": step, "outcome": outcome, **{k: v for k, v in status(store, now).items()
                                                               if k in ("next_step", "ask", "finished")}}


__all__ = ["ASK", "CALIBRATION_KEY", "CAL_SIZE", "log_reaction", "portrait_verdict", "MAX_ANCHORS", "STEPS", "active", "anchor_label", "anchors", "calibration_view",
           "finish_calibration", "first_edition_built", "mark", "taste_context", "parse_anchor", "propose", "react", "resolve_anchor", "restart",
           "save_note", "set_anchors", "skip", "start", "status"]
