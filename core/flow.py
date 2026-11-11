"""The click-through "Get to know me" flow (Desktop), step by step, with per-step timing.

    meet()      Connect done → learn the taste, write the portrait, prepare the calibration round
    verdict()   "Sounds like me" / "Not quite" (+ optional anchors → the portrait is rewritten)
    react       the calibration buttons (onboarding.react, record only)
    build()     preset or "your own" words + the optional note → rotation → first edition → links
    tune_in_*   the optional recurring round (core.tunein)

Words come from *llm* (``core.writer``); without one every step still completes from the user's
own data. The chat path (the agent + the same tools) stays available and uses the same core.
Every result carries ``timing`` (ms per sub-step) so latency is measured, not guessed.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Mapping, Optional

from . import onboarding as ob
from . import portrait as pt
from . import rotations as rot
from . import setup as su
from . import taste
from . import tunein
from . import writer
from .curate import known_artist_names
from .spotify import SpotifyClient
from .store import Store

BUILD_SIZE = 15
_TUNE_IN_RETRY = "Tune in needs the curator's picks; try again in a moment."
Progress = Optional[Callable[[str], None]]  # one short line for the card while a step runs (core.jobs)

# What the card says while each step runs. Plain words; the card shows them as they come.
SAY = {
    "learn": "Reading your listening history…",
    "portrait": "Writing your portrait…",
    "picks": "Choosing five quick picks for you…",
    "rewrite": "Rewriting your portrait with what you told me…",
    "arrangement": "Setting up your arrangement…",
    "candidates": "Your curator is choosing the songs…",
    "slot": "Writing your playlists… {i}/{n}",
    "top_up": "Topping up the short ones…",
}


def _say(progress: Progress, key: str, **kw: Any) -> None:
    if progress is not None:
        try:
            progress(SAY[key].format(**kw))
        except Exception:
            pass


class _Clock:
    def __init__(self) -> None:
        self.timing: dict[str, int] = {}
        self._t0 = time.monotonic()

    def lap(self, name: str, since: float) -> None:
        self.timing[name] = int((time.monotonic() - since) * 1000)

    def done(self) -> dict:
        self.timing["total"] = int((time.monotonic() - self._t0) * 1000)
        return self.timing


def _portrait(store: Store, facts: Mapping, llm: Optional[writer.Llm], clock: _Clock) -> dict:
    t = time.monotonic()
    out = writer.write_portrait(facts, llm)
    store.set_portrait(out["text"])
    clock.lap("portrait", t)
    return out


def _round(client: SpotifyClient, store: Store, facts: Mapping, llm: Optional[writer.Llm], clock: _Clock, *,
           targets: Optional[Mapping] = None, top_tracks: Optional[list] = None, key: str = ob.CALIBRATION_KEY,
           now: Optional[int] = None, prefix: str = "calibration") -> tuple[dict, dict]:
    """Picks → resolved round, with at most ONE automatic retry when fewer than 5 valid picks survive.

    Valid = clean (no drafting notes) *and* resolved on Spotify past the filters, so album titles and
    abandoned picks count as missing. The retry happens in the writer (too few clean picks) or here (too
    few resolved), never both. Returns ``(picks, proposed)``.
    """
    t = time.monotonic()
    picks = writer.calibration_picks(facts, llm, top_tracks=top_tracks, targets=targets)
    clock.lap(f"{prefix}_picks", t)
    if not picks["picks"]:
        return picks, {"ok": False}
    t = time.monotonic()
    res = ob.propose(client, store, picks["picks"], now, key=key)
    clock.lap(f"{prefix}_resolve", t)
    survived = len(((res.get("calibration") or {}).get("tracks")) or []) if res.get("ok") else 0
    if picks["by"] == "llm" and picks.get("attempts", 1) < 2 and survived < ob.CAL_SIZE:
        t = time.monotonic()
        kept = [{"title": tr["title"], "artist": (tr["artists"] or [""])[0], "kind": tr["kind"], "reason": tr["reason"]}
                for tr in ((res.get("calibration") or {}).get("tracks") or [])] if res.get("ok") else []
        more = writer.more_picks(facts, llm, targets=targets, tried=picks["picks"])
        clock.lap(f"{prefix}_retry", t)
        if more["picks"]:
            t = time.monotonic()
            again = ob.propose(client, store, (kept + more["picks"])[:ob.CAL_MAX_CANDIDATES], now, key=key)
            clock.lap(f"{prefix}_retry_resolve", t)
            if again.get("ok") and len(again["calibration"]["tracks"]) >= survived:
                res = again
            elif res.get("ok"):  # the retry did worse: put the first round back
                res = ob.propose(client, store, kept, now, key=key)
        picks = {**picks, "attempts": 2}
    return picks, res


def meet(client: SpotifyClient, store: Store, llm: Optional[writer.Llm] = None, now: Optional[int] = None,
         progress: Progress = None) -> dict:
    """After Connect: seed taste, write the portrait, prepare 5 calibration picks. One call, no chat."""
    clock = _Clock()
    ob.start(store, now)
    _say(progress, "learn")
    t = time.monotonic()
    learned = su.onboard(client, store, now)
    clock.lap("learn_taste", t)
    facts = learned["portrait_facts"]
    _say(progress, "portrait")
    portrait = _portrait(store, facts, llm, clock)
    _say(progress, "picks")
    picks, cal = _round(client, store, facts, llm, clock, top_tracks=learned.get("own_top_tracks"), now=now)
    if not cal.get("ok") and picks["by"] == "llm":  # nothing the model sent survived: the user's own hits
        fallback = writer.familiar_fallback(facts, learned.get("own_top_tracks") or [])
        if fallback:
            t = time.monotonic()
            cal, picks = ob.propose(client, store, fallback, now), {**picks, "by": "fallback"}
            clock.lap("calibration_fallback", t)
    return {"ok": True, "portrait": portrait["text"], "portrait_by": portrait["by"],
            "evidence": facts.get("evidence"), "claims": facts.get("claims"),
            "calibration": ob.calibration_view(store), "calibration_by": picks["by"],
            "calibration_ok": bool(cal.get("ok")), "spotify_calls": learned.get("spotify_calls"),
            "timing": clock.done()}


def verdict(client: SpotifyClient, store: Store, verdict_: str, anchors: Any = None,
            llm: Optional[writer.Llm] = None, now: Optional[int] = None, progress: Progress = None) -> dict:
    """The portrait buttons. "Not quite" may carry 1-3 anchors (optional); then the portrait is rewritten."""
    clock = _Clock()
    out: dict[str, Any] = ob.portrait_verdict(store, verdict_, now)
    items = [a for a in (anchors or []) if str(a or "").strip()] if isinstance(anchors, list) else []
    if items:
        t = time.monotonic()
        res = ob.set_anchors(client, store, items[:ob.MAX_ANCHORS], now)
        clock.lap("anchors", t)
        out.update(anchors=res.get("anchors"), unresolved=res.get("unresolved"))
    else:
        ob.mark(store, "anchors", ob.SKIPPED, now)
    if out["verdict"] == "not_quite":
        # Re-learn with the corrections (the verdict + anchors just stored). Re-seeding keeps reactions.
        _say(progress, "rewrite")
        t = time.monotonic()
        facts = su.onboard(client, store, now)["portrait_facts"]
        clock.lap("refacts", t)
        rewritten = _portrait(store, facts, llm, clock)
        out.update(portrait=rewritten["text"], portrait_by=rewritten["by"])
    out["timing"] = clock.done()
    return out


def build(client: SpotifyClient, store: Store, *, preset: Optional[str] = None, words: Optional[str] = None,
          note: Optional[str] = None, llm: Optional[writer.Llm] = None, now: Optional[int] = None,
          size: int = BUILD_SIZE, progress: Progress = None) -> dict:
    """Rotation (a preset card or the user's own words) + note → first edition → playlist links."""
    clock = _Clock()
    _say(progress, "arrangement")
    if ob.calibration_view(store) is not None:
        ob.finish_calibration(store, now)
    if note is not None:
        ob.save_note(store, note, now)
    else:
        ob.mark(store, "note", ob.SKIPPED, now)
    t = time.monotonic()
    if preset:
        spec = rot.from_preset(preset)
    elif words and str(words).strip():
        drafted = writer.spec_from_words(str(words), rot.presets_view(), llm)
        if not drafted["spec"]:
            return {"ok": False, "error": "Could not turn that into an arrangement right now. Pick a card, or try "
                                          "other words.", "timing": clock.done()}
        spec = rot.validate_spec(drafted["spec"])
    else:
        raise ValueError("pick a preset or describe your own arrangement")
    rot.set_spec(store, spec, now)
    ob.mark(store, "rotation", ob.DONE, now)
    clock.lap("rotation_spec", t)
    _say(progress, "candidates")
    t = time.monotonic()
    context = {"lead": [s.display for s in taste.scores(store, now)[:10]],
               "known_artists": known_artist_names(store, now),
               "avoid": [s.display for s in taste.scores(store, now) if s.avoid][:30],
               "anchors": ob.taste_context(store).get("anchors"), "user_note": taste.note_block(store)}
    cands = writer.slot_candidates(spec, context, llm, per_slot=size + 9)
    clock.lap("candidates", t)
    t = time.monotonic()
    out = rot.refresh(client, store, candidates=cands["candidates"], size=size, now=now, top_up=False,
                      on_slot=lambda i, n, _name: _say(progress, "slot", i=i, n=n))
    clock.lap("first_edition", t)
    short = [s["slot"] for s in out.get("slots", []) if s.get("ok") and s.get("short")]
    if short and llm is not None:  # one automatic top-up with fresh candidates for the short slots
        _say(progress, "top_up")
        t = time.monotonic()
        more = writer.slot_candidates({"slots": [s for s in spec["slots"] if s["name"] in short]},
                                      {**context, "known_artists": context["known_artists"]}, llm, per_slot=size)
        if more["candidates"]:
            topped = rot.refresh(client, store, candidates=more["candidates"], size=size, slots=short, now=now,
                                 top_up=True)
            by_slot = {s["slot"]: s for s in topped.get("slots", []) if s.get("ok")}
            out["slots"] = [by_slot.get(s["slot"], s) for s in out["slots"]]
        clock.lap("top_up", t)
    if out.get("ok"):
        ob.first_edition_built(store, now)
    # A playlist written into an existing one (a top-up) comes back without its name; it is the one
    # rotations gave it on creation.
    links = [{"slot": s["slot"], "name": (s.get("playlist") or {}).get("name") or f"Loshify · {s['slot']}",
              "url": (s.get("playlist") or {}).get("url"), "tracks": len(s.get("tracks") or [])}
             for s in out.get("slots", []) if s.get("ok")]
    return {"ok": bool(out.get("ok")), "playlists": links, "candidates_by": cands["by"],
            "slots": [{k: s.get(k) for k in ("slot", "ok", "skipped", "short", "need", "filled_from")}
                      for s in out.get("slots", [])],
            "archive": (out.get("archive") or {}).get("line"), "error": out.get("error"), "timing": clock.done()}


# ── tune in ────────────────────────────────────────────────────────────────────────────────────

def tune_in_start(client: SpotifyClient, store: Store, llm: Optional[writer.Llm] = None,
                  now: Optional[int] = None, interval_days: Any = tunein.INTERVAL_DEFAULT, *,
                  demo: bool = False) -> dict:
    """Open a tune-in round when one is available (the button is never shown otherwise).
    *demo*: the ``demo_mode`` setting (available now, no wait)."""
    clock = _Clock()
    state = tunein.status(store, now, interval_days, demo=demo)
    if not state["available"]:
        return {"ok": False, "error": state["line"], "tune_in": state, "timing": clock.done()}
    targets = tunein.targets(store, now)
    facts = {"lead": [{"artist": s.display} for s in taste.scores(store, now)[:8]],
             "corrections": pt.corrections_from_store(store)}
    if llm is None:
        return {"ok": False, "error": _TUNE_IN_RETRY, "tune_in": state, "timing": clock.done()}
    picks, res = _round(client, store, facts, llm, clock, targets=targets, key=tunein.ROUND_KEY, now=now,
                        prefix="tune_in")
    if picks["by"] != "llm" or not res.get("ok"):  # no fallback: a tune-in of the user's own hits teaches nothing
        return {"ok": False, "error": _TUNE_IN_RETRY, "tune_in": state, "attempts": picks.get("attempts", 1),
                "timing": clock.done()}
    return {"ok": True, "round": ob.calibration_view(store, tunein.ROUND_KEY), "targets": targets,
            "attempts": picks.get("attempts", 1), "warnings": res.get("warnings"), "timing": clock.done()}


def tune_in_finish(store: Store, now: Optional[int] = None, interval_days: Any = tunein.INTERVAL_DEFAULT, *,
                   demo: bool = False) -> dict:
    tunein.mark_run(store, now)
    return {"ok": True, "tune_in": tunein.status(store, now, interval_days, demo=demo)}


__all__ = ["SAY", "build", "meet", "tune_in_finish", "tune_in_start", "verdict"]
