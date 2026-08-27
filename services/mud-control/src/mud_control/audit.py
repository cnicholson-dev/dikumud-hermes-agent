# Copyright (C) 2026 Cody Nicholson
# SPDX-License-Identifier: LGPL-2.1-only
"""Append-only audit record for the MCP boundary.

`SECURITY.md` lists the audit record as a protected asset that must be
"append-only from the runtime perspective" and "unavailable for agent
modification". The agent has no tool that can reach this file: the five MUD
tools expose no filesystem surface, and the audit volume is mounted only into
mud-control, never into hermes-player.

What is recorded: session lifecycle, every accepted command with its stated
intent, every rejected command with the reason it was refused, state
transitions, and transport faults.

What is never recorded, per SECURITY.md section 8 and the design's rule that
private reasoning is not exposed: credentials, the raw environment, host and
port, and any chain-of-thought. The intent field is the short user-visible
statement the design asks for, not reasoning.

Rejected commands are recorded by reason and length, not by content. Logging
the rejected string would copy hostile input verbatim into a file that a human
later reads, and the reason is what makes the event useful anyway.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

#: Fields that must never appear in an event, whatever a caller passes.
_FORBIDDEN = frozenset({
    "password", "passwd", "secret", "credential", "credentials", "token",
    "api_key", "apikey", "authorization", "auth", "host", "port", "env",
    "environ", "reasoning", "thought", "chain_of_thought",
})

#: Cap on any single recorded string, so one enormous field cannot be used to
#: bury earlier events or fill the volume.
_MAX_FIELD = 512


class AuditLog:
    """Line-delimited JSON, opened for append and flushed per event."""

    def __init__(self, path: Path | str, redactions: list[bytes] | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._redactions = redactions if redactions is not None else []
        # Opened append-only. O_APPEND makes every write land at the end even
        # if something else holds the file open, so an earlier event cannot be
        # overwritten by a later seek.
        self._fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o640)

    def register_secret(self, secret: str) -> None:
        """Add a value to scrub from every future event."""
        encoded = secret.encode("utf-8", errors="replace")
        if encoded and encoded not in self._redactions:
            self._redactions.append(encoded)

    def _scrub(self, value: object) -> object:
        if isinstance(value, str):
            out = value
            for secret in self._redactions:
                text = secret.decode("utf-8", errors="replace")
                if text and text in out:
                    out = out.replace(text, "<redacted>")
            return out[:_MAX_FIELD]
        if isinstance(value, (int, float, bool)) or value is None:
            return value
        return str(value)[:_MAX_FIELD]

    def record(self, event: str, **fields) -> dict:
        entry: dict = {
            "ts": round(time.time(), 3),
            "event": str(event)[:64],
        }
        for key, value in fields.items():
            lowered = key.lower()
            if lowered in _FORBIDDEN:
                # Refuse the field rather than scrubbing it, so a mistake at a
                # call site is visible in the record instead of silently
                # producing an empty-looking value.
                entry[key] = "<refused: forbidden field>"
                continue
            entry[key] = self._scrub(value)

        line = json.dumps(entry, separators=(",", ":"), sort_keys=True) + "\n"
        os.write(self._fd, line.encode("utf-8"))
        return entry

    def close(self) -> None:
        try:
            os.close(self._fd)
        except OSError:
            pass

    def __enter__(self) -> "AuditLog":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
