"""Okay is 👌 (U+1F44C) everywhere; the thumbs-up (U+1F44D) appears nowhere in the plugin."""

import subprocess
from pathlib import Path

from core import reactions as rx

ROOT = Path(__file__).resolve().parents[1]
THUMBS_UP = chr(0x1F44D)  # built from its code point so this file passes its own check


def _plugin_files():
    try:
        out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
        return [ROOT / f for f in out.split()]
    except (OSError, subprocess.CalledProcessError):  # not a git checkout (installed copy)
        return [p for p in ROOT.rglob("*") if p.is_file() and "__pycache__" not in p.parts and ".git" not in p.parts]


def test_no_thumbs_up_anywhere_in_the_plugin():
    offenders = []
    for path in _plugin_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError):
            continue
        if THUMBS_UP in text:
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []


def test_ok_hand_is_the_okay_reaction():
    assert rx.normalize_reaction("\U0001F44C") == "okay"
    for emoji, value in (("\U0001F44E", "down"), ("\u2764\ufe0f", "love")):
        assert rx.normalize_reaction(emoji) == value
