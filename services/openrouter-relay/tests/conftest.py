"""A controllable stub upstream.

Every negative path in RELAY-01..07 is exercised against this rather than
against OpenRouter, so the outcomes are deterministic and no test spends the
operator's quota. The live path is verified separately, once.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx2
import pytest

from openrouter_relay.app import build_app
from openrouter_relay.policy import RelayPolicy

#: Script sentinel: hold the connection open and never answer.
HANG = "::hang::"

#: The configured models, not literals. The tests assert
#: what the relay does with whatever it was configured with, so they read the
#: ids the same way the relay does. With RELAY_MODEL_CONFIG unset that is the
#: packaged default: three nvidia models, in order.
_DEFAULT_POLICY = RelayPolicy()
PRIMARY_ID = _DEFAULT_POLICY.models[0].id
FALLBACK_ID = _DEFAULT_POLICY.models[1].id
#: Third in the shipped order. Named so a test can assert the relay stops at
#: the end of the order rather than wrapping or inventing a fourth attempt.
THIRD_ID = _DEFAULT_POLICY.models[2].id

GOOD_RESPONSE = {
    "id": "gen-test",
    "model": PRIMARY_ID,
    "choices": [
        {
            "index": 0,
            "finish_reason": "stop",
            "message": {"role": "assistant", "content": "You see a temple."},
        }
    ],
    "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
}


class StubUpstream:
    """Records what the relay sent and returns what a test dictates."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.headers: list[dict[str, str]] = []
        self.urls: list[str] = []
        self.response: Any = GOOD_RESPONSE
        self.status: int = 200
        self.raise_exc: Exception | None = None
        self.raw_body: bytes | None = None
        #: Per-attempt scripting, for the two-model path: each entry is a
        #: (status, body) pair consumed in order, falling back to the single
        #: response above once exhausted. Needed because a fallback test has to
        #: make the first attempt fail and the second succeed.
        self.script: list[tuple[int, Any]] = []

    async def _decide(self, url, json, headers):
        """Record the attempt and return the (status, payload) it should get.

        Shared by both transports so a test scripts one thing and gets the same
        decision whichever way the relay calls it.
        """
        self.urls.append(str(url))
        self.requests.append(json or {})
        self.headers.append(dict(headers or {}))
        if self.script:
            status, payload = self.script.pop(0)
            if isinstance(payload, Exception):
                raise payload
            if payload == HANG:
                # A connection that stays open and never answers. This is what
                # a queued free-tier request actually looks like, and why the
                # socket read timeout alone does not bound an attempt.
                await asyncio.sleep(3600)
            return status, payload
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.status, (self.raw_body if self.raw_body is not None
                             else self.response)

    async def post(self, url, json=None, headers=None, **kwargs):  # noqa: A002
        status, payload = await self._decide(url, json, headers)
        content = (payload if isinstance(payload, bytes)
                   else __import__("json").dumps(payload).encode())
        return httpx2.Response(
            status_code=status, content=content,
            request=httpx2.Request("POST", str(url)))

    def stream(self, method, url, json=None, headers=None, **kwargs):  # noqa: A002
        """The transport the relay actually uses now.

        Returns an async context manager, like httpx's. Raw bytes set by a test
        are streamed verbatim, so the malformed and oversize cases still
        exercise the same failures they always did; a dict is turned into the
        SSE a real endpoint would have sent, reasoning included, so the
        response-side guards are still tested against reasoning that is really
        there rather than against a stream that never carried any.
        """
        return _StubStream(self, url, json, headers)

    @property
    def models_requested(self) -> list[str]:
        """Which model each attempt asked for, in order."""
        return [r.get("model") for r in self.requests]

    async def aclose(self) -> None:
        return None

    @property
    def last_request(self) -> dict[str, Any]:
        return self.requests[-1] if self.requests else {}


class _StubStreamResponse:
    """The subset of a streamed httpx response the relay touches."""

    def __init__(self, status_code: int, lines: list[str]) -> None:
        self.status_code = status_code
        self._lines = lines

    @property
    def is_redirect(self) -> bool:
        return 300 <= self.status_code < 400

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _StubStream:
    """Async context manager mirroring httpx's client.stream()."""

    def __init__(self, stub: StubUpstream, url, json, headers) -> None:
        self._stub = stub
        self._url = url
        self._json = json
        self._headers = headers

    async def __aenter__(self) -> _StubStreamResponse:
        status, payload = await self._stub._decide(  # noqa: SLF001
            self._url, self._json, self._headers)
        if isinstance(payload, bytes):
            # Verbatim, so "not JSON" and "too large" stay the same tests they
            # were before the transport changed.
            text = payload.decode("utf-8", errors="replace")
            return _StubStreamResponse(status, text.splitlines() or [""])
        return _StubStreamResponse(status, _to_sse(payload))

    async def __aexit__(self, *exc) -> None:
        return None


