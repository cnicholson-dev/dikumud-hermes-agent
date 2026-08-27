# Copyright (C) 2026 Cody Nicholson
# SPDX-License-Identifier: LGPL-2.1-only
"""The one outbound call, and the validation of what comes back.

Three rules this file exists to keep:

* The credential is attached here and nowhere else. It is never logged, never
  returned, and never placed in an error.
* Redirects are not followed. An upstream redirect is a way to reach a host the
  policy never approved, which would defeat the fixed-upstream rule with no
  code change on our side.
* The fallback path is closed and bounded. At most four configured models, one
  attempt each, in a fixed order, and only an availability failure moves to the
  next. There is no loop and no model identifier in this module: the order comes
  from policy, which reads it from the operator's configuration file. When the
  order is exhausted, the relay stops closed with a stop reason, which is the
  property that mattered when there was only one model and still does.
* Every attempt is bounded by wall-clock time, not only by socket timeouts. An
  unavailable free endpoint accepts the connection and holds it open with
  keep-alives instead of refusing, which resets a read timeout indefinitely.
* The call is streamed, and the answer is still validated whole. The stream
  exists so reasoning can reach the spectator's feed while the model is still
  thinking; nothing is forwarded to the caller incrementally. Chunks are
  accumulated here and `validate_response` runs against the assembled body, so
  there is still no possibility of having sent half an answer before finding
  the rest malformed.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx2

from .errors import RelayError, upstream_failure
from .policy import PinnedModel, RelayPolicy

#: Fields removed from every upstream response before it reaches a caller.
#: The design forbids exposing private reasoning; the model emits it by
#: default, so this is a second guard behind reasoning.exclude on the request.
_REASONING_FIELDS = ("reasoning", "reasoning_content", "reasoning_details")


class UpstreamClient:
    """Posts one fixed request shape to one fixed URL."""

    def __init__(self, api_key: str, policy: RelayPolicy,
                 client: "httpx2.AsyncClient | None" = None,
                 on_fallback=None, reasoning_log=None) -> None:
        self._key = api_key
        self._policy = policy
        self._served_model = policy.models[0].id
        self._fallbacks = 0
        #: Where reasoning deltas are written as they arrive, or None when the
        #: deployment has no spectator feed. Never returned to a caller and
        #: never part of an assembled payload; see _assemble.
        self._reasoning = reasoning_log
        #: Numbers the calls in the feed, so a reader can tell one call's
        #: thinking from the next. Counts attempts that actually thought, not
        #: requests, which is why it is incremented at the first delta.
        self._calls = 0
        #: Called as (from_model, to_model, reason) when a fallback happens, so
        #: the switch is counted and visible rather than silent. A relay that
        #: quietly served a different model would make the project's central
        #: claim untrue without anyone noticing.
        self._on_fallback = on_fallback
        self._client = client or httpx2.AsyncClient(
            timeout=policy.upstream_timeout_seconds,
            # Not negotiable: see the module docstring.
            follow_redirects=False,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    #: Failure classes worth asking the other pinned model about. Everything
    #: absent from this set fails closed on the first attempt, deliberately:
    #: a 400 means our request is malformed and the other model will reject it
    #: too, a 401 means the credential is wrong and retrying with a bad key on
    #: a second model is pointless, and a malformed or mismatched response is a
    #: correctness problem rather than an availability one.
    RETRYABLE = frozenset({
        "upstream_unreachable",
        "upstream_server_error",
        "upstream_rate_limited",
    })

    @property
    def served_model(self) -> str:
        """The model that answered the last successful request."""
        return self._served_model

    async def complete(self, body: dict[str, Any]) -> dict[str, Any]:
        """Try the pinned models in order, once each.

        One attempt per model, no loop and no third option. If the primary
        answers, the secondary is never contacted; if both fail, the caller
        sees the failure from the *last* attempt and the relay has still failed
        closed, which is the property RELAY-03 protects.
        """
        models = self._policy.attempt_order
        last: RelayError | None = None

        for index, model in enumerate(models):
            attempt = dict(body)
            attempt["model"] = model.id
            # Pin the provider as well as the model, so the party that
            # processes a request is named by policy rather than chosen
            # upstream. Both free variants have a single provider today; this
            # keeps that true rather than assuming it stays true. The list is
            # never empty: load_models refuses a model without provider tags,
            # because an empty order with allow_fallbacks false frees routing
            # instead of pinning it.
            attempt["provider"] = {
                "order": list(model.providers),
                "allow_fallbacks": False,
            }
            # The last attempt gets the full timeout; earlier ones get the
            # shorter one, so a hung primary cannot consume the caller's
            # patience before the fallback is even tried.
            is_last_model = index + 1 >= len(models)
            timeout = (self._policy.upstream_timeout_seconds if is_last_model
                       else min(self._policy.primary_timeout_seconds,
                                self._policy.upstream_timeout_seconds))
            try:
                payload = await self._attempt(attempt, model, timeout)
            except RelayError as err:
                last = err
                if is_last_model or err.reason not in self.RETRYABLE:
                    raise
                self._fallbacks += 1
                if self._on_fallback is not None:
                    self._on_fallback(model.id, models[index + 1].id,
                                      err.reason)
                continue
            self._served_model = model.id
            return payload

        raise last  # unreachable: the loop either returns or raises

    async def _attempt(self, body: dict[str, Any], model: PinnedModel,
                       timeout: float | None = None) -> dict[str, Any]:
        """One streamed call, assembled into one complete validated body.

        The stream exists for the spectator, not for the caller. Reasoning
        deltas are written to the feed the moment they arrive, which is the
        only way a watcher sees thinking during a call rather than after it,
        while the answer itself is accumulated and handed to
        `validate_response` whole. So every Phase 4 property survives: the
        shape check, the model check and the reasoning strip still run against
        a complete body, and nothing is forwarded incrementally.
        """
        attempt = dict(body)
        attempt["stream"] = True
        # Without this the final usage chunk is not established to arrive, and
        # app.py records the token budget from it. A streamed path that lost
        # usage would have the ledger recording zeros for every request while
        # looking perfectly healthy. Confirmed present with this option set
        # against all three configured models by
        # scripts/verify-openrouter-reasoning-stream.
        attempt["stream_options"] = {"include_usage": True}

        try:
            # The socket timeout is necessary but not sufficient. While a
            # free-tier request sits queued, the upstream keeps the connection
            # alive with periodic bytes, and every one of them resets the read
            # timeout. Measured against a queued primary, a 30-second client
            # timeout had still not fired after three minutes. Only a
            # wall-clock deadline bounds that, so each attempt gets one, and it
            # now bounds the whole stream rather than just the first byte.
            read = self._stream_attempt(attempt, model, timeout)
            return await asyncio.wait_for(read, timeout) if timeout \
                else await read
        except RelayError:
            # Already a decided outcome with its own reason. Re-raised as-is so
            # a status refusal is not reclassified as a transport failure,
            # which would change which failures are retried.
            raise
        except Exception:  # noqa: BLE001
            # The exception text can contain the request, so nothing about it
            # is recorded or forwarded to the caller.
            raise upstream_failure(
                "upstream_unreachable",
                "The model endpoint could not be reached.",
            ) from None

    async def _stream_attempt(self, body: dict[str, Any], model: PinnedModel,
                              timeout: float | None = None) -> dict[str, Any]:
        """Open the stream, check the status, then assemble what arrives."""
        async with self._client.stream(
            "POST",
            self._policy.upstream_url,
            json=body,
            headers={
                "Authorization": f"Bearer {self._key}",
                "Content-Type": "application/json",
            },
            **({"timeout": timeout} if timeout is not None else {}),
        ) as response:
            self._check_status(response)
            return await self._assemble(response, model)

    def _check_status(self, response) -> None:
        """The status refusals, before a single byte of body is read."""
        if response.is_redirect or 300 <= response.status_code < 400:
            raise upstream_failure(
                "upstream_redirect_refused",
                "The model endpoint attempted to redirect; refusing.",
            )

        if response.status_code == 429:
            raise upstream_failure(
                "upstream_rate_limited",
                "The model endpoint is rate limiting this key.",
            )

        if response.status_code >= 500:
            # The provider's problem. Separated from 4xx because this is the
            # class worth trying the next ordered model for, and
            # because "the endpoint is broken" and "our request is wrong" are
            # different facts that were previously reported identically.
            raise upstream_failure(
                "upstream_server_error",
                f"The model endpoint returned status {response.status_code}.",
            )

        if response.status_code >= 400:
            # Our request or our credential. Upstream text is deliberately
            # dropped; only the code survives.
            raise upstream_failure(
                "upstream_error",
                f"The model endpoint returned status {response.status_code}.",
            )

    async def _assemble(self, response, model: PinnedModel) -> dict[str, Any]:
        """Read the stream, feed the reasoning out, and rebuild one body.

        Two things leave this method by different doors. Reasoning goes to the
        spectator's feed as it arrives and is never put into the payload, so
        the caller cannot receive it even before `_strip_reasoning` runs. The
        answer is accumulated and returned whole, so `validate_response` sees
        exactly what it always saw.
        """
        received = 0
        chunks = 0
        opened = False
        head: dict[str, Any] = {}
        error: Any = None
        usage: Any = None
        # Keyed by choice index, because a stream may interleave them and the
        # order chunks arrive in is not the order choices are numbered.
        choices: dict[int, dict[str, Any]] = {}

        async for line in response.aiter_lines():
            # Counted before anything is parsed, so an endless or oversized
            # stream is cut on the same rule a large body always was. The
            # newline is added back because aiter_lines strips it.
            received += len(line) + 1
            if received > self._policy.max_response_bytes:
                raise upstream_failure(
                    "upstream_response_too_large",
                    "The model endpoint returned an oversized response.",
                )

            line = line.strip()
            if not line or not line.startswith("data:"):
                # Comments, keep-alives and blank separators. A body that is
                # not SSE at all produces only these, which is caught below by
                # the chunk count rather than here, line by line.
                continue
            data = line[len("data:"):].strip()
            if data == "[DONE]":
                break

            try:
                chunk = json.loads(data)
            except (ValueError, TypeError):
                raise upstream_failure(
                    "upstream_malformed_json",
                    "The model endpoint returned a response that is not JSON.",
                ) from None
            if not isinstance(chunk, dict):
                raise upstream_failure(
                    "upstream_malformed_shape",
                    "The model endpoint returned an unexpected shape.")

            chunks += 1
            for key in ("id", "created", "model", "object"):
                if key in chunk and key not in head:
                    head[key] = chunk[key]
            if chunk.get("usage"):
                usage = chunk["usage"]
            if chunk.get("error"):
                error = chunk["error"]

            for choice in chunk.get("choices") or []:
                # Checked here rather than only on the assembled body. An
                # assembler builds a well-formed message out of whatever it is
                # given, so validate_response's malformed-choice rule can never
                # fire once the pieces have been merged: by then the shape is
                # this method's, not the endpoint's. The rule has to apply to
                # the chunks or it does not apply at all.
                if not isinstance(choice, dict):
                    raise upstream_failure(
                        "upstream_malformed_choice",
                        "The model endpoint returned a malformed choice.")
                delta = choice.get("delta")
                if delta is None and "finish_reason" not in choice:
                    # Every real choice chunk carries one or the other.
                    raise upstream_failure(
                        "upstream_malformed_choice",
                        "The model endpoint returned a malformed choice.")
                if delta is not None and not isinstance(delta, dict):
                    raise upstream_failure(
                        "upstream_malformed_choice",
                        "The model endpoint returned a malformed choice.")

                index = choice.get("index", 0)
                slot = choices.setdefault(
                    index, {"content": [], "tool_calls": {},
                            "role": "assistant", "finish_reason": None})
                if choice.get("finish_reason"):
                    slot["finish_reason"] = choice["finish_reason"]

                if delta is None:
                    continue
                if delta.get("role"):
                    slot["role"] = delta["role"]

                # The feed, written before anything else in the chunk is
                # considered. This is the one line the whole feature exists
                # for: it happens while the call is still open.
                thought = delta.get("reasoning")
                if isinstance(thought, str) and thought and self._reasoning:
                    if not opened:
                        # Numbered at the first delta rather than at the start
                        # of the attempt, so a primary that failed before
                        # thinking does not leave an empty call in the feed.
                        self._calls += 1
                        self._reasoning.open_call(self._calls, model.id)
                        opened = True
                    self._reasoning.write(thought)

                # Deliberately not accumulated into the payload. delta.content
                # carries the key on almost every chunk with an empty string in
                # it (105 chunks, 10 with content, measured), so appending
                # unconditionally is harmless but joining must ignore the
                # empties, which "".join does.
                if isinstance(delta.get("content"), str):
                    slot["content"].append(delta["content"])
                self._merge_tool_calls(slot, delta.get("tool_calls"))

        if opened and self._reasoning:
            last = next((s["finish_reason"] for s in choices.values()
                         if s["finish_reason"]), "")
            self._reasoning.close_call(str(last or ""))

        if chunks == 0:
            if received == 0:
                # A 200 that opened a stream and said nothing. Observed from a
                # free endpoint that was not actually serving, so it is treated
                # as the availability failure it is and stays retryable rather
                # than failing the turn outright.
                raise upstream_failure(
                    "upstream_unreachable",
                    "The model endpoint opened a stream and sent nothing.")
            raise upstream_failure(
                "upstream_malformed_json",
                "The model endpoint returned a response that is not JSON.")

        payload: dict[str, Any] = dict(head)
        if error is not None:
            payload["error"] = error
        if usage is not None:
            payload["usage"] = usage
        payload["choices"] = [
            {
                "index": index,
                "finish_reason": slot["finish_reason"] or "stop",
                "message": self._message(slot),
            }
            for index, slot in sorted(choices.items())
        ]

        return validate_response(payload, expected_model=model.id)

    @staticmethod
    def _message(slot: dict[str, Any]) -> dict[str, Any]:
        """One assembled assistant message. No reasoning key is ever built."""
        message: dict[str, Any] = {
            "role": slot["role"],
            "content": "".join(slot["content"]),
        }
        if slot["tool_calls"]:
            message["tool_calls"] = [
                call for _, call in sorted(slot["tool_calls"].items())
            ]
            # A tool call and an empty string are not the same answer. The
            # client keys on content being absent, which is how a completed
            # response reports it.
            if not message["content"]:
                message["content"] = None
        return message

    @staticmethod
    def _merge_tool_calls(slot: dict[str, Any], deltas: Any) -> None:
        """Accumulate streamed tool calls by index.

        The whole game runs on tool calls, and they arrive in pieces: the name
        once, then `function.arguments` as a series of fragments that only
        become valid JSON when concatenated. Reassembling them wrongly would
        not look like a transport bug, it would look like an agent that had
        stopped being able to act.
        """
        if not isinstance(deltas, list):
            return
        for item in deltas:
            if not isinstance(item, dict):
                continue
            index = item.get("index", 0)
            call = slot["tool_calls"].setdefault(
                index, {"id": "", "type": "function",
                        "function": {"name": "", "arguments": ""}})
            if item.get("id"):
                call["id"] = item["id"]
            if item.get("type"):
                call["type"] = item["type"]
            function = item.get("function")
            if not isinstance(function, dict):
                continue
            if function.get("name"):
                call["function"]["name"] = function["name"]
            if isinstance(function.get("arguments"), str):
                call["function"]["arguments"] += function["arguments"]


def validate_response(payload: Any, expected_model: str) -> dict[str, Any]:
    """RELAY-07: an incompatible response fails closed rather than passing through."""
    if not isinstance(payload, dict):
        raise upstream_failure("upstream_malformed_shape",
                               "The model endpoint returned an unexpected shape.")

    if "error" in payload and "choices" not in payload:
        raise upstream_failure("upstream_error",
                               "The model endpoint reported an error.")

    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise upstream_failure("upstream_missing_choices",
                               "The model endpoint returned no choices.")

    for choice in choices:
        if not isinstance(choice, dict) or not isinstance(choice.get("message"), dict):
            raise upstream_failure(
                "upstream_malformed_choice",
                "The model endpoint returned a malformed choice.")

    served = payload.get("model")
    if isinstance(served, str) and served and not _model_matches(served, expected_model):
        # The request pinned the model, so a different one coming back means
        # the upstream rerouted. That must never be accepted silently: it is a
        # fallback this relay did not choose, arriving from the other
        # direction. The configured order says which models we may ask for; it
        # does not permit the upstream to substitute one we did not.
        raise upstream_failure(
            "upstream_model_mismatch",
            "The model endpoint served a different model than the pinned one.")

    return _strip_reasoning(payload)


def _model_matches(served: str, expected: str) -> bool:
    if served == expected:
        return True
    # OpenRouter reports the dated canonical slug for the :free alias; both
    # denote the same pinned route, recorded in the Phase 0 dependency record.
    base = expected.split(":", 1)[0]
    return served.startswith(base)


def _strip_reasoning(payload: dict[str, Any]) -> dict[str, Any]:
    for choice in payload.get("choices", []):
        message = choice.get("message")
        if isinstance(message, dict):
            for field in _REASONING_FIELDS:
                message.pop(field, None)
        for field in _REASONING_FIELDS:
            choice.pop(field, None)
    return payload
