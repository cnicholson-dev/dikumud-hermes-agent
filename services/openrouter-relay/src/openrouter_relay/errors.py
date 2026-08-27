# Copyright (C) 2026 Cody Nicholson
# SPDX-License-Identifier: LGPL-2.1-only
"""Safe, explicit failures.

SECURITY.md requires the relay "redact headers and upstream errors" and that
the design's fail-closed rule produce "an explicit stop reason". Two things
follow.

Upstream error text is never forwarded. It is untrusted, it can quote request
material back, and OpenRouter's messages have carried account details. Callers
get a stable machine-readable reason and a short fixed sentence.

Every failure carries a stop_reason, so a caller never has to infer why the
session ended from an HTTP code alone.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Message that makes the pinned Hermes client fall back to non-streaming.
#:
#: agent/chat_completion_helpers.py treats an error as "streaming unsupported"
#: when the lowercased text contains both "stream" and "not supported", then
#: sets _disable_streaming for the rest of the session. The exact wording is
#: what makes that happen, so the relay can decline to stream without breaking
#: the agent. Changing it changes behaviour; do not reword it casually.
STREAMING_REFUSED_MESSAGE = (
    "Streaming is not supported by this relay; retry without stream."
)


@dataclass(frozen=True, slots=True)
class RelayError(Exception):
    """A refusal the caller may see. Carries no upstream text and no secrets."""

    status: int
    reason: str
    message: str
    stop_reason: str = ""

    def __str__(self) -> str:
        return f"{self.reason}: {self.message}"

    def to_body(self) -> dict:
        """OpenAI-shaped error body, which is what the client expects."""
        return {
            "error": {
                "message": self.message,
                "type": self.reason,
                "code": self.reason,
                "stop_reason": self.stop_reason or self.reason,
            }
        }


def policy_violation(reason: str, message: str) -> RelayError:
    return RelayError(400, reason, message, stop_reason="policy_violation")


def budget_exhausted(reason: str, message: str) -> RelayError:
    return RelayError(429, reason, message, stop_reason="budget_exhausted")


def upstream_failure(reason: str, message: str) -> RelayError:
    # 502: the failure is upstream, and the caller must not retry into a
    # different model, because there is no other model.
    return RelayError(502, reason, message, stop_reason="upstream_unavailable")


def streaming_refused() -> RelayError:
    return RelayError(400, "streaming_unsupported", STREAMING_REFUSED_MESSAGE,
                      stop_reason="streaming_unsupported")


class ModelConfigError(Exception):
    """The pinned model configuration could not be loaded.

    Deliberately not a RelayError: this is a startup failure, not a refusal a
    caller can see. Nothing is served when it is raised, so it has no status,
    no stop reason and no error body. Its message names the file and the
    problem, because the operator reading it is looking at container logs
    rather than at an HTTP response, and it never quotes file contents beyond
    the offending key.
    """

