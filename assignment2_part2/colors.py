"""ANSI color helpers for CLI output.

Disabled automatically when stdout is not a TTY or `NO_COLOR` is set; set
`FORCE_COLOR=1` to override (e.g. when piping into `less -R`). Tests run
under pytest capture, so `enabled()` returns False there — assertions
that look for plain `[hub->]` / `[skip]` substrings keep working.
"""

from __future__ import annotations

import hashlib
import os
import sys
import time

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"

RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
MAGENTA = "\033[35m"
CYAN = "\033[36m"
BRIGHT_RED = "\033[91m"
BRIGHT_GREEN = "\033[92m"
BRIGHT_YELLOW = "\033[93m"
BRIGHT_CYAN = "\033[96m"
BRIGHT_MAGENTA = "\033[95m"

_PALETTE = (CYAN, MAGENTA, GREEN, YELLOW, BLUE, BRIGHT_CYAN, BRIGHT_MAGENTA, BRIGHT_GREEN)
_FIXED = {
    "alice": CYAN,
    "alice-swe": CYAN,
    "bob": MAGENTA,
    "bob-swe": MAGENTA,
    "emil-user": BRIGHT_YELLOW,
    "runtime": DIM,
}


def enabled() -> bool:
    if os.environ.get("FORCE_COLOR"):
        return True
    if os.environ.get("NO_COLOR"):
        return False
    try:
        return sys.stdout.isatty()
    except Exception:
        return False


def paint(text: str, *codes: str) -> str:
    if not codes or not enabled():
        return text
    return f"{''.join(codes)}{text}{RESET}"


def agent_color(name: str) -> str:
    key = (name or "").lower().strip()
    if key in _FIXED:
        return _FIXED[key]
    digest = int(hashlib.sha1(key.encode("utf-8")).hexdigest(), 16)
    return _PALETTE[digest % len(_PALETTE)]


def agent_label(name: str) -> str:
    return paint(name, BOLD, agent_color(name))


def dim(text: str) -> str:
    return paint(text, DIM)


def ts() -> str:
    return dim(time.strftime("%H:%M"))
