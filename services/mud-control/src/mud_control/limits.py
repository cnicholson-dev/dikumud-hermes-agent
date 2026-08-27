# Copyright (C) 2026 Cody Nicholson
# SPDX-License-Identifier: LGPL-2.1-only
"""Supervised session limits, and the stop reason a session ends with.

`public-documentation/TEST_PLAN.md` E2E-05 requires that quota, repetition,
timeout, disconnect and safety faults each produce an **explicit** stop reason,
and the design ends the play loop on "a manual stop, configured time/turn limit,
API quota condition, repeated failure to progress, unrecoverable disconnect,
ambiguous transport state, or safety fault".

The relay and the transport already cover three of those: the relay stops on its
own budgets, and the transport fails closed on a lost link or an unsettled
prompt. Three were not covered anywhere else:

* **Repetition and lack of progress.** A model looping on one command would keep
  going until the framework's iteration budget ran out, which is a different
  thing wearing the same coat.
* **A play session bounded in turns or wall-clock.** The relay's session limits
  bound the relay *process*, not a character's outing.
* **A recorded reason for a session ending.** `session_closed` on its own never
  said what closed it, so the spectator surface had nothing to show.

This module is that half. It counts, it decides, and it names the reason;
`session.py` enforces it by refusing `mud_act` once a limit trips, in code
rather than in prompt text. `SECURITY.md` section 1 is explicit that prompts are
supplementary rather than primary controls.

The counters live with the game session, so a `mud_disconnect` followed by a
`mud_connect` starts a fresh outing with a fresh budget. That is deliberate: an
operator ending and restarting a session is exactly the supervision the design
asks for, and it should not require restarting a service.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from enum import Enum


class StopReason(str, Enum):
    """Why a session stopped. Reported to the agent and to the audit."""

    TURN_LIMIT = "turn_limit"
    TIME_LIMIT = "time_limit"
    NO_PROGRESS = "no_progress"
    REJECTION_LOOP = "rejection_loop"
    OPERATOR = "operator_stop"
    #: Set by the caller when the link is gone or ambiguous; the transport
    #: decides this one, and it is named here so every stop shares a vocabulary.
    TRANSPORT = "transport_fault"


@dataclass(frozen=True, slots=True)
class SessionLimits:
    """Bounds for one play session. Trusted configuration only."""

    #: Commands per session. Generous: a session should end because the
    #: operator stopped it or the character did something interesting, not
    #: because it hit a wall mid-fight.
    max_turns: int = 400

    #: Wall-clock seconds. Two hours matches the relay's own session bound, so
    #: the two do not expire at surprisingly different times.
    max_seconds: float = 7200.0

    #: Identical consecutive commands before the session is judged stuck.
    #: Five, because four is a plausible run of "kill guard" in a real fight
    #: and six starts to look like an operator watching a loop and waiting.
    max_repeats: int = 5

    #: Consecutive refusals before stopping. A model that cannot get a command
    #: past the validator is not playing, and each attempt costs a model call.
    max_consecutive_rejections: int = 10

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "SessionLimits":
        env = os.environ if environ is None else environ
        # Defaults are read off an instance, not off the class: with
        # slots=True the class attribute is a member descriptor rather than
        # the default value, so type(default)(raw) would fail on any configured
        # value. Found by the test that configures one.
        fallback = cls()

        def number(name: str, default):
            raw = env.get(name)
            if raw is None or not raw.strip():
                return default
            return type(default)(raw)

        return cls(
            max_turns=number("MUD_CONTROL_MAX_TURNS", fallback.max_turns),
            max_seconds=number("MUD_CONTROL_MAX_SECONDS", fallback.max_seconds),
            max_repeats=number("MUD_CONTROL_MAX_REPEATS", fallback.max_repeats),
            max_consecutive_rejections=number(
                "MUD_CONTROL_MAX_REJECTIONS",
                fallback.max_consecutive_rejections),
        )


@dataclass(slots=True)
class SessionBudget:
    """Counts one session and decides when it is over."""

    limits: SessionLimits = field(default_factory=SessionLimits)
    started_at: float = field(default_factory=time.monotonic)
    turns: int = 0
    stopped: StopReason | None = None
    _last_command: str | None = None
    _repeats: int = 1
    _rejections: int = 0

    # -- queries --------------------------------------------------------

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    def stop(self, reason: StopReason) -> StopReason:
        """Stop the session, keeping the first reason.

        The first reason is the true one. A session that stopped for no
        progress and then hits its time limit while an operator reads the log
        did not stop because of the clock.
        """
        if self.stopped is None:
            self.stopped = reason
        return self.stopped

    def check_before(self) -> StopReason | None:
        """The limits that can trip without a command being sent."""
        if self.stopped is not None:
            return self.stopped
        if self.turns >= self.limits.max_turns:
            return self.stop(StopReason.TURN_LIMIT)
        if self.elapsed >= self.limits.max_seconds:
            return self.stop(StopReason.TIME_LIMIT)
        return None

    # -- accounting -----------------------------------------------------

    def record_accepted(self, command: str) -> StopReason | None:
        """Count a command that reached the game. Returns a stop, if any."""
        self.turns += 1
        self._rejections = 0

        if command == self._last_command:
            self._repeats += 1
        else:
            self._last_command = command
            self._repeats = 1

        if self._repeats >= self.limits.max_repeats:
            return self.stop(StopReason.NO_PROGRESS)
        return None

    def record_rejected(self) -> StopReason | None:
        """Count a refused command. Refusals do not consume a turn: the game
        never saw them, and counting them would let hostile input burn a
        session's budget."""
        self._rejections += 1
        if self._rejections >= self.limits.max_consecutive_rejections:
            return self.stop(StopReason.REJECTION_LOOP)
        return None

    # -- reporting ------------------------------------------------------

    def detail(self, reason: StopReason) -> str:
        """A sentence a human or a model can act on."""
        return {
            StopReason.TURN_LIMIT:
                f"this session reached its limit of {self.limits.max_turns} "
                "commands",
            StopReason.TIME_LIMIT:
                f"this session reached its time limit of "
                f"{self.limits.max_seconds:.0f} seconds",
            StopReason.NO_PROGRESS:
                f"the same command was sent {self.limits.max_repeats} times in "
                "a row, which is a loop rather than play",
            StopReason.REJECTION_LOOP:
                f"{self.limits.max_consecutive_rejections} commands in a row "
                "were refused",
            StopReason.OPERATOR: "an operator stopped this session",
            StopReason.TRANSPORT: "the link to the game was lost or ambiguous",
        }[reason]

    def summary(self) -> dict:
        """What the spectator surface shows when a session ends."""
        return {
            "turns": self.turns,
            "seconds": round(self.elapsed, 1),
            "stop_reason": self.stopped.value if self.stopped else None,
            "max_turns": self.limits.max_turns,
            "max_seconds": self.limits.max_seconds,
        }
