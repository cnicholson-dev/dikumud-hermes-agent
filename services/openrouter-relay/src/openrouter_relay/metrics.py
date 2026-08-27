# Copyright (C) 2026 Cody Nicholson
# SPDX-License-Identifier: LGPL-2.1-only
"""Safe metrics.

The design asks the spectator surface to show "OpenRouter request, token,
latency, and error metrics". Those numbers must carry nothing sensitive, so
this module records counters and durations only. It never sees a prompt, a
completion, a header, or a key.

Error metrics are keyed by the relay's own reason strings, which are a closed
set defined in errors.py, rather than by upstream text. That keeps an
attacker-influenced or account-revealing string out of a label, which is the
usual way secrets leak into metrics.
"""

from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass, field


@dataclass(slots=True)
class Metrics:
    started_at: float = field(default_factory=time.time)
    latencies_ms: list[float] = field(default_factory=list)
    reasons: Counter = field(default_factory=Counter)
    #: Which pinned model answered, counted per model, plus how often the
    #: relay had to move down its order. Moving is allowed; the switch being
    #: invisible is not, because "which model played the game" is a claim this
    #: project makes out loud.
    models_served: Counter = field(default_factory=Counter)
    fallbacks: int = 0
    last_model: str = ""
    #: Time spent waiting for a rate-limit slot rather than refusing. Counted
    #: because a session that waits is a session that looks slow, and "the free
    #: tier is the ceiling" and "the model is slow" are different facts that
    #: would otherwise be reported identically.
    rate_waits: int = 0
    rate_wait_seconds: float = 0.0
    #: Monotonic start times of the calls currently open upstream. A list
    #: rather than a counter so the gauge can report how long the oldest has
    #: been running, which is the number the spectator's indicator shows. The
    #: median call against these models measured 48.8 seconds, so "a call is
    #: open, and for how long" is the difference between a watcher seeing a
    #: thinking agent and seeing a frozen screen.
    _open: list[float] = field(default_factory=list)
    #: When the last call finished, so an idle relay can say how long it has
    #: been idle rather than only that it is.
    last_completed_at: float = 0.0

    def begin_inference(self) -> float:
        """One call is now open upstream. Returns the token that closes it."""
        started = time.monotonic()
        self._open.append(started)
        return started

    def end_inference(self, started: float) -> None:
        """That call is done, however it ended."""
        try:
            self._open.remove(started)
        except ValueError:
            # Two calls opened in the same monotonic tick carry equal tokens,
            # and removing either is correct: they are interchangeable for the
            # min() below. A token that is absent entirely means the call was
            # already closed, which is not worth failing a request over.
            pass
        self.last_completed_at = time.monotonic()

    def record_latency(self, milliseconds: float) -> None:
        # Bounded so a long run cannot grow this without limit.
        self.latencies_ms.append(round(milliseconds, 1))
        if len(self.latencies_ms) > 1000:
            del self.latencies_ms[:500]

    def record_reason(self, reason: str) -> None:
        self.reasons[reason] += 1

    def record_served(self, model: str) -> None:
        """A request answered, and by which of the pinned models."""
        self.models_served[model] += 1
        self.last_model = model

    def record_rate_wait(self, seconds: float) -> None:
        """One request paced by the rolling window rather than refused."""
        self.rate_waits += 1
        self.rate_wait_seconds += max(0.0, seconds)

    def record_fallback(self, from_model: str, to_model: str,
                        reason: str) -> None:
        """The primary failed in a retryable way and the secondary was tried.

        The reason is one of the relay's own closed set, never upstream text,
        for the same reason the outcome counters are.
        """
        self.fallbacks += 1
        self.reasons[f"fallback:{reason}"] += 1

    def snapshot(self) -> dict:
        lat = sorted(self.latencies_ms)
        return {
            "uptime_seconds": round(time.time() - self.started_at, 1),
            "latency_ms": {
                "count": len(lat),
                "p50": lat[len(lat) // 2] if lat else 0.0,
                "p95": lat[int(len(lat) * 0.95)] if lat else 0.0,
                "max": lat[-1] if lat else 0.0,
            },
            # Closed set of relay reasons; never upstream text.
            "outcomes": dict(self.reasons),
            "models": {
                "served": dict(self.models_served),
                "last": self.last_model,
                "fallbacks": self.fallbacks,
            },
            "rate": {
                "waits": self.rate_waits,
                "seconds": round(self.rate_wait_seconds, 1),
            },
            # Counts and durations, like everything else here. What the model
            # is thinking goes to reasoninglog.py, which is a file inside this
            # container rather than an endpoint on the agent's network; this
            # says only that a call is open and for how long.
            "inference": {
                "in_flight": len(self._open),
                "seconds": (round(time.monotonic() - min(self._open), 1)
                            if self._open else 0.0),
                "idle_seconds": (
                    round(time.monotonic() - self.last_completed_at, 1)
                    if self.last_completed_at else 0.0),
            },
        }
