"""Curation: candidate resolver, playlist templates and the playlist writer (PLAN §4.1).

The agent recommends; this module grounds and remembers (PLAN D3):

- ``Resolver`` turns an agent candidate ``{title, artist, reason}`` into a real Spotify track through
  ``/search`` only (limit <= 10), with strict fuzzy thresholds so a wrong song is never written.
- Templates pick tracks:
  - ``time_capsule``: deterministic, no LLM. The user's own saved / all-time-top / loved tracks minus
    anything heard lately (short-term top, recently played, served in the last 60 days).
  - ``deep_dig``: agent candidates; >= 80% by artists the user does not already know.
  - ``mood``: agent candidates mixing own and new music (below the PLAN cut line; same pipeline,
    no familiarity quota).
- Every template drops blocked tracks (👎, also re-releases of the same song), avoid-artists and
  anything served in the last 60 days, and keeps <= 2 tracks per artist.
- ``write_playlist`` creates or replaces a playlist this plugin owns and logs every served track.
  It never touches a playlist Loshify did not create.

Pure logic is separated from I/O so the rules are testable on plain dicts.
"""

from __future__ import annotations

import threading
import time
from collections import Counter, defaultdict
import contextvars
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping, Optional

from .spotify import SETUP_PREMIUM_OR_USERS, SpotifyClient, SpotifyError
from .store import DAY, Store
from .taste import scores
from .text import artist_key, clip, is_meta_reason, norm, similarity, title_key

TEMPLATES = ("time_capsule", "deep_dig", "mood")
TEMPLATE_LABEL = {"time_capsule": "Time Capsule", "deep_dig": "Deep Dig", "mood": "Mood Room"}
NEEDS_CANDIDATES = frozenset({"deep_dig", "mood"})

ARTIST_MIN = 0.85
TITLE_MIN = 0.80
STRICT_LIMIT = 5
FREE_LIMIT = 10

MAX_CANDIDATES = 40
MAX_SEARCHES = 45      # ≈ 0.3 s each → well under 20 s
PARALLEL_SEARCHES = 4  # concurrent /search calls while resolving (latency only: the budget is shared)
LOOKAHEAD = 0          # extra candidates resolved per batch beyond what is still needed (0: same search count as sequential)
PER_ARTIST = 2
AVOID_PER_ARTIST = 1   # an avoid-artist may appear once, only from the user's own library
FAMILIAR_MAX_SHARE = 0.20  # deep_dig: at most 20% by artists the user already knows
SERVED_RECENT_DAYS = 60
SIZE_MIN, SIZE_MAX, SIZE_DEFAULT = 5, 50, 25
REASON_MAX = 140

PLAYLIST_URL = "https://open.spotify.com/playlist/"


def _now(now: Optional[int]) -> int:
    return int(now if now is not None else time.time())


def clamp_size(size: Any, default: int = SIZE_DEFAULT) -> int:
    try:
        value = int(size)
    except (TypeError, ValueError):
        value = default
    return max(SIZE_MIN, min(SIZE_MAX, value))


# ── track helpers ──────────────────────────────────────────────────────────────────────────────

def artists_of(track: Mapping) -> list[str]:
    return [str(a.get("name")) for a in (track.get("artists") or []) if isinstance(a, Mapping) and a.get("name")]


def primary_artist(track: Mapping) -> str:
    names = artists_of(track)
    return names[0] if names else ""


def song_key(title: str, artist: str) -> tuple[str, str]:
    """Identity of a *song* across releases (single, album, remaster): (title key, artist key)."""
    return title_key(title), artist_key(artist)


def _is_track(track: Any) -> bool:
    return isinstance(track, Mapping) and str(track.get("uri", "")).startswith("spotify:track:")


# ── resolver ───────────────────────────────────────────────────────────────────────────────────

@dataclass
class Match:
    track: dict
    title_score: float
    artist_score: float


