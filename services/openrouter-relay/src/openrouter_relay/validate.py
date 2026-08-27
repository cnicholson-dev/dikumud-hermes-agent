# Copyright (C) 2026 Cody Nicholson
# SPDX-License-Identifier: LGPL-2.1-only
"""Request validation against the immutable policy.

The relay accepts one request shape: a non-streaming chat completion for the
pinned primary model. Everything else is refused with a specific reason.

The rule followed here mirrors the MCP boundary: build the upstream request
from validated values only. The outgoing body is constructed field by field
from what passed inspection, never by mutating and forwarding the caller's
object, so a field that was not examined cannot ride along.
"""

from __future__ import annotations

from typing import Any

from .errors import policy_violation
from .policy import REQUIRED_PARAMETERS, ROUTING_PARAMETERS, RelayPolicy


def _reject_routing_attempts(body: dict[str, Any]) -> None:
    """RELAY-01 and RELAY-02: no caller may steer host, provider or model."""
    present = ROUTING_PARAMETERS & set(body)
    if present:
        raise policy_violation(
            "routing_override_refused",
            "This relay has one fixed upstream and one fixed model; "
            f"routing fields are not accepted: {', '.join(sorted(present))}",
        )


def _reject_unknown_fields(body: dict[str, Any], policy: RelayPolicy) -> None:
    """RELAY-04: an allowlist, so an unknown field is refused, not forwarded.

    The allowlist is the effective one, which is narrower than the relay's
    ceiling whenever a configured model advertises less than the rest (see
    `RelayPolicy.effective_parameters`). A field that leaves the allowlist
    that way becomes a refusal with a reason rather than something dropped in
    silence, which is the same treatment a field the relay never accepted gets.
    """
    known = (policy.effective_parameters | REQUIRED_PARAMETERS
             | {"model", "stream", "user", "stream_options"})
    unknown = set(body) - known
    if unknown:
        raise policy_violation(
            "unsupported_field",
            "This relay forwards only the parameters every configured model "
            f"advertises; unsupported: {', '.join(sorted(unknown))}",
        )


def _validate_messages(messages: Any) -> list[dict]:
    if not isinstance(messages, list) or not messages:
        raise policy_violation("invalid_messages",
                               "messages must be a non-empty list")
    for item in messages:
        if not isinstance(item, dict) or "role" not in item:
            raise policy_violation("invalid_messages",
                                   "each message must be an object with a role")
    return messages


def wants_stream(body: Any) -> bool:
    """True when the caller asked for SSE.

    Independent of how the upstream call is made; this only decides the wire
    format facing the client. The upstream half is streamed and reassembled in
    upstream.py, and the caller still receives a complete validated body either
    way. See sse.py for why.
    """
    return bool(isinstance(body, dict) and body.get("stream"))


def validate_request(body: Any, policy: RelayPolicy) -> dict[str, Any]:
    """Return the upstream request body, built only from validated values.

    'stream' is deliberately absent from the result. Whether the upstream call
    streams is upstream.py's decision and not the caller's: it sets the field
    itself, per attempt, so that a caller cannot switch off the reasoning feed
    by asking for a non-streamed call.
    """
    if not isinstance(body, dict):
        raise policy_violation("invalid_body", "request body must be a JSON object")

    _reject_routing_attempts(body)
    _reject_unknown_fields(body, policy)

    missing = REQUIRED_PARAMETERS - set(body)
    if missing:
        raise policy_violation("missing_field",
                               f"missing required field: {', '.join(sorted(missing))}")

    outgoing: dict[str, Any] = {
        # Policy, not caller input. Whatever model was asked for is discarded,
        # including the configured fallback: knowing the pair is not permission
        # to pick from it. upstream.py sets this again per attempt.
        "model": policy.model,
        "messages": _validate_messages(body["messages"]),
    }

    for name in sorted(policy.effective_parameters):
        if name not in body:
            continue
        value = body[name]
        if name == "max_tokens":
            value = _clamp_output_tokens(value, policy)
        outgoing[name] = value

    # Reasoning is left on, at the model's own default effort.
    #
    # This reverses what this block used to do. Reasoning was switched off at
    # the source because the design excluded chain-of-thought from every
    # surface; it is now the spectator's live view of what the agent is
    # thinking, written to a file inside this container by reasoninglog.py and
    # read by scripts/spectate. Stripping still happens in upstream.py, which
    # is no longer a second guard but the only thing keeping the model's own
    # reasoning out of the reply it receives back.
    #
    # Both fields are dropped rather than forwarded, for the same reason the
    # model id is discarded above: a caller that could set them could switch
    # the operator's view off, and what the caller may not decide is policy's
    # to decide. Omitting them is what selects the model's default, which
    # Phase 0 records as enabled for all three configured models and which
    # scripts/verify-openrouter-reasoning-stream confirmed against each of them
    # by observing delta.reasoning arrive with neither field sent.
    outgoing.pop("reasoning", None)
    outgoing.pop("include_reasoning", None)

    if "max_tokens" not in outgoing:
        outgoing["max_tokens"] = policy.max_output_tokens

    return outgoing


def _clamp_output_tokens(value: Any, policy: RelayPolicy) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise policy_violation("invalid_max_tokens",
                               "max_tokens must be a positive integer")
    # Clamped rather than refused: asking for too much is wasteful, not hostile.
    return min(value, policy.max_output_tokens)
