# Copyright (C) 2026 Cody Nicholson
# SPDX-License-Identifier: LGPL-2.1-only
"""Serialise a fully validated completion as a Server-Sent Events stream.

Phase 4 refused streaming outright and required the client to fall back. That
was wrong, and Phase 5 proved it: the pinned Hermes client always attempts a
stream first, there is no configuration to stop it, and its fallback lives in a
handler the error did not reach, so the session aborted instead of retrying.

The premise behind the refusal was a false choice. The relay never had to
choose between streaming to the client and validating a complete body, because
the two ends are independent:

    upstream   streamed, then assembled into one complete body
    downstream SSE, if that is what the client asked for

Every Phase 4 property survives. The upstream call is still a single request
whose whole body is shape-checked, model-checked and stripped of reasoning
before anything is emitted. Nothing is forwarded incrementally, so there is no
possibility of having sent half an answer before discovering the rest is
malformed. What changes is only the wire format on the side facing Hermes.

The upstream half became streamed later, for the spectator's live reasoning
feed (`reasoninglog.py`), and it changed nothing here: `upstream.py` assembles
the chunks and validates the result before this module ever sees it, so what
arrives is still one complete, already-validated payload.
"""

from __future__ import annotations

import json
from typing import Any, Iterator


def completion_to_sse(payload: dict[str, Any]) -> Iterator[bytes]:
    """Yield an OpenAI-compatible SSE stream for one completed response.

    Emitted as a single content chunk followed by a finish chunk, because the
    answer is already complete by the time this runs. The client reassembles it
    exactly as it would a many-chunk stream.
    """
    base = {
        "id": payload.get("id", "chatcmpl-relay"),
        "object": "chat.completion.chunk",
        "created": payload.get("created", 0),
        "model": payload.get("model", ""),
    }

    for index, choice in enumerate(payload.get("choices", [])):
        message = choice.get("message") or {}
        delta: dict[str, Any] = {"role": message.get("role", "assistant")}
        if message.get("content") is not None:
            delta["content"] = message.get("content")
        if message.get("tool_calls"):
            # Tool calls carry an index in streaming form; the client keys its
            # accumulator on it.
            delta["tool_calls"] = [
                {**call, "index": position}
                for position, call in enumerate(message["tool_calls"])
            ]

        yield _event({**base, "choices": [
            {"index": choice.get("index", index), "delta": delta,
             "finish_reason": None},
        ]})

        yield _event({**base, "choices": [
            {"index": choice.get("index", index), "delta": {},
             "finish_reason": choice.get("finish_reason", "stop")},
        ]})

    if payload.get("usage"):
        # Usage is not part of a normal chunk, but clients that ask for it
        # tolerate a final chunk carrying it, and dropping it would lose the
        # token metrics the spectator view needs.
        yield _event({**base, "choices": [], "usage": payload["usage"]})

    yield b"data: [DONE]\n\n"


def _event(obj: dict[str, Any]) -> bytes:
    return b"data: " + json.dumps(obj, separators=(",", ":")).encode() + b"\n\n"
