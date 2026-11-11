"""Readiness probe and onboarding seed.

``probe`` answers "can Loshify talk to Spotify yet, and if not, what is the user's next step?" with
one ``GET /me``. It never reads the environment or a credential store: the client-ID and sign-in
states come back from Hermes as typed errors (``core/spotify.map_exception``).

``onboard`` seeds taste from listening history (top artists/tracks for 3 ranges, saved tracks,
recently played) within a fixed call budget, and returns a *portrait payload*: facts the agent turns
into 3–4 sentences about the listener. Endpoints that a new Development-Mode app cannot use
(possible for ``/me/top/*``) degrade to empty lists instead of failing onboarding.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from typing import Any, Optional

from . import portrait, taste
from .spotify import (SETUP_APP, SETUP_CLIENT_ID, SETUP_INTERNAL, SETUP_PREMIUM_OR_USERS, SETUP_SIGNIN, SpotifyClient,
                      SpotifyError)
from .store import DAY, Store

# Register this example loopback URI with Spotify. Hermes chooses a free port at sign-in;
# Spotify accepts a loopback redirect whose port differs.
REDIRECT_PORT = 43828
REDIRECT_URI = f"http://127.0.0.1:{REDIRECT_PORT}/callback"
APP_NAME = "Loshify"
API_NAME = "Web API"
DASHBOARD_URL = "https://developer.spotify.com/dashboard"
SIGNIN_COMMAND = "hermes auth add loshify:spotify"
CLIENT_ID_ENV = "LOSHIFY_SPOTIFY_CLIENT_ID"  # = plugin.yaml oauth[0].client_id_env (tests/test_manifest.py)
SAVED_MAX = 500           # 10 pages of 50
CALL_BUDGET = 20
RANGES = ("short_term", "medium_term", "long_term")
FORGOTTEN_AGE_DAYS = 365

# The setup copy below is shared by the Desktop wizard (via /setup), the loshify_setup tool and the README.
# Plain words on purpose: a stranger must get through it without asking anyone.
WHY_LINE = "This is your own key to Spotify; 127.0.0.1 means 'this computer', nothing leaves it."
# Plugin OAuth sign-in runs only from the user's own terminal (Hermes refuses it from agents and plugins), so
# step 5 says plainly where that is.
CONNECT_WHERE = ("Where to paste it: on Windows press Start, type PowerShell and press Enter; on a Mac open "
                 "Terminal. Paste with Ctrl+V (Mac: Cmd+V), then press Enter.")
USERS_WHY = "New Spotify apps only let in the accounts listed there."

# What can go wrong, in the user's words → the fix. Keys are stable ids the wizard and tests use.
FIXES = {
    "redirect": (
        f"Spotify says the redirect URI is wrong or \"not secure\". In your app, open Settings → Edit and make "
        f"the Redirect URI exactly {REDIRECT_URI} (with :{REDIRECT_PORT}, no slash at the end, not "
        "\"localhost\"). Save, then connect again."),
    "refused": (
        "Spotify refused this app. Check that the Client ID you pasted is the one on your app's Settings page, "
        f"and that the Redirect URI is exactly {REDIRECT_URI}. Then connect again."),
    "not_listed": (
        "Spotify said no to this account. In your app, open Settings → User Management and add your name and "
        "the email of your Spotify account, then Save and check again. The app's owner also needs Spotify "
        "Premium."),
    "port_busy": (
        "The sign-in port on this computer is busy. Close any other sign-in that is still waiting (another "
        "terminal or browser tab), wait a minute, then run the command again."),
    "no_premium": (
        "Connected, but this Spotify account has no Premium: playlists and your taste work, play, pause and "
        "skip don't."),
    "no_answer": "Spotify didn't answer. Check your internet connection, then check again in a minute.",
    "hermes": "Hermes blocked the request. Update Hermes, then reinstall Loshify.",
}

# Probe outcome → the fix to show. ``client_id`` / ``signin`` are steps still to do, not problems.
PROBLEM_FOR_STEP = {
    SETUP_APP: "refused",
    SETUP_PREMIUM_OR_USERS: "not_listed",
    SETUP_INTERNAL: "hermes",
    "retry": "no_answer",
}

# The same steps the Desktop wizard shows, for the agent to walk through in chat (one at a time).
GUIDE = (
    ("dashboard", f"Open the Spotify Dashboard at {DASHBOARD_URL}, log in, and press Create app."),
    ("app", f"Fill in the form: App name {APP_NAME}. Redirect URI {REDIRECT_URI} (paste it, then press Add). "
            f"Tick only {API_NAME}. Accept the terms and Save. Why: {WHY_LINE}"),
    ("users", "In the new app open Settings → User Management, add your name and the email of your Spotify "
              f"account, and Save. Why: {USERS_WHY}"),
    ("client_id", "Copy the Client ID (Settings → Basic Information; not the Client secret) and paste it into "
                  "the Loshify card in Hermes Desktop, or Settings → Plugins → Loshify. Not into a chat."),
    ("connect", f"Run `{SIGNIN_COMMAND}` once in a terminal. {CONNECT_WHERE} Your browser opens: log in and "
                "press Agree. An agent can't run this for you."),
    ("check", "Loshify checks the connection by itself (one real call to Spotify); ask again and it confirms."),
)
GUIDE_START = {SETUP_CLIENT_ID: "dashboard", SETUP_SIGNIN: "connect"}

CLIENT_ID_ERRORS = {
    "empty": "Paste the Client ID first.",
    "shape": ("That doesn't look like a Client ID: it is 32 letters and digits, on your app's Settings page "
              "under Basic Information. Not the Client secret."),
}
_CLIENT_ID_RE = re.compile(r"^[0-9A-Fa-f]{32}$")

NEXT_STEP = {
    SETUP_CLIENT_ID: (
        f"Set up your own Spotify app (about two minutes): open {DASHBOARD_URL} and press Create app; App name "
        f"{APP_NAME}, Redirect URI {REDIRECT_URI}, tick only {API_NAME}, Save; then Settings → User Management: "
        "add your name and Spotify email. Paste the app's Client ID into the Loshify card in Hermes Desktop, or "
        "Settings → Plugins → Loshify. It is not a password, but don't paste it into a chat."),
    SETUP_SIGNIN: (
        f"Connect Spotify: run `{SIGNIN_COMMAND}` once in a terminal. {CONNECT_WHERE} Your browser opens: log "
        "in and press Agree. An agent can't run this for you."),
    SETUP_APP: FIXES["refused"],
    SETUP_PREMIUM_OR_USERS: FIXES["not_listed"],
    SETUP_INTERNAL: FIXES["hermes"],
    "retry": FIXES["no_answer"],
}


def check_client_id(raw: Any) -> tuple[Optional[str], Optional[str]]:
    """``(client_id, None)`` when *raw* looks like a Spotify Client ID, else ``(None, plain-language error)``."""
    text = raw.strip() if isinstance(raw, str) else ""
    if not text:
        return None, CLIENT_ID_ERRORS["empty"]
    if not _CLIENT_ID_RE.match(text):
        return None, CLIENT_ID_ERRORS["shape"]
    return text, None


def guide(step: Optional[str] = None) -> dict:
    """The setup steps for chat, plus where this user starts (``start_at``)."""
    return {"steps": [{"step": sid, "do": text} for sid, text in GUIDE],
            "start_at": GUIDE_START.get(step or "", "check"), "fixes": dict(FIXES)}


def _now(now: Optional[int]) -> int:
    return int(now if now is not None else time.time())


def probe(client: SpotifyClient, store: Optional[Store] = None) -> dict:
    """One real ``GET /me``: ``{ready, step, next_step?, problem?, fix?, status?, reason?, note?}``.

    *step* is the user's next step (``client_id``, ``signin``) or ``done``; a failure that needs the user
    to fix something also names it as ``problem`` (a ``FIXES`` key) with ``fix`` in plain words.
    """
    try:
        me = client.me()
    except SpotifyError as exc:
        step = exc.setup or "retry"
        out: dict[str, Any] = {"ready": False, "step": step, "next_step": NEXT_STEP.get(step, NEXT_STEP["retry"])}
        problem = PROBLEM_FOR_STEP.get(step) if step in NEXT_STEP else "no_answer"
        if problem:
            out.update(problem=problem, fix=FIXES[problem])
        if exc.status is not None:
            out["status"] = exc.status
        if exc.reason:
            out["reason"] = exc.reason
        return out
    me = me or {}
    if store is not None:
        if me.get("id"):
            store.set_meta("user_id", me["id"])
        if me.get("country"):
            store.set_meta("market", me["country"])
    out = {"ready": True, "step": "done", "status": 200, "has_user": bool(me.get("id"))}
    if me.get("product") and me.get("product") != "premium":
        out.update(problem="no_premium", note=FIXES["no_premium"])
    return out


def _soft(fn, default, skipped: list, label: str):
    try:
        return fn()
    except SpotifyError as exc:
        if exc.setup and exc.setup != SETUP_PREMIUM_OR_USERS:
            raise
        skipped.append({"source": label, **({"status": exc.status} if exc.status else {})})
        return default


def portrait_payload(top_artists: dict, top_tracks: dict, saved: list, recent: list,
                     now: Optional[int] = None, corrections: Optional[dict] = None) -> dict:
    """Facts for the agent's portrait: :func:`core.portrait.portrait_facts` (weights, bursts, confidence)."""
    return portrait.portrait_facts(top_artists, top_tracks, saved, recent, _now(now), corrections)


