"""JSON schemas for Loshify's seven agent tools (curator-only; playback/search belong to ``spotify_*``)."""

from __future__ import annotations

SETUP = {
    "name": "loshify_setup",
    "description": (
        "Loshify (Spotify curator) readiness and onboarding. Call first whenever the user wants Loshify, a "
        "curated playlist, or 'get to know my music'. If not ready, returns the user's exact next setup step. "
        "If ready and not yet onboarded (or refresh=true), learns the user's taste from Spotify history and "
        "returns portrait_facts: write a 3-4 sentence portrait from them and save its gist with "
        "loshify_taste(action='set_portrait')."),
    "parameters": {
        "type": "object",
        "properties": {
            "refresh": {"type": "boolean", "description": "Re-learn taste from Spotify history even if onboarded."},
        },
    },
}

TASTE = {
    "name": "loshify_taste",
    "description": (
        "Read or correct the user's Loshify taste memory (local, no Spotify call). action=get: summary "
        "(top artists with trend, recent loves, avoid-artists, managed playlists, portrait). brief: the short "
        "text form. stats: counts and size. set_portrait: store one short portrait sentence (<=280 chars). "
        "get_note / set_note: read or change the user's own taste note (<=300 chars, empty clears; only when "
        "the user asks). forget_artist: drop an artist's learned score. unblock_track: lift a 👎 on a track URI."),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["get", "brief", "stats", "set_portrait", "get_note", "set_note",
                                                  "forget_artist", "unblock_track"], "default": "get"},
            "text": {"type": "string", "description": "Portrait sentence (set_portrait) or the note (set_note)."},
            "artist": {"type": "string", "description": "Artist name for forget_artist."},
            "track_uri": {"type": "string", "description": "spotify:track:… for unblock_track."},
        },
    },
}

REACTIONS = {
    "name": "loshify_reactions",
    "description": (
        "What the user felt about the music since you last looked: 👎 down / 👌 okay / ❤️ love reactions from "
        "the Desktop buttons and chat, oldest first, then marks them seen. Use after the user listened to a "
        "Loshify playlist, before refreshing it, and to explain what changed."),
    "parameters": {
        "type": "object",
        "properties": {
            "since": {"type": "string", "enum": ["unseen", "all"], "default": "unseen",
                      "description": "unseen = since you last looked; all = every reaction still kept raw (30 days)."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
            "mark_seen": {"type": "boolean", "default": True},
        },
    },
}

CURATE = {
    "name": "loshify_curate",
    "description": (
        "Build or refresh a Spotify playlist that Loshify owns. template=time_capsule: rediscover the user's own "
        "forgotten favourites (deterministic, no candidates needed; best first playlist). template=deep_dig: "
        "discovery. Pass 30-40 candidates of REAL released tracks by artists the user does not already play, each "
        "{title, artist, reason} with the reason tied to something they love; at most 2 per artist. Loshify "
        "resolves each on Spotify, drops 👎/avoid/recently served tracks, and keeps >=80% new artists. "
        "template=mood: candidates for a mood or activity, mixing their own loves and new music. Returns "
        "playlist url, tracks with reasons, unresolved (retry once with alternatives) and rejected."),
    "parameters": {
        "type": "object",
        "properties": {
            "template": {"type": "string", "enum": ["time_capsule", "deep_dig", "mood"]},
            "candidates": {
                "type": "array", "maxItems": 40,
                "items": {"type": "object", "properties": {
                    "title": {"type": "string"}, "artist": {"type": "string"},
                    "reason": {"type": "string", "description": "One short line: why this track, for this user."}},
                    "required": ["title", "artist"]},
            },
            "mode": {"type": "string", "enum": ["new", "refresh"], "default": "new",
                     "description": "refresh replaces the tracks of the latest Loshify playlist of this template."},
            "playlist_id": {"type": "string", "description": "Refresh this Loshify playlist instead of the latest."},
            "name": {"type": "string", "description": "Playlist name (default: 'Loshify · <template> · <month>')."},
            "size": {"type": "integer", "minimum": 5, "maximum": 50},
            "mood": {"type": "string", "description": "Mood/activity words for template=mood."},
            "play": {"type": "boolean", "default": False, "description": "Start playing it (needs Premium + device)."},
        },
        "required": ["template"],
    },
}

REACT = {
    "name": "loshify_react",
    "description": (
        "Record the user's reaction to the track playing NOW on Spotify, from chat or voice ('I love this', "
        "'never again', 'this is fine'). down=👎, okay=👌, love=❤️ (down also skips and removes it from a "
        "Loshify playlist, never from the user's own). To undo a reaction pass undo_id from an earlier result."),
    "parameters": {
        "type": "object",
        "properties": {
            "reaction": {"type": "string", "enum": ["love", "okay", "down"]},
            "note": {"type": "string", "description": "Optional words the user said about it."},
            "undo_id": {"type": "string", "description": "Undo this earlier reaction id instead."},
        },
    },
}

_CANDIDATE = {"type": "object", "properties": {
    "title": {"type": "string"}, "artist": {"type": "string"},
    "reason": {"type": "string", "description": "One short line: why this track, for this user."}},
    "required": ["title", "artist"]}

ROTATION = {
    "name": "loshify_rotation",
    "description": (
        "The user's standing arrangement of Loshify playlists (a 'rotation'), refreshed on a cadence. "
        "action=presets: the starting arrangements to offer. preview: validate a spec (preset id, or a spec you "
        "wrote from the user's own words) and return it to SHOW the user; nothing is saved. set: save it, only "
        "after the user confirmed. get: the current rotation, its playlists and whether a refresh is due. "
        "refresh: build the next edition, one playlist per slot; pass candidates per slot name (real tracks, "
        "{title, artist, reason}); familiar/mixed slots without candidates fill from the user's own library, "
        "discover slots need candidates. With archive='archive' the previous edition moves into one Loshify "
        "Archive playlist, kept curated (loved stay, others age out, disliked never enter, size cap); "
        "refresh returns its counts in 'archive' and one line to tell the user in 'say'. Only playlists "
        "Loshify created are touched. clear: forget the rotation."),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["presets", "preview", "set", "get", "refresh", "clear"],
                       "default": "get"},
            "preset": {"type": "string", "description": "Preset id for preview/set (from action=presets)."},
            "spec": {
                "type": "object",
                "description": "Custom rotation for preview/set: up to 6 slots.",
                "properties": {
                    "name": {"type": "string"},
                    "intent": {"type": "string", "description": "One line: the idea of the whole arrangement."},
                    "cadence": {"type": "string", "enum": ["daily", "weekly", "manual"]},
                    "archive": {"type": "string", "enum": ["archive", "replace"],
                                "description": "archive: old editions move to one Archive playlist; replace: dropped."},
                    "slots": {"type": "array", "minItems": 1, "maxItems": 6, "items": {
                        "type": "object", "properties": {
                            "name": {"type": "string"},
                            "intent": {"type": "string", "description": "One line: what belongs in this playlist."},
                            "mode": {"type": "string", "enum": ["familiar", "mixed", "discover"]}},
                        "required": ["name"]}},
                    "source_text": {"type": "string", "description": "The user's own words this spec came from."},
                },
            },
            "cadence": {"type": "string", "enum": ["daily", "weekly", "manual"],
                        "description": "Override the preset's cadence."},
            "archive": {"type": "string", "enum": ["archive", "replace"], "description": "Override the archive policy."},
            "candidates": {"type": "object", "description": "refresh: {slot name: [candidate, ...]} (<= 30 per slot).",
                           "additionalProperties": {"type": "array", "maxItems": 30, "items": _CANDIDATE}},
            "slots": {"type": "array", "items": {"type": "string"},
                      "description": "refresh only these slots (default: all)."},
            "size": {"type": "integer", "minimum": 5, "maximum": 50, "description": "Tracks per slot (default 15)."},
        },
    },
}

