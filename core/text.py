"""Text normalization shared by the store, taste and (Day 2) the resolver. Stdlib only."""

from __future__ import annotations

import re
import unicodedata

_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_SPACE = re.compile(r"\s+")


def norm(text: str) -> str:
    """Casefold, NFKC, punctuation -> space, collapsed whitespace. Hebrew and other scripts survive."""
    text = unicodedata.normalize("NFKC", str(text or "")).casefold()
    # Drop combining marks (niqqud, accents) so "Beyoncé" == "beyonce" and pointed Hebrew == unpointed.
    text = "".join(ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch))
    text = _PUNCT.sub(" ", text).replace("_", " ")
    return _SPACE.sub(" ", text).strip()


def artist_key(name: str) -> str:
    """Key for per-artist aggregates; a leading English article is ignored ("The Beatles" == "Beatles")."""
    key = norm(name)
    return key[4:] if key.startswith("the ") and len(key) > 4 else key


_VERSION_WORDS = (r"feat|ft|featuring|with|remaster(?:ed)?|live|version|edit|mono|stereo|deluxe|bonus|mix|"
                  r"remix|acoustic|demo|radio|single|album|explicit|clean|anniversary|re-?recorded|taylor's")
# "(feat. X)", "[2011 Remaster]", "(Live at …)"
_BRACKETED = re.compile(r"\s*[\(\[][^\)\]]*\b(?:" + _VERSION_WORDS + r")\b[^\)\]]*[\)\]]", re.IGNORECASE)
# " - 2011 Remaster", " - Live", " – Radio Edit"
_DASH_TAIL = re.compile(r"\s+[-–—]\s+[^-–—]*\b(?:" + _VERSION_WORDS + r")\b.*$", re.IGNORECASE)
# trailing "feat. X" without brackets
_FEAT_TAIL = re.compile(r"\s+(?:feat\.?|ft\.?|featuring)\s+.*$", re.IGNORECASE)


def title_key(title: str) -> str:
    """Comparable track title: version/feature decorations removed, then :func:`norm`.

    ``"Imagine - 2010 Remaster"`` == ``"Imagine"``; ``"Song (feat. X) [Live]"`` == ``"Song"``.
    Falls back to the plain normalized title if stripping would leave nothing.
    """
    raw = str(title or "")
    stripped = _FEAT_TAIL.sub("", _DASH_TAIL.sub("", _BRACKETED.sub("", raw)))
    return norm(stripped) or norm(raw)


def song_key_text(title: object, artist: object) -> tuple[str, str]:
    """``(title_key, artist_key)`` of loose values (the same identity as ``curate.song_key``)."""
    return title_key(str(title or "")), artist_key(str(artist or ""))


def similarity(a: str, b: str) -> float:
    """0..1 similarity of two already-normalized strings (stdlib ``difflib``)."""
    from difflib import SequenceMatcher

    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b, autojunk=False).ratio()


def clip(text: str, limit: int) -> str:
    text = str(text or "").strip()
    return text if len(text) <= limit else text[: max(0, limit - 1)].rstrip() + "…"


# The model's own thinking leaking into a user-facing line: "Wait, wrong title; skip", "Ignore: replaced
# below with a verified pick.", "Wait: this is an album title, so use…". A reason that starts with one of
# these words, or carries one as a standalone clause marker, is a draft or a correction, never a reason.
_META_WORDS = r"wait|ignore|ignored|skip|skipped|skipping|replaced|replace|placeholder|todo|tbd|tba|n/a|scratch|" \
              r"correction|oops|note to self|draft"
_META_START = re.compile(r"^\W*(?:" + _META_WORDS + r")\b", re.IGNORECASE)
# Anywhere: the unmistakable ones as whole words, and the softer ones only as a clause marker
# ("…; skip", "wait:"), so "worth the wait" or "a song you never skip" stay valid.
_META_ANYWHERE = re.compile(r"\b(?:placeholder|todo|tbd|replaced (?:below|above|with|by)|verified pick|"
                            r"album title|wrong title|wrong song|wrong artist|not a (?:real )?(?:song|track)|"
                            r"use (?:this|that|the next) instead)\b", re.IGNORECASE)
_META_CLAUSE = re.compile(r"(?:^|[.;:,!?(\[—–-])\s*(?:" + _META_WORDS + r")\s*(?:[.;:,!?)\]—–-]|$)",
                          re.IGNORECASE)


def is_meta_reason(reason: str) -> bool:
    """True when *reason* is empty, a placeholder, or the model's drafting/correction notes."""
    text = str(reason or "").strip()
    if not text:
        return True
    return bool(_META_START.search(text) or _META_ANYWHERE.search(text) or _META_CLAUSE.search(text))