def onboard(client: SpotifyClient, store: Store, now: Optional[int] = None) -> dict:
    """Seed taste from Spotify history and return the portrait payload. Bounded to ``CALL_BUDGET`` calls."""
    now = _now(now)
    start = client.calls
    skipped: list = []
    top_artists = {rng: _soft(lambda r=rng: client.top("artists", r), [], skipped, f"top_artists:{rng}")
                   for rng in RANGES}
    top_tracks = {rng: _soft(lambda r=rng: client.top("tracks", r), [], skipped, f"top_tracks:{rng}")
                  for rng in RANGES}
    remaining = max(1, CALL_BUDGET - (client.calls - start) - 1)
    saved = _soft(lambda: client.saved_tracks(max_items=min(SAVED_MAX, remaining * 50)), [], skipped, "saved")
    recent = _soft(lambda: client.recently_played(), [], skipped, "recently_played")
    weights = taste.seed_weights(top_artists, top_tracks, saved, recent)
    corrections = portrait.corrections_from_store(store)
    taste.apply_seed(store, weights, now)
    taste.digest(store, now)
    return {
        "seeded_artists": len(weights),
        "portrait_facts": portrait_payload(top_artists, top_tracks, saved, recent, now, corrections),
        "own_top_tracks": [t for rng in ("short_term", "medium_term") for t in (top_tracks.get(rng) or [])][:20],
        "skipped_sources": skipped,
        "spotify_calls": client.calls - start,
    }


__all__ = ["CALL_BUDGET", "CLIENT_ID_ERRORS", "FIXES", "GUIDE", "NEXT_STEP", "REDIRECT_PORT", "REDIRECT_URI",
           "SIGNIN_COMMAND", "WHY_LINE", "check_client_id", "guide", "onboard", "portrait_payload", "probe"]