def best_match(title: str, artist: str, results: Iterable[Mapping]) -> Optional[Match]:
    """The best search result for (title, artist), or ``None`` below the thresholds.

    Artist similarity is the best over all credited artists (``artist_key``: casefolded, accents and
    niqqud dropped, leading "The" ignored). Titles compare without version decorations
    (feat./remaster/live/edit). Ties keep Spotify's relevance order; a result whose own title carries
    no decoration wins a tie, so "Song" beats "Song - Live" when both are offered.
    """
    want_title, want_artist = title_key(title), artist_key(artist)
    if not want_title or not want_artist:
        return None
    best_key: Optional[tuple[float, int]] = None
    best: Optional[Match] = None
    for track in results:
        if not _is_track(track):
            continue
        a_score = max((similarity(want_artist, artist_key(n)) for n in artists_of(track)), default=0.0)
        if a_score < ARTIST_MIN:
            continue
        t_score = similarity(want_title, title_key(track.get("name", "")))
        if t_score < TITLE_MIN:
            continue
        plain = 1 if norm(track.get("name", "")) == title_key(track.get("name", "")) else 0
        key = (round(a_score + t_score, 6), plain)
        if best_key is None or key > best_key:  # strict: earlier (more relevant) results win ties
            best_key, best = key, Match(dict(track), t_score, a_score)
    return best


def is_album_title(title: str, artist: str, results: Iterable[Mapping]) -> bool:
    """True when *title* names an album of *artist* in these search results, not a song on it.

    Only asked after no track matched, so a title track ("Rumours" on *Rumours*) still resolves; this
    only explains the miss (``why="album_title"``) instead of a generic ``not_found``.
    """
    want_title, want_artist = title_key(title), artist_key(artist)
    if not want_title or not want_artist:
        return False
    for track in results:
        if not _is_track(track):
            continue
        album = track.get("album") if isinstance(track.get("album"), Mapping) else {}
        names = [str(a.get("name")) for a in (album.get("artists") or []) if isinstance(a, Mapping)] or artists_of(track)
        if max((similarity(want_artist, artist_key(n)) for n in names), default=0.0) < ARTIST_MIN:
            continue
        if similarity(want_title, title_key(str(album.get("name") or ""))) >= TITLE_MIN:
            return True
    return False


def _quote(text: str) -> str:
    return str(text or "").replace('"', " ").strip()


class Resolver:
    """Resolves candidates via ``/search`` with a per-call cache and a hard search budget."""

    def __init__(self, client: SpotifyClient, *, market: Optional[str] = None, max_searches: int = MAX_SEARCHES,
                 workers: int = PARALLEL_SEARCHES):
        self.client = client
        self.market = market or None
        self.max_searches = max_searches
        self.workers = max(1, int(workers))
        self.searches = 0
        self.rate_limited = False
        self._cache: dict[tuple[str, str], tuple[Optional[dict], str]] = {}
        self._lock = threading.Lock()

    @property
    def exhausted(self) -> bool:
        return self.rate_limited or self.searches >= self.max_searches

    def _search(self, query: str, limit: int) -> Optional[list]:
        with self._lock:  # budget check + count are atomic, so a parallel prefetch never overspends
            if self.exhausted:
                return None
            self.searches += 1
        try:
            return self.client.search_tracks(query, limit=limit, market=self.market)
        except SpotifyError as exc:
            if exc.setup:  # sign-in / app problems: the caller must surface them
                raise
            if exc.status == 429:
                self.rate_limited = True
            return None

    def prefetch(self, pairs: Iterable[tuple[str, str]]) -> None:
        """Resolve ``(title, artist)`` pairs concurrently into the cache (same budget, same answers).

        A latency optimisation only: :meth:`resolve` afterwards is a cache hit. Sign-in/app errors raise.
        """
        todo: dict[tuple[str, str], tuple[str, str]] = {}
        for title, artist in pairs:
            key = song_key(title, artist)
            if key[0] and key[1] and key not in self._cache and key not in todo:
                todo[key] = (title, artist)
        if len(todo) < 2 or self.workers < 2:
            return  # nothing to gain; resolve() does it in order
        with ThreadPoolExecutor(max_workers=min(self.workers, len(todo))) as pool:
            futures = [pool.submit(contextvars.copy_context().run, self.resolve, *pair) for pair in todo.values()]
            for future in futures:
                future.result()

    def search(self, query: str, limit: int = FREE_LIMIT) -> Optional[list]:
        """One budgeted ``/search`` (``None`` once the budget is spent or Spotify rate-limited)."""
        return self._search(query, limit)

    def resolve(self, title: str, artist: str) -> tuple[Optional[dict], str]:
        """``(track, "")`` or ``(None, why)``; why ∈ not_found | album_title | budget | rate_limited | bad_candidate."""
        key = song_key(title, artist)
        if not key[0] or not key[1]:
            return None, "bad_candidate"
        if key in self._cache:
            return self._cache[key]
        strict = f'track:"{_quote(title)}" artist:"{_quote(artist)}"'
        free = f"{_quote(title)} {_quote(artist)}"
        outcome: tuple[Optional[dict], str] = (None, "not_found")
        seen: list = []
        for query, limit in ((strict, STRICT_LIMIT), (free, FREE_LIMIT)):
            results = self._search(query, limit)
            if results is None:
                outcome = (None, "rate_limited" if self.rate_limited else "budget")
                break
            match = best_match(title, artist, results)
            if match:
                outcome = (match.track, "")
                break
            seen += results
        if outcome[1] == "not_found" and is_album_title(title, artist, seen):
            outcome = (None, "album_title")
        if outcome[1] not in ("budget", "rate_limited"):
            self._cache[key] = outcome
        return outcome


