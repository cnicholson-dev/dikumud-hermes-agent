# Copyright (C) 2026 Cody Nicholson
# SPDX-License-Identifier: LGPL-2.1-only
"""Typed transport state for the TinTin++ link.

This is the transport layer only. The READY/COMMAND_SENT/OBSERVING turn
protocol that governs the model belongs to the MCP boundary in Phase 3 and is
deliberately not implemented here: mixing the two would put gameplay policy
inside the byte transport, and the design keeps them apart.

What this file owns is whether the link is usable at all, and every way it can
stop being usable. `SECURITY.md` requires that timeout, prompt ambiguity,
malformed transport and disconnect all fail closed rather than be guessed at,
so each of those is a distinct terminal state, not a flag on a healthy one.
"""

from __future__ import annotations

from enum import Enum


class LinkState(str, Enum):
    """Where the transport is."""

    IDLE = "idle"                # constructed, TinTin++ not started
    STARTING = "starting"        # process up, session not yet connected
    CONNECTED = "connected"      # session established, traffic flowing
    AUTHENTICATING = "authenticating"  # credential injection in progress
    DISCONNECTED = "disconnected"      # link lost, cleanly observed
    FAULTED = "faulted"          # ambiguous or malformed; fail closed
    CLOSED = "closed"            # shut down deliberately


#: States from which no further traffic will flow without a reconnect.
TERMINAL = frozenset({LinkState.DISCONNECTED, LinkState.FAULTED, LinkState.CLOSED})

#: States in which sending input to the game is meaningful.
WRITABLE = frozenset({LinkState.CONNECTED, LinkState.AUTHENTICATING})


class TransportFault(Exception):
    """Raised when the transport cannot continue safely.

    Carrying the state means a caller never has to infer why the link died,
    which is what "reported explicitly" means in the design.
    """

    def __init__(self, message: str, state: LinkState = LinkState.FAULTED) -> None:
        super().__init__(message)
        self.state = state
