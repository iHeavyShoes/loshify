"""Loshify's local state: one SQLite file per Hermes profile.

``open_store()`` uses Hermes's ``plugins.plugin_storage.plugin_db("loshify")`` →
``<HERMES_HOME>/plugin-data/loshify/data.db`` (WAL where safe). Tests pass a path or ``":memory:"``.

Schema changes are migrations keyed on ``PRAGMA user_version``. Nothing here talks to the network.

Retention (bounded by design, see PLAN §3):
- ``reactions``: raw rows, kept until digested (>= 30 days, or the oldest beyond 1,000 undigested rows).
- ``artist_score``: decayed aggregates. Each row stores values *as of* ``ref_ts``; readers decay them.
- ``loved_tracks``: capped at 500 (least recently loved dropped).
- ``blocked_tracks``: kept forever (only grows by a deliberate 👎).
- ``okay_tally``: each track's 👌 count carried across digests (never decayed; a 👎 or a promotion
  resets it), so three 👌 on one track make a ❤️ however far apart they are. Capped at 5,000 tracks.
- ``served``: capped at 2,000 rows and 120 days.
- ``playlists``: the playlists this plugin created (the only ones it may edit).
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Iterable, Optional, Union

from .text import artist_key, clip

PLUGIN_NAME = "loshify"
REACTIONS = ("love", "okay", "down")
SOURCES = ("desktop", "chat", "import")

LOVED_CAP = 500
OKAY_TALLY_CAP = 5000
SERVED_CAP = 2000
SERVED_MAX_AGE_S = 120 * 86400
PORTRAIT_MAX = 280
NOTE_MAX = 500
TITLE_MAX = 300

DAY = 86400
UNDO_TTL_S = DAY  # a 👎 can be undone (track put back) for a day; after that it is a decision

_MIGRATIONS: list[str] = [
    # v1
    """
    CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE reactions (
        id TEXT PRIMARY KEY,
        ts INTEGER NOT NULL,
        track_uri TEXT NOT NULL,
        title TEXT NOT NULL DEFAULT '',
        artists TEXT NOT NULL DEFAULT '[]',
        album TEXT NOT NULL DEFAULT '',
        reaction TEXT NOT NULL CHECK (reaction IN ('love', 'okay', 'down')),
        source TEXT NOT NULL DEFAULT 'desktop',
        playlist_uri TEXT,
        note TEXT,
        digested INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX reactions_ts ON reactions (ts);
    CREATE INDEX reactions_undigested ON reactions (digested, ts);
    CREATE TABLE artist_score (
        artist TEXT PRIMARY KEY,
        display TEXT NOT NULL,
        love REAL NOT NULL DEFAULT 0,
        okay REAL NOT NULL DEFAULT 0,
        down REAL NOT NULL DEFAULT 0,
        seed REAL NOT NULL DEFAULT 0,
        ref_ts INTEGER NOT NULL
    );
    CREATE TABLE loved_tracks (
        track_uri TEXT PRIMARY KEY,
        title TEXT NOT NULL DEFAULT '',
        artist TEXT NOT NULL DEFAULT '',
        last_ts INTEGER NOT NULL,
        n INTEGER NOT NULL DEFAULT 1
    );
    CREATE INDEX loved_last ON loved_tracks (last_ts);
    CREATE TABLE blocked_tracks (
        track_uri TEXT PRIMARY KEY,
        title TEXT NOT NULL DEFAULT '',
        artist TEXT NOT NULL DEFAULT '',
        ts INTEGER NOT NULL
    );
    CREATE TABLE served (
        track_uri TEXT NOT NULL,
        playlist_id TEXT NOT NULL,
        ts INTEGER NOT NULL,
        template TEXT NOT NULL DEFAULT '',
        reason TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX served_ts ON served (ts);
    CREATE INDEX served_uri ON served (track_uri);
    CREATE TABLE playlists (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        template TEXT NOT NULL DEFAULT '',
        created_ts INTEGER NOT NULL,
        updated_ts INTEGER NOT NULL,
        params TEXT NOT NULL DEFAULT '{}'
    );
    """,
    # v2: the per-track 👌 count survives the digest (3×👌 = ❤️ across any time span)
    """
    CREATE TABLE okay_tally (
        track_uri TEXT PRIMARY KEY,
        n INTEGER NOT NULL DEFAULT 0,
        last_ts INTEGER NOT NULL
    );
    CREATE INDEX okay_tally_last ON okay_tally (last_ts);
    """,
]
SCHEMA_VERSION = len(_MIGRATIONS)


def _now() -> int:
    return int(time.time())


def migrate(conn: sqlite3.Connection) -> int:
    """Bring *conn* to ``SCHEMA_VERSION``. Refuses a newer file (written by a newer Loshify)."""
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current > SCHEMA_VERSION:
        raise RuntimeError(f"Loshify data was written by a newer version (schema {current} > {SCHEMA_VERSION})")
    for version in range(current, SCHEMA_VERSION):
        with conn:
            conn.executescript("BEGIN;" + _MIGRATIONS[version] + f"PRAGMA user_version = {version + 1};")
    return SCHEMA_VERSION


def _connect(path: Union[str, Path, None]) -> sqlite3.Connection:
    if path is None:
        from plugins.plugin_storage import plugin_db  # Hermes: <HERMES_HOME>/plugin-data/loshify/data.db

        conn = plugin_db(PLUGIN_NAME)
    else:
        conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


class Store:
    """All reads/writes of Loshify state. Thread use: one Store per thread, or serialize externally."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        try:
            migrate(self.conn)
        except BaseException:
            conn.close()  # a refused (newer) or failed migration must not leak the connection
            raise

    def close(self) -> None:
        self.conn.close()

    # ── meta ─────────────────────────────────────────────────────────────────────────────────
    def get_meta(self, key: str, default: Optional[str] = None) -> Optional[str]:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return default if row is None else row["value"]

    def set_meta(self, key: str, value: Optional[Any]) -> None:
        with self.conn:
            if value is None:
                self.conn.execute("DELETE FROM meta WHERE key = ?", (key,))
            else:
                self.conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) "
                                  "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, str(value)))

    def get_meta_int(self, key: str, default: int = 0) -> int:
        try:
            return int(self.get_meta(key, None) or default)
        except ValueError:
            return default

    @property
    def onboarded_at(self) -> Optional[int]:
        value = self.get_meta_int("onboarded_at", 0)
        return value or None

    def set_portrait(self, text: str) -> str:
        text = clip(" ".join(str(text or "").split()), PORTRAIT_MAX)
        self.set_meta("portrait", text or None)
        return text

    # ── reactions (raw) ──────────────────────────────────────────────────────────────────────
    def add_reaction(self, track_uri: str, reaction: str, *, title: str = "", artists: Iterable[str] = (),
                     album: str = "", source: str = "desktop", playlist_uri: Optional[str] = None,
                     note: Optional[str] = None, ts: Optional[int] = None, reaction_id: Optional[str] = None) -> str:
        if reaction not in REACTIONS:
            raise ValueError(f"reaction must be one of {REACTIONS}")
        if source not in SOURCES:
            raise ValueError(f"source must be one of {SOURCES}")
        if not str(track_uri).startswith("spotify:track:"):
            raise ValueError("track_uri must be a spotify:track: URI")
        rid = reaction_id or str(uuid.uuid4())
        names = [clip(a, TITLE_MAX) for a in artists if str(a).strip()]
        with self.conn:
            self.conn.execute(
                "INSERT INTO reactions (id, ts, track_uri, title, artists, album, reaction, source, playlist_uri, note) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (rid, int(ts if ts is not None else _now()), track_uri, clip(title, TITLE_MAX),
                 json.dumps(names, ensure_ascii=False), clip(album, TITLE_MAX), reaction, source, playlist_uri,
                 clip(note, NOTE_MAX) if note else None))
        return rid

    def get_reaction(self, reaction_id: str) -> Optional[dict]:
        row = self.conn.execute("SELECT * FROM reactions WHERE id = ?", (reaction_id,)).fetchone()
        return _reaction_row(row) if row else None

    def amend_reaction(self, reaction_id: str, *, reaction: Optional[str] = None, note: Optional[str] = None) -> bool:
        """Change an undigested reaction in place (a different emoji, or a note). False if gone/digested."""
        if reaction is not None and reaction not in REACTIONS:
            raise ValueError(f"reaction must be one of {REACTIONS}")
        sets, args = [], []
        if reaction is not None:
            sets.append("reaction = ?")
            args.append(reaction)
        if note is not None:
            sets.append("note = ?")
            args.append(clip(note, NOTE_MAX) or None)
        if not sets:
            return False
        with self.conn:
            cur = self.conn.execute(f"UPDATE reactions SET {', '.join(sets)} WHERE id = ? AND digested = 0",
                                    (*args, reaction_id))
        return cur.rowcount == 1

    def retract_reaction(self, reaction_id: str) -> bool:
        """Undo: remove an undigested reaction entirely. False if unknown or already digested."""
        with self.conn:
            cur = self.conn.execute("DELETE FROM reactions WHERE id = ? AND digested = 0", (reaction_id,))
        return cur.rowcount == 1

    def undigested(self, *, before_ts: Optional[int] = None) -> list[dict]:
        sql, args = "SELECT * FROM reactions WHERE digested = 0", []
        if before_ts is not None:
            sql += " AND ts < ?"
            args.append(int(before_ts))
        return [_reaction_row(r) for r in self.conn.execute(sql + " ORDER BY ts, id", args)]

    def reactions_since(self, ts: int, limit: int = 200) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM reactions WHERE ts > ? ORDER BY ts, id LIMIT ?", (int(ts), int(limit)))
        return [_reaction_row(r) for r in rows]

    def count_reactions(self, *, since_ts: Optional[int] = None) -> int:
        if since_ts is None:
            return self.conn.execute("SELECT COUNT(*) FROM reactions").fetchone()[0]
        return self.conn.execute("SELECT COUNT(*) FROM reactions WHERE ts > ?", (int(since_ts),)).fetchone()[0]

    def mark_digested(self, ids: Iterable[str]) -> int:
        ids = list(ids)
        with self.conn:
            self.conn.executemany("DELETE FROM reactions WHERE id = ?", [(i,) for i in ids])
        return len(ids)

    # ── artist aggregates ────────────────────────────────────────────────────────────────────
    def artist_rows(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM artist_score")]

    def artist_row(self, name: str) -> Optional[dict]:
        row = self.conn.execute("SELECT * FROM artist_score WHERE artist = ?", (artist_key(name),)).fetchone()
        return dict(row) if row else None

    def put_artist(self, key: str, display: str, love: float, okay: float, down: float, seed: float,
                   ref_ts: int) -> None:
        """Write one aggregate row whose values are valid as of *ref_ts* (callers decay first)."""
        with self.conn:
            self.conn.execute(
                "INSERT INTO artist_score (artist, display, love, okay, down, seed, ref_ts) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(artist) DO UPDATE SET display = excluded.display, love = excluded.love, "
                "okay = excluded.okay, down = excluded.down, seed = excluded.seed, ref_ts = excluded.ref_ts",
                (key, display, love, okay, down, seed, int(ref_ts)))

    def forget_artist(self, name: str) -> bool:
        with self.conn:
            cur = self.conn.execute("DELETE FROM artist_score WHERE artist = ?", (artist_key(name),))
        return cur.rowcount == 1

    def prune_artists(self, epsilon: float = 0.01) -> int:
        with self.conn:
            cur = self.conn.execute("DELETE FROM artist_score WHERE love < ? AND okay < ? AND down < ? AND seed < ?",
                                    (epsilon,) * 4)
        return cur.rowcount

    # ── loved / blocked tracks ───────────────────────────────────────────────────────────────
    def add_loved(self, track_uri: str, title: str, artist: str, ts: int) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO loved_tracks (track_uri, title, artist, last_ts, n) VALUES (?, ?, ?, ?, 1) "
                "ON CONFLICT(track_uri) DO UPDATE SET n = n + 1, last_ts = MAX(last_ts, excluded.last_ts), "
                "title = excluded.title, artist = excluded.artist",
                (track_uri, clip(title, TITLE_MAX), clip(artist, TITLE_MAX), int(ts)))
            self.conn.execute(
                "DELETE FROM loved_tracks WHERE track_uri IN (SELECT track_uri FROM loved_tracks "
                "ORDER BY last_ts DESC, track_uri LIMIT -1 OFFSET ?)", (LOVED_CAP,))

    def loved(self, limit: int = LOVED_CAP) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM loved_tracks ORDER BY last_ts DESC, track_uri LIMIT ?", (int(limit),))]

    def add_blocked(self, track_uri: str, title: str, artist: str, ts: int) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO blocked_tracks (track_uri, title, artist, ts) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(track_uri) DO UPDATE SET ts = excluded.ts",
                (track_uri, clip(title, TITLE_MAX), clip(artist, TITLE_MAX), int(ts)))
            # A blocked track is never a loved one.
            self.conn.execute("DELETE FROM loved_tracks WHERE track_uri = ?", (track_uri,))

    def unblock(self, track_uri: str) -> bool:
        with self.conn:
            cur = self.conn.execute("DELETE FROM blocked_tracks WHERE track_uri = ?", (track_uri,))
        return cur.rowcount == 1

    def blocked_uris(self) -> set[str]:
        """Blocked = digested 👎 plus any undigested 👎 (a fresh 👎 must block immediately)."""
        rows = self.conn.execute("SELECT track_uri FROM blocked_tracks UNION "
                                 "SELECT track_uri FROM reactions WHERE reaction = 'down' AND digested = 0")
        return {r[0] for r in rows}

    def count_blocked(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM blocked_tracks").fetchone()[0]

    def blocked_rows(self) -> list[dict]:
        """``{track_uri, title, artist}`` for digested 👎 plus undigested 👎 (artist = primary artist)."""
        out = [dict(r) for r in self.conn.execute("SELECT track_uri, title, artist FROM blocked_tracks")]
        for row in self.conn.execute("SELECT track_uri, title, artists FROM reactions "
                                     "WHERE reaction = 'down' AND digested = 0"):
            try:
                names = json.loads(row["artists"] or "[]")
            except ValueError:
                names = []
            out.append({"track_uri": row["track_uri"], "title": row["title"], "artist": names[0] if names else ""})
        return out

    # ── per-track 👌 tally (survives the digest) ─────────────────────────────────────────────
    def okay_counts(self, uris: Iterable[str]) -> dict[str, int]:
        """``{uri: n}``: digested 👌 on each track since its last 👎 or promotion (missing = 0)."""
        wanted = list(dict.fromkeys(uris))
        out: dict[str, int] = {}
        for i in range(0, len(wanted), 500):
            chunk = wanted[i:i + 500]
            marks = ",".join("?" * len(chunk))
            for row in self.conn.execute(f"SELECT track_uri, n FROM okay_tally WHERE track_uri IN ({marks})", chunk):
                out[row[0]] = int(row[1])
        return out

    def set_okay_counts(self, counts: dict, ts: int) -> None:
        """Store each track's carried 👌 count (0 forgets the row), then enforce ``OKAY_TALLY_CAP``."""
        with self.conn:
            for uri, n in counts.items():
                if int(n) > 0:
                    self.conn.execute(
                        "INSERT INTO okay_tally (track_uri, n, last_ts) VALUES (?, ?, ?) "
                        "ON CONFLICT(track_uri) DO UPDATE SET n = excluded.n, last_ts = excluded.last_ts",
                        (uri, int(n), int(ts)))
                else:
                    self.conn.execute("DELETE FROM okay_tally WHERE track_uri = ?", (uri,))
            self.conn.execute(
                "DELETE FROM okay_tally WHERE track_uri IN (SELECT track_uri FROM okay_tally "
                "ORDER BY last_ts DESC, track_uri LIMIT -1 OFFSET ?)", (OKAY_TALLY_CAP,))

    # ── undo records (short-lived; kept in meta so chat undo survives across tool calls) ────────
    def put_undo(self, reaction_id: str, record: dict, ts: Optional[int] = None) -> None:
        now = int(ts if ts is not None else _now())
        self.set_meta(f"undo:{reaction_id}", json.dumps({**record, "ts": now}, ensure_ascii=False))
        self.prune_undo(now)

    def pop_undo(self, reaction_id: str, ts: Optional[int] = None) -> Optional[dict]:
        now = int(ts if ts is not None else _now())
        self.prune_undo(now)
        raw = self.get_meta(f"undo:{reaction_id}")
        if raw is None:
            return None
        self.set_meta(f"undo:{reaction_id}", None)
        try:
            return json.loads(raw)
        except ValueError:
            return None

    def prune_undo(self, now: Optional[int] = None, ttl_s: int = UNDO_TTL_S) -> int:
        now = int(now if now is not None else _now())
        stale = []
        for row in self.conn.execute("SELECT key, value FROM meta WHERE key LIKE 'undo:%'"):
            try:
                ts = int(json.loads(row["value"]).get("ts", 0))
            except (ValueError, TypeError, AttributeError):
                ts = 0
            if now - ts > ttl_s:
                stale.append(row["key"])
        with self.conn:
            self.conn.executemany("DELETE FROM meta WHERE key = ?", [(k,) for k in stale])
        return len(stale)

    # ── served log ───────────────────────────────────────────────────────────────────────────
    def record_served(self, playlist_id: str, items: Iterable[tuple], *, template: str = "",
                      ts: Optional[int] = None) -> int:
        """*items*: ``(track_uri, reason)`` pairs. Enforces the cap and age limit afterwards."""
        now = int(ts if ts is not None else _now())
        rows = [(uri, playlist_id, now, template, clip(reason or "", 200)) for uri, reason in items]
        with self.conn:
            self.conn.executemany("INSERT INTO served (track_uri, playlist_id, ts, template, reason) "
                                  "VALUES (?, ?, ?, ?, ?)", rows)
            self.conn.execute("DELETE FROM served WHERE ts < ?", (now - SERVED_MAX_AGE_S,))
            self.conn.execute("DELETE FROM served WHERE rowid IN (SELECT rowid FROM served "
                              "ORDER BY ts DESC, rowid DESC LIMIT -1 OFFSET ?)", (SERVED_CAP,))
        return len(rows)

    def served_since(self, ts: int) -> set[str]:
        return {r[0] for r in self.conn.execute("SELECT DISTINCT track_uri FROM served WHERE ts >= ?", (int(ts),))}

    def served_reasons(self, playlist_id: str) -> dict[str, str]:
        """``{track_uri: reason}`` last recorded for *playlist_id* (the newest row wins)."""
        rows = self.conn.execute("SELECT track_uri, reason FROM served WHERE playlist_id = ? ORDER BY ts, rowid",
                                 (playlist_id,))
        return {r[0]: r[1] for r in rows}

    def count_served(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM served").fetchone()[0]

    # ── managed playlists ────────────────────────────────────────────────────────────────────
    def add_playlist(self, playlist_id: str, name: str, template: str, params: Optional[dict] = None,
                     ts: Optional[int] = None) -> None:
        now = int(ts if ts is not None else _now())
        with self.conn:
            self.conn.execute(
                "INSERT INTO playlists (id, name, template, created_ts, updated_ts, params) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET name = excluded.name, template = excluded.template, "
                "updated_ts = excluded.updated_ts, params = excluded.params",
                (playlist_id, clip(name, 100), template, now, now, json.dumps(params or {}, ensure_ascii=False)))

    def touch_playlist(self, playlist_id: str, ts: Optional[int] = None) -> bool:
        with self.conn:
            cur = self.conn.execute("UPDATE playlists SET updated_ts = ? WHERE id = ?",
                                    (int(ts if ts is not None else _now()), playlist_id))
        return cur.rowcount == 1

    def is_managed(self, playlist_id_or_uri: str) -> bool:
        pid = str(playlist_id_or_uri or "").rsplit(":", 1)[-1]
        return self.conn.execute("SELECT 1 FROM playlists WHERE id = ?", (pid,)).fetchone() is not None

    def playlists(self) -> list[dict]:
        out = []
        for r in self.conn.execute("SELECT * FROM playlists ORDER BY updated_ts DESC"):
            row = dict(r)
            row["params"] = json.loads(row["params"] or "{}")
            out.append(row)
        return out

    def get_playlist(self, playlist_id_or_uri: str) -> Optional[dict]:
        pid = str(playlist_id_or_uri or "").rsplit(":", 1)[-1]
        return next((p for p in self.playlists() if p["id"] == pid), None)

    def latest_playlist(self, template: Optional[str] = None) -> Optional[dict]:
        return next((p for p in self.playlists() if template is None or p["template"] == template), None)

    def forget_playlist(self, playlist_id: str) -> bool:
        with self.conn:
            cur = self.conn.execute("DELETE FROM playlists WHERE id = ?", (playlist_id,))
        return cur.rowcount == 1


def _reaction_row(row: sqlite3.Row) -> dict:
    out = dict(row)
    try:
        out["artists"] = json.loads(out.get("artists") or "[]")
    except ValueError:
        out["artists"] = []
    return out


def open_store(path: Union[str, Path, None] = None) -> Store:
    """The profile's store (``path=None``: Hermes ``plugin_db``), or a file/``":memory:"`` for tests."""
    return Store(_connect(path))


__all__ = ["LOVED_CAP", "OKAY_TALLY_CAP", "PORTRAIT_MAX", "REACTIONS", "SCHEMA_VERSION", "SERVED_CAP", "SERVED_MAX_AGE_S", "Store",
           "migrate", "open_store"]