# ── selection rules (pure) ─────────────────────────────────────────────────────────────────────

@dataclass
class Exclusions:
    """What a template must not serve."""

    blocked_uris: set = field(default_factory=set)
    blocked_songs: set = field(default_factory=set)   # song_key of 👎 tracks (re-releases too)
    avoid_artists: set = field(default_factory=set)   # artist keys
    served_uris: set = field(default_factory=set)     # served in the last SERVED_RECENT_DAYS

    def why_not(self, track: Mapping) -> str:
        uri = track.get("uri")
        if uri in self.blocked_uris or song_key(track.get("name", ""), primary_artist(track)) in self.blocked_songs:
            return "blocked"
        if uri in self.served_uris:
            return "served_recently"
        return ""


def exclusions(store: Store, now: Optional[int] = None) -> Exclusions:
    now = _now(now)
    blocked = store.blocked_rows()
    return Exclusions(
        blocked_uris={b["track_uri"] for b in blocked},
        blocked_songs={song_key(b["title"], b["artist"]) for b in blocked if b["title"] and b["artist"]},
        avoid_artists={s.artist for s in scores(store, now) if s.avoid},
        served_uris=store.served_since(now - SERVED_RECENT_DAYS * DAY),
    )


@dataclass
class Pick:
    track: dict
    reason: str
    source: str  # library | agent

    def view(self) -> dict:
        return {"title": self.track.get("name", ""), "artist": ", ".join(artists_of(self.track)),
                "uri": self.track.get("uri"), "reason": self.reason, "source": self.source}


class Picker:
    """Accumulates picks under the shared rules (dedupe, ≤2 per artist, avoid-artists, exclusions)."""

    def __init__(self, ex: Exclusions, size: int, *, allow_avoid: bool) -> None:
        self.ex = ex
        self.size = size
        self.allow_avoid = allow_avoid
        self.picks: list[Pick] = []
        self.rejected: list[dict] = []
        self._uris: set = set()
        self._songs: set = set()
        self._per_artist: dict[str, int] = defaultdict(int)

    @property
    def full(self) -> bool:
        return len(self.picks) >= self.size

    def check(self, track: Mapping) -> str:
        uri = track.get("uri")
        artist = artist_key(primary_artist(track))
        if uri in self._uris or song_key(track.get("name", ""), primary_artist(track)) in self._songs:
            return "duplicate"
        why = self.ex.why_not(track)
        if why:
            return why
        if artist in self.ex.avoid_artists:
            if not self.allow_avoid:
                return "avoid_artist"
            if self._per_artist[artist] >= AVOID_PER_ARTIST:
                return "artist_cap"
        if self._per_artist[artist] >= PER_ARTIST:
            return "artist_cap"
        return ""

    def add(self, track: dict, reason: str, source: str) -> None:
        self.picks.append(Pick(track, clip(reason, REASON_MAX), source))
        self._uris.add(track.get("uri"))
        self._songs.add(song_key(track.get("name", ""), primary_artist(track)))
        self._per_artist[artist_key(primary_artist(track))] += 1

    def reject(self, title: str, artist: str, why: str) -> None:
        self.rejected.append({"title": title, "artist": artist, "why": why})


