"""Button emoji that Discord actually accepts.

Discord rejects plain text symbols such as "←" (U+2190): they are characters,
not emoji. Only these are valid on a button:

* a custom emoji reference, ``<:name:123…>`` / ``<a:name:123…>``
* a Unicode emoji — one with emoji presentation by default (✅, 🔎, …) or a
  text-presentation symbol followed by the variation selector U+FE0F (⬅️, ▶️)
"""

from __future__ import annotations

import re

CUSTOM_EMOJI = re.compile(r"<a?:[A-Za-z0-9_]{2,32}:\d{15,21}>")
VARIATION_SELECTOR = "\ufe0f"
ZERO_WIDTH_JOINER = "\u200d"
KEYCAP = "\u20e3"

# Symbols that Discord shows as emoji without a variation selector
# (Unicode "Emoji_Presentation=Yes" outside the pictograph planes).
DEFAULT_PRESENTATION = (
    (0x231A, 0x231B), (0x23E9, 0x23EC), (0x23F0, 0x23F0), (0x23F3, 0x23F3),
    (0x25FD, 0x25FE), (0x2614, 0x2615), (0x2648, 0x2653), (0x267F, 0x267F),
    (0x2693, 0x2693), (0x26A1, 0x26A1), (0x26AA, 0x26AB), (0x26BD, 0x26BE),
    (0x26C4, 0x26C5), (0x26CE, 0x26CE), (0x26D4, 0x26D4), (0x26EA, 0x26EA),
    (0x26F2, 0x26F3), (0x26F5, 0x26F5), (0x26FA, 0x26FA), (0x26FD, 0x26FD),
    (0x2705, 0x2705), (0x270A, 0x270B), (0x2728, 0x2728), (0x274C, 0x274C),
    (0x274E, 0x274E), (0x2753, 0x2755), (0x2757, 0x2757), (0x2795, 0x2797),
    (0x27B0, 0x27B0), (0x27BF, 0x27BF), (0x2B1B, 0x2B1C), (0x2B50, 0x2B50),
    (0x2B55, 0x2B55),
)


def _is_pictograph(char: str) -> bool:
    return ord(char) >= 0x1F000


def _has_default_presentation(char: str) -> bool:
    code = ord(char)
    return any(low <= code <= high for low, high in DEFAULT_PRESENTATION)


def is_valid_emoji(raw: str) -> bool:
    """True if Discord will accept ``raw`` as a button emoji."""
    text = (raw or "").strip()
    if not text:
        return False
    if CUSTOM_EMOJI.fullmatch(text):
        return True
    if len(text) > 16 or any(char.isspace() for char in text):
        return False
    if any(char.isalnum() for char in text) and KEYCAP not in text:
        return False  # letters/digits aren't emoji (keycaps like 1️⃣ are)
    core = [c for c in text if c not in (VARIATION_SELECTOR, KEYCAP, ZERO_WIDTH_JOINER)]
    if not core:
        return False
    if ZERO_WIDTH_JOINER not in text and len(core) > 1:
        return False  # one emoji per button, never a row of them
    if VARIATION_SELECTOR in text or KEYCAP in text:
        # e.g. ⬅️ (U+2B05 U+FE0F): the selector is what makes it an emoji
        return all(_is_pictograph(c) or ord(c) > 0x2000 for c in core)
    return all(_is_pictograph(char) or _has_default_presentation(char) for char in core)