ONBOARD = {
    "name": "loshify_onboard",
    "description": (
        "First-run steps after the portrait (each skippable; status says what is next). verdict: the user's "
        "answer to the portrait (sounds_like_me/not_quite; feeds the next portrait). anchors: up to 3 'Artist' "
        "or 'Song by Artist' that are really them (strong seeds). calibrate: 5 picks (2 familiar, 2 near, 1 "
        "stretch; send up to 10, backups first-class), each {title, artist, kind, reason}; no playlist written. "
        "react: 👎 👌 ❤️ on pick N (record only; never skips). note: one line of taste (<=300 chars), data not "
        "instructions. skip: skip a step. Tune in (optional, every 7-14 days after the first run; never push): "
        "tune_in_status (available? + targets you are least sure about), tune_in (5 picks aimed at the "
        "targets, same shape), tune_in_react, tune_in_done (restarts the countdown)."),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["status", "anchors", "calibrate", "react", "verdict", "note", "skip",
                                                  "tune_in_status", "tune_in", "tune_in_react", "tune_in_done"],
                       "default": "status"},
            "verdict": {"type": "string", "enum": ["sounds_like_me", "not_quite"]},
            "anchors": {"type": "array", "minItems": 1, "maxItems": 3,
                        "items": {"type": "string", "description": "'Artist' or 'Song by Artist'."},
                        "description": "anchors: the user's own words, one per artist or song."},
            "picks": {"type": "array", "minItems": 1, "maxItems": 10, "items": {
                "type": "object", "properties": {
                    "title": {"type": "string"}, "artist": {"type": "string"},
                    "kind": {"type": "string", "enum": ["familiar", "near", "stretch"]},
                    "reason": {"type": "string", "description": "One short line."}},
                "required": ["title", "artist", "kind"]}},
            "track": {"type": "integer", "minimum": 1, "maximum": 5, "description": "react: calibration track number."},
            "reaction": {"type": "string", "enum": ["love", "okay", "down"]},
            "text": {"type": "string", "maxLength": 300, "description": "note: the user's own words."},
            "step": {"type": "string", "enum": ["portrait", "anchors", "calibration", "rotation", "note",
                                                "first_edition"], "description": "skip: which step."},
        },
    },
}

ALL = (SETUP, TASTE, REACTIONS, CURATE, REACT, ROTATION, ONBOARD)