_SEASONS = ("winter", "winter", "spring", "spring", "spring", "summer", "summer", "summer", "autumn", "autumn",
            "autumn", "winter")


def _when(iso: Optional[str]) -> Optional[datetime]:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


_QUIET = ("not heard lately", "missing from your recent plays", "quiet for a while now", "")


class _CapsuleFacts:
    """Varied, true one-liners for rediscovered tracks (no LLM; every number comes from the library).

    Per track it prefers the most specific fact still unused: an album the user saved a lot from that
    year, an artist they saved many tracks by that year, an all-time favourite, a ❤️, then the season
    it was saved. Each artist-year / album-year story is told once per playlist, and the closing
    "not heard lately" rotates (or is dropped) so a 15-track list does not read as one template.
    """

    def __init__(self, saved: list) -> None:
        self.by_artist: Counter = Counter()
        self.by_album: Counter = Counter()
        for row in saved:
            added = _when(row.get("added_at"))
            track = row.get("track") if isinstance(row.get("track"), Mapping) else {}
            if not added or not track:
                continue
            self.by_artist[(added.year, artist_key(primary_artist(track)))] += 1
            album = str((track.get("album") or {}).get("name") or "")
            if album:
                self.by_album[(added.year, album)] += 1
        self.told: set = set()

    def reason(self, slot: Mapping, n: int) -> str:
        track, added, kinds = slot["track"], slot["added"], slot["reasons"]
        artist = primary_artist(track)
        options: list[tuple[str, str]] = []
        if added:
            year = added.year
            album = str((track.get("album") or {}).get("name") or "")
            n_album = self.by_album.get((year, album), 0)
            n_artist = self.by_artist.get((year, artist_key(artist)), 0)
            if album and n_album >= 4:
                options.append((f"album:{year}:{album}",
                                f"{album} was a {year} obsession: you saved {n_album} songs from it"))
            if artist and n_artist >= 3:
                options.append((f"artist:{year}:{artist_key(artist)}",
                                f"you saved {n_artist} {artist} tracks in {year}"))
        if "top" in kinds:
            options.append(("", "one of your all-time most played"))
        if "loved" in kinds:
            options.append(("", "you ❤️ it in Loshify"))
        if added:
            plain = (f"saved in {_SEASONS[added.month - 1]} {added.year}", f"from your {added.year} library",
                     f"a {_SEASONS[added.month - 1]} {added.year} save")
            options.append(("", plain[n % len(plain)]))
        if not options:
            options.append(("", "from your saved songs"))
        story, text = next(((k, t) for k, t in options if not k or k not in self.told), options[-1])
        if story:
            self.told.add(story)
        tail = _QUIET[n % len(_QUIET)]
        return clip(f"{text}, {tail}" if tail else text, REASON_MAX)


