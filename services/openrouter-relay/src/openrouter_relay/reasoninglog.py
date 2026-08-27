# Copyright (C) 2026 Cody Nicholson
# SPDX-License-Identifier: LGPL-2.1-only
"""The spectator's live view of what the model was thinking.

The relay holds no content anywhere else. `metrics.py` records counters and
durations precisely so that the endpoint on the agent's network can carry
nothing sensitive, and that stays true: this is a file, not an endpoint.

That distinction is the reason for the design. `scripts/spectate` reads
`/metrics` from inside `hermes-player`, so anything the spectator can reach
over HTTP, the agent can reach too. Reasoning served that way would hand the
agent its own chain-of-thought back on the next turn. A file inside this
container is reachable by `docker compose exec openrouter-relay` and by nothing
on the agent's network, which is exactly the asymmetry required: the operator
watches, the player does not read its own mind.

It lives on the tmpfs the relay already mounts at /tmp (compose.yaml), so it
needs no volume, no compose change, and never touches disk. It dies with the
container, which is right for a view of a session rather than a record of one.

Two properties this file must keep:

* **Bounded, and more tightly than the play log.** That one rotates at 512 kB
  onto a disk volume; this is RAM inside a 256 MB container, and reasoning is
  the bulk of a streamed response rather than a side channel. Measured against
  all three configured models, reasoning outnumbered content chunks by roughly
  ten to one (93 against 10, 390 against 19, 27 against 4). So the cap is
  smaller and the arithmetic is deliberate: at most `DEFAULT_MAX_BYTES` live
  plus one rotated generation.

* **A write here can never fail a model call.** The play log is written by the
  session that owns the game and an error there is the session's problem. This
  is written from inside the relay's request path, so a full tmpfs or a bad
  descriptor must degrade to "the spectator stops updating" and never to "the
  agent's turn failed". Every write is guarded and the log disables itself
  rather than propagating.

No redaction happens here, and none is needed. The relay never sees the game
credential: it is injected directly into mud-control and redacted from
observations before they are buffered, so it is not in the model's context and
the model cannot reason about it. The API credential is attached in
`upstream.py` to an outgoing header and never appears in a response.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

#: Bytes before the live file is rotated. Half the play log's cap, because this
#: sits on tmpfs inside the relay's 256 MB limit rather than on a volume. At
#: most this many live plus one rotated generation, so the ceiling is double.
#: Roughly forty turns of a talkative model, which is far more scrollback than
#: a panel showing the last screenful can use.
DEFAULT_MAX_BYTES = 256_000


class ReasoningLog:
    """Append-only, bounded record of the model's streamed reasoning."""

    def __init__(self, path: Path | str,
                 max_bytes: int = DEFAULT_MAX_BYTES) -> None:
        self.path = Path(path)
        self.max_bytes = max_bytes
        self._fd: int | None = None
        self._written = 0
        #: Set once a write has failed. The failure is not retried on every
        #: chunk of every subsequent call, because a tmpfs that filled once
        #: will fill again and the request path must not pay for it each time.
        self._disabled = False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._fd = self._open()
            self._written = self.path.stat().st_size
        except OSError:
            self._disabled = True

    def _open(self) -> int:
        # O_APPEND for the same reason the audit record and the play log use
        # it: every write lands at the end, whatever else holds the file open.
        return os.open(self.path,
                       os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o640)

    def open_call(self, sequence: int, model: str) -> None:
        """Mark the start of one model call.

        Without a marker the feed is one unbroken wall of thinking and a
        watcher cannot tell where the reasoning behind the last command stops.
        The spectator splits on this line to show the current call alone, so
        the format cannot change without changing what it displays.

        The model id is included because a different model may answer when the
        one ahead of it is unavailable, and "which model thought this"
        should not become a guess for exactly the reason the metrics endpoint
        already reports which one served.
        """
        when = time.strftime("%H:%M:%S", time.localtime())
        self._append(f"\n--- call {sequence}  {model}  {when} ---\n"
                     .encode("utf-8", errors="replace"))

    def write(self, text: str) -> None:
        """Append one reasoning delta, exactly as it arrived."""
        if not text:
            return
        self._append(text.encode("utf-8", errors="replace"))

    def close_call(self, reason: str = "") -> None:
        """Mark the end of one model call.

        A call that ended is visually distinct from one still streaming, which
        is what stops the panel from looking permanently mid-thought once the
        pulse has gone out.
        """
        self._append(f"\n--- end {reason} ---\n".encode("utf-8",
                                                       errors="replace")
                     if reason else b"\n--- end ---\n")

    def _append(self, blob: bytes) -> None:
        if self._disabled or self._fd is None:
            return
        try:
            os.write(self._fd, blob)
        except OSError:
            # See the module docstring: the spectator stops updating, the
            # agent's turn does not fail.
            self._disabled = True
            return
        self._written += len(blob)
        if self._written >= self.max_bytes:
            self._rotate()

    def _rotate(self) -> None:
        """Move the live file aside and start a new one.

        Rotation rather than truncation in place, for the reason playlog.py
        gives: the reader tails this file, and truncating under it would have
        it read into a hole, while a rename leaves the open descriptor writing
        to a file that is simply no longer the one being read.
        """
        try:
            if self._fd is not None:
                os.close(self._fd)
            previous = self.path.with_suffix(self.path.suffix + ".1")
            os.replace(self.path, previous)
            self._fd = self._open()
            self._written = 0
        except OSError:
            # A rotation that cannot happen must not stop a call. The cap is a
            # housekeeping rule, not a boundary.
            self._disabled = True

    def close(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
