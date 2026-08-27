# Copyright (C) 2026 Cody Nicholson
# SPDX-License-Identifier: LGPL-2.1-only
"""The operator's live view of what the game said.

Design section 10 asks a spectator surface to expose "Live cleaned DikuMUD
output" first, before intents, state or metrics. Nothing carried it. The audit
record holds reasons, digests and lengths rather than content, deliberately,
and the cleaned text otherwise lives only in the transport's in-memory
`OutputBuffer`, whose single route out is `mud_observe`. That route is
destructive: it takes from the buffer and settles the turn state, so a
spectator reading through it would consume the output the agent was about to
read.

This is the second sink. It is written where the transport has already cleaned
the stream, suppressed the echo and redacted the credential, so it inherits all
three rather than repeating them, and it is read by `scripts/spectate` with the
same `tail` it already uses on the audit record.

Two properties this file must keep:

* **No credential, ever.** Game text arrives here already redacted. Commands
  are written by the session layer, which only ever sees a command that passed
  the validator; the transport's `send_line` is not a write site, because that
  is also what types the character's password during login.
* **Bounded.** Unlike the audit record, which grows by one line per command,
  this grows at the speed the game talks. It rotates once at a byte cap and
  keeps a single previous generation, so a long session cannot fill the volume.

Unlike the audit record this is not evidence, and losing it costs nothing: it
is a view of a session, not a record of one.
"""

from __future__ import annotations

import os
from pathlib import Path

#: Bytes before the live file is rotated. Roughly an hour of busy play, which
#: is more scrollback than a spectator can use and small enough that the audit
#: volume is never at risk.
DEFAULT_MAX_BYTES = 512_000


class PlayLog:
    """Append-only, bounded, credential-free record of what the game said."""

    def __init__(self, path: Path | str,
                 max_bytes: int = DEFAULT_MAX_BYTES) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max_bytes
        self._fd = self._open()
        # Counted rather than stat'ed on every write: this is on the hot path
        # of the read loop, and the only writer is this process.
        self._written = self.path.stat().st_size

    def _open(self) -> int:
        # O_APPEND for the same reason the audit record uses it: every write
        # lands at the end, whatever else holds the file open.
        return os.open(self.path,
                       os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o640)

    def write(self, text: str) -> None:
        """Append game text exactly as the transport cleaned it."""
        if not text:
            return
        self._append(text.encode("utf-8", errors="replace"))

    def command(self, command: str) -> None:
        """Append the command that produced what follows.

        A feed of pure game output reads oddly, because a MUD transcript is a
        conversation and only one side of it would be here. The marker is
        written by the caller that holds a validated model command.

        Written bare, with no sigil of its own. DikuMUD's prompt is literally
        "> " and it arrives without a trailing newline, so the command lands
        directly after it and the feed reads as the session actually looked:

            > north
            The Temple Altar

        A sigil here would have produced "> > north". The prompt is always
        there to receive it, because the turn protocol only permits a command
        once a prompt has settled.
        """
        if not command:
            return
        self._append(f"{command}\n".encode("utf-8", errors="replace"))

    def _append(self, blob: bytes) -> None:
        os.write(self._fd, blob)
        self._written += len(blob)
        if self._written >= self.max_bytes:
            self._rotate()

    def _rotate(self) -> None:
        """Move the live file aside and start a new one.

        Rotation rather than truncation in place, because the reader tails this
        file: truncating under it would have it read into a hole, while a
        rename leaves the open descriptor writing to a file that is simply no
        longer the one being read.
        """
        os.close(self._fd)
        try:
            previous = self.path.with_suffix(self.path.suffix + ".1")
            os.replace(self.path, previous)
        except OSError:
            # A rotation that cannot happen must not stop the session. The cap
            # is a housekeeping rule, not a boundary.
            pass
        self._fd = self._open()
        self._written = 0

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None  # type: ignore[assignment]