def time_capsule(*, saved: list, top_long: list, top_short: list, recent: list, loved: list,
                 ex: Exclusions, size: int) -> tuple[list[Pick], list[dict]]:
    """Deterministic rediscovery playlist (no LLM). Returns ``(picks, rejected)``.

    Pool = saved tracks ∪ all-time top tracks ∪ loved tracks, minus anything heard lately
    (short-term top, recently played) and the shared exclusions. Picks are spread across eras:
    tracks are grouped by the year they were saved and taken round-robin, oldest year first, so a
    25-track capsule is not 25 songs from last spring. Within a year, all-time favourites and loved
    tracks lead.
    """
    heard_lately = {t.get("uri") for t in top_short if _is_track(t)}
    heard_lately |= {(r.get("track") or {}).get("uri") for r in recent}
    pool: dict[str, dict] = {}

    def offer(track: Any, priority: int, added: Optional[datetime], reason: str) -> None:
        if not _is_track(track):
            return
        uri = track["uri"]
        slot = pool.get(uri)
        if slot is None:
            pool[uri] = {"track": dict(track), "priority": priority, "added": added, "reasons": [reason]}
        else:
            slot["priority"] += priority
            slot["added"] = slot["added"] or added
            if reason not in slot["reasons"]:
                slot["reasons"].append(reason)

    for row in saved:
        added = _when(row.get("added_at"))
        offer(row.get("track"), 1, added, "saved")
    for track in top_long:
        offer(track, 2, None, "top")
    for row in loved:
        offer({"uri": row["track_uri"], "name": row.get("title", ""), "artists": [{"name": row.get("artist", "")}]},
              2, None, "loved")
    facts = _CapsuleFacts(saved)

    picker = Picker(ex, size, allow_avoid=True)
    eras: dict[Any, list[dict]] = defaultdict(list)
    for slot in pool.values():
        if slot["track"]["uri"] in heard_lately:
            continue  # quietly: a capsule is about what you have NOT heard lately
        eras[slot["added"].year if slot["added"] else 0].append(slot)
    for era in eras.values():
        era.sort(key=lambda s: (-s["priority"], s["added"] or datetime.max.replace(tzinfo=timezone.utc),
                                s["track"]["uri"]))
    order = sorted(eras, key=lambda y: (y == 0, y))  # oldest year first; undated (top/loved only) last
    cursors = {year: 0 for year in order}
    while not picker.full and any(cursors[y] < len(eras[y]) for y in order):
        for year in order:
            if picker.full:
                break
            era = eras[year]
            while cursors[year] < len(era):
                slot = era[cursors[year]]
                cursors[year] += 1
                why = picker.check(slot["track"])
                if why:
                    if why != "duplicate":
                        picker.reject(slot["track"].get("name", ""), primary_artist(slot["track"]), why)
                    continue
                picker.add(slot["track"], facts.reason(slot, len(picker.picks)), "library")
                break
    return picker.picks, picker.rejected


def known_artists(store: Store, now: Optional[int] = None) -> set:
    """Artist keys the user already knows: anything with a seed (top/saved/recent) or a reaction."""
    return {s.artist for s in scores(store, now) if s.seed > 0.01 or s.love > 0.01 or s.okay > 0.01}


def _is_known(track: Mapping, known: set) -> bool:
    return any(artist_key(a) in known for a in artists_of(track))