def _to_sse(payload: Any) -> list[str]:
    """One complete response, rendered as the chunks an endpoint would send.

    Deliberately not sse.completion_to_sse: that one is the relay's outbound
    converter and drops reasoning by design. A stub that used it could never
    deliver reasoning into the assembler, and the tests that check reasoning
    does not reach the caller would be passing on an empty stream.
    """
    import json as _json

    if not isinstance(payload, dict):
        return [f"data: {_json.dumps(payload)}", "data: [DONE]"]

    base = {
        "id": payload.get("id", "chatcmpl-stub"),
        "object": "chat.completion.chunk",
        "created": payload.get("created", 0),
        "model": payload.get("model", ""),
    }
    lines: list[str] = []

    def emit(obj: dict) -> None:
        lines.append("data: " + _json.dumps(obj, separators=(",", ":")))

    if payload.get("error"):
        emit({**base, "choices": [], "error": payload["error"]})

    for index, choice in enumerate(payload.get("choices", [])):
        if not isinstance(choice, dict):
            emit({**base, "choices": [choice]})
            continue
        message = choice.get("message")
        if "message" in choice and not isinstance(message, dict):
            # A choice whose message is the wrong type is one of the malformed
            # shapes the relay must refuse. Emitted as-is rather than coerced,
            # or the stub would quietly repair the very thing under test.
            emit({**base, "choices": [choice]})
            continue
        message = message if isinstance(message, dict) else {}
        position = choice.get("index", index)

        # Reasoning first and on its own chunks, the way the real stream sends
        # it: many chunks of thinking, a handful of content at the end.
        for field in ("reasoning", "reasoning_content"):
            if isinstance(message.get(field), str) and message[field]:
                emit({**base, "choices": [{
                    "index": position,
                    "delta": {"role": "assistant", "content": "",
                              field: message[field]},
                    "finish_reason": None}]})
        if message.get("reasoning_details"):
            emit({**base, "choices": [{
                "index": position,
                "delta": {"role": "assistant", "content": "",
                          "reasoning_details": message["reasoning_details"]},
                "finish_reason": None}]})
        if choice.get("reasoning"):
            emit({**base, "choices": [{
                "index": position, "reasoning": choice["reasoning"],
                "delta": {"role": "assistant", "content": ""},
                "finish_reason": None}]})

        delta: dict[str, Any] = {"role": message.get("role", "assistant")}
        if message.get("content") is not None:
            delta["content"] = message["content"]
        if message.get("tool_calls"):
            delta["tool_calls"] = [
                {**call, "index": slot}
                for slot, call in enumerate(message["tool_calls"])
            ]
        if "message" not in choice:
            # A choice with no message at all, which is one of the malformed
            # shapes the relay must refuse. Passed through as-is so the
            # assembler builds exactly that and validate_response decides.
            emit({**base, "choices": [choice]})
            continue
        emit({**base, "choices": [
            {"index": position, "delta": delta, "finish_reason": None}]})
        emit({**base, "choices": [
            {"index": position, "delta": {},
             "finish_reason": choice.get("finish_reason", "stop")}]})

    if payload.get("usage"):
        emit({**base, "choices": [], "usage": payload["usage"]})
    lines.append("data: [DONE]")
    return lines


@pytest.fixture
def stub() -> StubUpstream:
    return StubUpstream()


@pytest.fixture
def policy() -> RelayPolicy:
    return RelayPolicy()


@pytest.fixture
def app(stub, policy):
    return build_app(api_key="test-key-not-real", policy=policy, client=stub)


@pytest.fixture
def call(app):
    """Call the relay through a real ASGI transport, as Hermes would."""
    async def _call(path: str, method: str = "POST", body: Any = None,
                    raw: bytes | None = None):
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport,
                                      base_url="http://relay") as client:
            if method == "GET":
                return await client.get(path)
            if raw is not None:
                return await client.post(path, content=raw)
            return await client.post(path, json=body)
    return _call


def chat(**overrides) -> dict:
    """A minimal valid chat request, with overrides for hostile variants."""
    body = {
        "model": PRIMARY_ID,
        "messages": [{"role": "user", "content": "You are in a temple."}],
    }
    body.update(overrides)
    return body
