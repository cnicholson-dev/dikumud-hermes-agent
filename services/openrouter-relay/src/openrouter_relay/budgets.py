# Copyright (C) 2026 Cody Nicholson
# SPDX-License-Identifier: LGPL-2.1-only
"""Request, rate and session budgets.

The design requires the relay to enforce "request-size, output-token,
request-rate, per-session request, and session-duration limits" and to "fail
closed and stop the play session cleanly" when a limit is reached.

Two of those are quotas and one is a rate, and they are treated
differently. A spent session budget is spent: refuse. A full rate window
refills by itself, so a request waits for it, within a bound, and only refuses
when the wait would outlast the caller's usefulness. The relay still enforces
the same ceiling either way; what changed is that reaching it slows a session
down instead of ending it.

"Session" is defined here as the lifetime of one relay process. The relay sits
below Hermes and has no view of a play session, so inventing a session
identifier from request content would be guessing, and accepting one from the
caller would let the caller reset its own budget. A relay restart is a
deliberate operator action, which is the right granularity for a bound whose
purpose is to keep an unattended run from burning quota indefinitely.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from typing import ClassVar

from .errors import budget_exhausted
from .policy import RelayPolicy


@dataclass(slots=True)
class BudgetLedger:
    """Tracks consumption for one relay session."""

    #: The rate window. A class attribute rather than a literal so a test can
    #: shorten it and exercise the waiting path without spending a real minute.
    WINDOW_SECONDS: ClassVar[float] = 60.0
    #: Added to a computed wait so the slot has certainly opened on waking.
    WAIT_MARGIN: ClassVar[float] = 0.05

    policy: RelayPolicy
    started_at: float = field(default_factory=time.monotonic)
    requests_total: int = 0
    requests_ok: int = 0
    requests_refused: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    _recent: deque[float] = field(default_factory=deque)

    async def check_and_reserve(self) -> float:
        """Reserve a slot, waiting for the rate window if it is full.

        Returns the seconds spent waiting, which the caller records so a
        throttled session is legible rather than merely slow.

        The two session budgets are quotas and still refuse immediately: no
        amount of waiting inside one session earns back a spent request count
        or a spent clock. The rolling minute is a rate, and a rate is a
        statement about pace, so a caller that can be paced is paced instead.
        """
        self._check_session_budgets()

        waited = 0.0
        wait = self._seconds_until_slot()
        if wait > 0.0:
            if wait > self.policy.max_rate_wait_seconds:
                # Longer than the bound, so refuse exactly as this always did.
                # A caller kept waiting past its own patience learns nothing
                # from a reply that arrives after it has given up.
                raise budget_exhausted(
                    "rate_limited",
                    "This relay is rate limited; retry shortly.",
                )
            await asyncio.sleep(wait)
            waited = wait
            # A wait can carry a request across the end of the session, and
            # reserving a slot on an expired session is the one case where
            # waiting bought a worse answer than refusing would have.
            self._check_session_budgets()
            self._prune(time.monotonic())

        self._recent.append(time.monotonic())
        self.requests_total += 1
        return waited

    def _check_session_budgets(self) -> None:
        """The quotas. Both refuse rather than wait, before and after a sleep."""
        if time.monotonic() - self.started_at >= self.policy.max_session_seconds:
            raise budget_exhausted(
                "session_duration_exhausted",
                "This relay session has reached its configured time limit.",
            )

        if self.requests_total >= self.policy.max_requests_per_session:
            raise budget_exhausted(
                "session_requests_exhausted",
                "This relay session has reached its configured request limit.",
            )

    def _prune(self, now: float) -> None:
        cutoff = now - self.WINDOW_SECONDS
        while self._recent and self._recent[0] < cutoff:
            self._recent.popleft()

    def _seconds_until_slot(self) -> float:
        """How long until the rolling window has room. Zero when it has room now.

        The window is full when it holds the whole allowance, and the oldest
        entry in it leaves the window WINDOW_SECONDS after it was made, so that
        is exactly how long a caller has to wait. The margin covers the
        difference between "the entry has aged out" and "the entry has aged out
        by the time we look again", which without it can leave the window still
        full on waking and turn one sleep into several.
        """
        now = time.monotonic()
        self._prune(now)
        if len(self._recent) < self.policy.max_requests_per_minute:
            return 0.0
        return (self._recent[0] + self.WINDOW_SECONDS) - now + self.WAIT_MARGIN

    def record_success(self, prompt: int, completion: int) -> None:
        self.requests_ok += 1
        self.prompt_tokens += max(0, prompt)
        self.completion_tokens += max(0, completion)

    def record_refusal(self) -> None:
        self.requests_refused += 1

    def snapshot(self) -> dict:
        return {
            "requests_total": self.requests_total,
            "requests_ok": self.requests_ok,
            "requests_refused": self.requests_refused,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "session_seconds": round(time.monotonic() - self.started_at, 1),
            "requests_remaining": max(
                0, self.policy.max_requests_per_session - self.requests_total),
            "seconds_remaining": max(
                0.0, round(self.policy.max_session_seconds
                           - (time.monotonic() - self.started_at), 1)),
        }