def from_candidates(candidates: list, resolver: Resolver, *, ex: Exclusions, size: int, known: set,
                    familiar_quota: bool, seed: Iterable[Pick] = ()) -> tuple[list[Pick], list[dict], list[dict]]:
    """Resolve agent candidates in order and pick under the rules. ``(picks, unresolved, rejected)``.

    *seed*: picks already in place (a per-slot top-up keeps the slot's current tracks). They come first,
    count toward *size*, the per-artist cap and the dedupe.

    With *familiar_quota* (discover / deep_dig) at most ``FAMILIAR_MAX_SHARE`` of the picks are by
    *known* artists. A candidate named by a known artist is not searched during the new-artist pass;
    afterwards it is searched only while the share still allows it (else rejected ``familiar_quota``),
    so a list full of library artists costs no search budget. Candidates are resolved in parallel
    batches of exactly what is still needed, so the search count matches a sequential run.
    """
    picker = Picker(ex, size, allow_avoid=False)
    for p in seed:
        if not picker.full and not picker.check(p.track):
            picker.add(p.track, p.reason, p.source)
    unresolved: list[dict] = []
    queue: list[tuple[str, str, str]] = []
    deferred: list[tuple[str, str, str]] = []
    for cand in candidates[:MAX_CANDIDATES]:
        title, artist = str(cand.get("title") or "").strip(), str(cand.get("artist") or "").strip()
        reason = str(cand.get("reason") or "").strip()
        if artist_key(artist) in ex.avoid_artists:
            picker.reject(title, artist, "avoid_artist")  # no search spent on a known no
            continue
        if reason and is_meta_reason(reason):
            picker.reject(title, artist, "meta_reason")  # a draft / correction / placeholder, not a pick
            continue
        (deferred if familiar_quota and artist_key(artist) in known else queue).append((title, artist, reason))

    held: list[tuple[dict, str]] = []  # resolved, but credited to a known artist (e.g. featured)
    i = 0
    while i < len(queue) and not picker.full:
        chunk = queue[i:i + picker.size - len(picker.picks) + LOOKAHEAD]
        resolver.prefetch((t, a) for t, a, _ in chunk)
        for title, artist, reason in chunk:
            i += 1
            track, why = resolver.resolve(title, artist)
            if track is None:
                unresolved.append({"title": title, "artist": artist, "why": why})
                continue
            problem = picker.check(track)
            if problem:
                picker.reject(title, artist, problem)
                continue
            if familiar_quota and _is_known(track, known):
                held.append((track, reason))
                continue
            if picker.full:
                picker.reject(title, artist, "full")
                continue
            picker.add(track, reason, "agent")
    if not familiar_quota:
        return picker.picks, unresolved, picker.rejected

    # Known artists: admitted while familiar/(total) stays <= the cap.
    n_familiar = sum(1 for p in picker.picks if _is_known(p.track, known))

    def admissible() -> bool:
        return not picker.full and (n_familiar + 1) <= FAMILIAR_MAX_SHARE * (len(picker.picks) + 1)

    for track, reason in held:
        if not admissible():
            picker.reject(track.get("name", ""), primary_artist(track), "familiar_quota")
            continue
        problem = picker.check(track)
        if problem:
            picker.reject(track.get("name", ""), primary_artist(track), problem)
            continue
        picker.add(track, reason, "agent")
        n_familiar += 1
    for title, artist, reason in deferred:
        if not admissible():
            picker.reject(title, artist, "familiar_quota")
            continue
        track, why = resolver.resolve(title, artist)
        if track is None:
            unresolved.append({"title": title, "artist": artist, "why": why})
            continue
        problem = picker.check(track)
        if problem:
            picker.reject(title, artist, problem)
            continue
        picker.add(track, reason, "agent")
        n_familiar += 1
    return picker.picks, unresolved, picker.rejected


def known_artist_names(store: Store, now: Optional[int] = None, limit: int = 150) -> list[str]:
    """Display names of artists the user already knows, strongest first: what a discover slot avoids."""
    return [s.display for s in scores(store, now)
            if s.seed > 0.01 or s.love > 0.01 or s.okay > 0.01][:max(0, int(limit))]


# ── writer ─────────────────────────────────────────────────────────────────────────────────────

def default_name(template: str, now: Optional[int] = None) -> str:
    stamp = datetime.fromtimestamp(_now(now), tz=timezone.utc).strftime("%b %Y")
    return f"Loshify · {TEMPLATE_LABEL.get(template, template)} · {stamp}"


def description(template: str, now: Optional[int] = None) -> str:
    stamp = datetime.fromtimestamp(_now(now), tz=timezone.utc).strftime("%Y-%m-%d")
    return f"Curated by Loshify for you · {TEMPLATE_LABEL.get(template, template)} · {stamp}"


def write_playlist(client: SpotifyClient, store: Store, template: str, picks: list[Pick], *, mode: str = "new",
                   name: Optional[str] = None, playlist_id: Optional[str] = None, params: Optional[dict] = None,
                   description_text: Optional[str] = None, now: Optional[int] = None,
                   on_created: Optional[Callable[[str], None]] = None) -> dict:
    """Create (``new``) or replace (``refresh``) a Loshify-owned playlist, then log what was served.

    ``refresh`` targets *playlist_id* (must be one Loshify created) or the newest managed playlist of
    the same template; with neither it creates a new one. Replace is idempotent (PUT).
    *description_text* overrides the default description of a newly created playlist.
    """
    now = _now(now)
    uris = [p.track["uri"] for p in picks]
    target = None
    if mode == "refresh":
        if playlist_id:
            target = store.get_playlist(playlist_id)
            if target is None:
                raise SpotifyError("Loshify only refreshes playlists it created; that one is not managed.",
                                   reason="not_managed")
        else:
            target = store.latest_playlist(template)
    if target is None:
        name = clip(name or default_name(template, now), 100)
        created = client.create_playlist(name, description_text or description(template, now), public=False)
        pid = str(created.get("id") or "")
        if not pid:
            raise SpotifyError("Spotify did not return the new playlist's id.", reason="no_playlist_id")
        url = ((created.get("external_urls") or {}).get("spotify")) or PLAYLIST_URL + pid
        store.add_playlist(pid, name, template, params, ts=now)  # managed from the moment it exists
        if on_created is not None:
            on_created(pid)
        action = "created"
    else:
        pid, name = target["id"], (clip(name, 100) if name else target["name"])
        url = PLAYLIST_URL + pid
        store.add_playlist(pid, name, template, {**(target.get("params") or {}), **(params or {})}, ts=now)
        action = "refreshed"
    client.replace_items(pid, uris)
    store.touch_playlist(pid, ts=now)
    store.record_served(pid, [(p.track["uri"], p.reason) for p in picks], template=template, ts=now)
    return {"id": pid, "name": name, "url": url, "uri": f"spotify:playlist:{pid}", "action": action,
            "tracks": len(uris)}


