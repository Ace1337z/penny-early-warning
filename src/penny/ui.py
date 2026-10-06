"""Telegram presentation helpers: HTML escaping, inline/reply keyboards (6.12).

Kept separate from `telegram.py` (transport) and `commands.py` (dispatch) so the
markup is easy to read and to test.
"""

from __future__ import annotations

from html import escape as _escape
from typing import Iterable, Optional, Sequence


def h(value: object) -> str:
    """Escape text for Telegram's HTML parse mode."""
    return _escape(str(value), quote=False)


def bold(value: object) -> str:
    return f"<b>{h(value)}</b>"


def code(value: object) -> str:
    return f"<code>{h(value)}</code>"


def arrow_for(change_pct: Optional[float]) -> str:
    """A directional marker for a percentage change."""
    if change_pct is None:
        return "-"
    if change_pct > 0.05:
        return "\U0001F7E2"  # green circle
    if change_pct < -0.05:
        return "\U0001F534"  # red circle
    return "\u26AA"  # white circle


def bar(value: Optional[float], *, lo: float = 0.0, hi: float = 1.0, width: int = 8) -> str:
    """A tiny text meter, safe for a monospace line."""
    if value is None:
        return "\u2591" * width
    if hi <= lo:
        frac = 0.0
    else:
        frac = max(0.0, min(1.0, (value - lo) / (hi - lo)))
    filled = int(round(frac * width))
    return "\u2588" * filled + "\u2591" * (width - filled)


def divider(label: str = "") -> str:
    if not label:
        return "\u2500" * 18
    return f"\u2500\u2500 {h(label)} " + "\u2500" * max(1, 16 - len(label))


# --- keyboards --------------------------------------------------------------

def button(text: str, data: str) -> dict:
    return {"text": text, "callback_data": data}


def inline(rows: Sequence[Sequence[dict]]) -> Optional[dict]:
    """Build an InlineKeyboardMarkup, dropping empty rows/buttons."""
    clean = [[b for b in row if b] for row in rows]
    clean = [row for row in clean if row]
    if not clean:
        return None
    return {"inline_keyboard": clean}


def reply_keyboard(rows: Sequence[Sequence[str]], *,
                   placeholder: str = "Choose a command",
                   one_time: bool = False) -> dict:
    return {
        "keyboard": [[{"text": t} for t in row] for row in rows],
        "resize_keyboard": True,
        "is_persistent": not one_time,
        "input_field_placeholder": placeholder,
    }


def remove_keyboard() -> dict:
    return {"remove_keyboard": True}


# --- callback routing -------------------------------------------------------

# Callback data is a short, opaque action code plus an optional argument, so the
# 64-byte Telegram limit is never a concern.
def cb(action: str, arg: str = "") -> str:
    return f"{action}:{arg}" if arg else action


def parse_cb(data: str) -> tuple[str, str]:
    action, _, arg = str(data).partition(":")
    return action.strip(), arg.strip()
