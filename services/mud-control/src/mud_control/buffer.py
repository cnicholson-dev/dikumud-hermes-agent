# Copyright (C) 2026 Cody Nicholson
# SPDX-License-Identifier: LGPL-2.1-only
"""Bounded, ordered accumulation of unread game output.

The design requires that observations be bounded and that overflow be
deterministic (PTY-07), while unread output stays ordered under a memory cap
(SECURITY.md section 7). Those two pull against each other: a hard cap that
silently drops the newest text would make the model act on a stale world.

This keeps the OLDEST text when it must discard, because a MUD's newest output
is the part that matters, and it records that a discard happened rather than
hiding it. Nothing is ever reordered, and a discard is always visible in
`overflowed`, so a caller can report a gap instead of inventing continuity.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Bytes of unread game text held before older text is discarded.
DEFAULT_MAX_UNREAD = 256 * 1024
#: Characters returned by a single take(); the rest stays unread.
DEFAULT_CHUNK = 8 * 1024


@dataclass(slots=True)
class OutputBuffer:
    """Ordered unread game text with a deterministic overflow policy."""

    max_unread: int = DEFAULT_MAX_UNREAD
    _text: str = ""
    overflowed: bool = False
    discarded_chars: int = 0

    def append(self, text: str) -> None:
        if not text:
            return
        self._text += text
        excess = len(self._text) - self.max_unread
        if excess > 0:
            # Drop from the front: oldest first, newest always retained.
            self._text = self._text[excess:]
            self.overflowed = True
            self.discarded_chars += excess

    def take(self, limit: int = DEFAULT_CHUNK) -> str:
        """Remove and return up to `limit` characters from the front."""
        if limit <= 0:
            return ""
        out, self._text = self._text[:limit], self._text[limit:]
        return out

    def peek(self) -> str:
        return self._text

    def tail(self, chars: int) -> str:
        return self._text[-chars:] if chars > 0 else ""

    def clear(self) -> None:
        self._text = ""

    @property
    def unread(self) -> int:
        return len(self._text)

    def __bool__(self) -> bool:
        return bool(self._text)