# ── template sources (I/O) ─────────────────────────────────────────────────────────────────────

def _soft(fn, default):
    """Optional source: 403/404/5xx/network (e.g. an endpoint closed to new apps) degrade to *default*;
    sign-in / client-id / app problems still raise so the user gets the setup step."""
    try:
        return fn()
    except SpotifyError as exc:
        if exc.setup and exc.setup != SETUP_PREMIUM_OR_USERS:
            raise
        return default


def time_capsule_sources(client: SpotifyClient, store: Store, *, saved_max: int = 500) -> dict:
    return {
        "saved": client.saved_tracks(max_items=saved_max),
        "top_long": _soft(lambda: client.top("tracks", "long_term"), []),
        "top_short": _soft(lambda: client.top("tracks", "short_term"), []),
        "recent": _soft(lambda: client.recently_played(), []),
        "loved": store.loved(),
    }


def curate(client: SpotifyClient, store: Store, template: str, *, candidates: Optional[list] = None,
           mode: str = "new", name: Optional[str] = None, playlist_id: Optional[str] = None,
           size: int = SIZE_DEFAULT, mood: Optional[str] = None, now: Optional[int] = None) -> dict:
    """Run one template end to end. Raises ``SpotifyError`` for sign-in/app problems only."""
    now = _now(now)
    if template not in TEMPLATES:
        return {"ok": False, "error": f"template must be one of {', '.join(TEMPLATES)}"}
    if mode not in ("new", "refresh"):
        return {"ok": False, "error": "mode must be 'new' or 'refresh'"}
    size = clamp_size(size)
    ex = exclusions(store, now)
    unresolved: list[dict] = []
    calls_before = client.calls
    if template == "time_capsule":
        picks, rejected = time_capsule(**time_capsule_sources(client, store), ex=ex, size=size)
        searches = 0
    else:
        cands = [c for c in (candidates or []) if isinstance(c, Mapping)]
        if not cands:
            return {"ok": False, "error": f"{template} needs candidates: [{{title, artist, reason}}, …]"}
        resolver = Resolver(client, market=store.get_meta("market"))
        picks, unresolved, rejected = from_candidates(
            cands, resolver, ex=ex, size=size, known=known_artists(store, now),
            familiar_quota=(template == "deep_dig"))
        searches = resolver.searches
    out: dict[str, Any] = {"template": template, "tracks": [p.view() for p in picks], "unresolved": unresolved,
                           "rejected": rejected, "searches": searches}
    if not picks:
        out.update(ok=False, error="No track survived resolution and the filters; nothing was written.")
        out["spotify_calls"] = client.calls - calls_before
        return out
    params = {"size": size, **({"mood": clip(mood, 200)} if mood else {})}
    out["playlist"] = write_playlist(client, store, template, picks, mode=mode, name=name, playlist_id=playlist_id,
                                     params=params, now=now)
    out["ok"] = True
    out["short"] = len(picks) < size
    out["spotify_calls"] = client.calls - calls_before
    return out


__all__ = [
    "Exclusions", "Match", "Pick", "Picker", "Resolver", "TEMPLATES", "best_match", "clamp_size", "curate", "exclusions",
    "from_candidates", "known_artist_names", "known_artists", "song_key", "time_capsule", "write_playlist",
]
