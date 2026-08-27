# Copyright (C) 2026 Cody Nicholson
# SPDX-License-Identifier: LGPL-2.1-only
"""Turn raw PTY bytes into clean game text.

Two jobs, kept separate because they fail differently:

1. Remove terminal control sequences that TinTin++ emits to paint a screen.
   The design requires stripping these before any observation is shown, while
   preserving ordinary game text "exactly enough for valid play" (PTY-03).

2. Separate TinTin++'s own status messages from game output. TinTin++ prints
   lines like "#SESSION 'diku' CONNECTED TO 'dikumud' PORT '4000'" onto the
   same stream as the game text. Those are transport events, not something the
   game said, and conflating them would let client chatter reach the model as
   if the world had produced it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# CSI: ESC [ ... final byte in @-~   (colour, cursor moves, mode toggles)
_CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
# OSC: ESC ] ... terminated by BEL or ST
_OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
# Two-character and charset-selection escapes, e.g. ESC =, ESC >, ESC ( B
_ESC_SHORT = re.compile(r"\x1b[()#][0-9A-Za-z]|\x1b[=>78MDEHc]")
# Anything else introduced by ESC that we did not recognise: drop the ESC only,
# so stray text is preserved rather than silently eaten.
_ESC_STRAY = re.compile(r"\x1b")
# Telnet IAC sequences. TinTin++ negotiates Telnet itself, so these should not
# reach us; fixture 15 (malformed Telnet) exists to prove we tolerate them.
_IAC = re.compile(
    # Subnegotiation. The SE terminator is optional on purpose: a truncated
    # SB with no SE is exactly fixture 15, and an earlier pattern that
    # required SE left the stray IAC SB bytes in the game text.
    rb"\xff\xfa[^\xff]*(?:\xff\xf0)?"
    rb"|\xff[\xfb-\xfe]."   # WILL / WONT / DO / DONT plus its option byte
    rb"|\xff\xff"           # an escaped 0xFF data byte
    rb"|\xff[\xf0-\xf9]"    # the remaining two-byte commands
    rb"|\xff$",             # a bare IAC at the end of a read
    re.S,
)

# Control characters that carry no meaning in game text. Tab and newline are
# deliberately absent: DikuMUD uses tabs for column layout in help tables.
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# TinTin++ status lines. Uppercase after the hash is what distinguishes them
# from a player typing "#something" that the game echoed back.
_TT_STATUS = re.compile(r"^#([A-Z][A-Z0-9 _:'-]*)(?::)?\s*(.*)$")


def strip_telnet(raw: bytes) -> bytes:
    """Remove Telnet negotiation sequences from raw bytes."""
    return _IAC.sub(b"", raw)


def strip_controls(text: str) -> str:
    """Remove terminal control sequences, preserving printable game text."""
    text = _CSI.sub("", text)
    text = _OSC.sub("", text)
    text = _ESC_SHORT.sub("", text)
    text = _ESC_STRAY.sub("", text)
    text = _CTRL.sub("", text)
    # A PTY reports CRLF; the game means one line break.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text


def decode(raw: bytes) -> str:
    """Decode raw PTY bytes to text without ever raising.

    latin-1 round-trips every byte, which matters because DikuMUD's stock data
    files contain high bytes (the authors' names carry Danish characters) and
    losing them would corrupt the credits the licence requires us to preserve.
    """
    return strip_telnet(raw).decode("latin-1", errors="replace")


@dataclass(slots=True)
class Cleaned:
    """The result of cleaning one chunk of PTY output."""

    game_text: str = ""
    events: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.game_text or self.events)


def clean(raw: bytes) -> Cleaned:
    """Clean raw PTY bytes into game text plus transport events."""
    text = strip_controls(decode(raw))
    game_lines: list[str] = []
    events: list[str] = []

    lines = text.split("\n")
    for index, line in enumerate(lines):
        match = _TT_STATUS.match(line.strip())
        if match:
            events.append(line.strip().lstrip("#").strip())
            continue
        # Keep the line, including empties, so blank lines in room
        # descriptions survive. The final element has no trailing newline and
        # is preserved as-is because it may be a prompt.
        game_lines.append(line)

    if not game_lines:
        return Cleaned(game_text="", events=events)

    game_text = "\n".join(game_lines)
    return Cleaned(game_text=game_text, events=events)
