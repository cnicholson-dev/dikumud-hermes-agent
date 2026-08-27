"""RELAY-01 to RELAY-07: the relay refuses to be anything but itself."""
from __future__ import annotations

import time
from dataclasses import replace

import httpx2
import pytest

from conftest import (FALLBACK_ID, GOOD_RESPONSE, HANG, PRIMARY_ID, THIRD_ID,
                      chat)
from openrouter_relay.policy import UPSTREAM_CHAT_URL

KEY = "test-key-not-real"


# -- RELAY-01 fixed upstream --------------------------------------------

@pytest.mark.asyncio
async def test_only_ever_posts_to_the_one_fixed_url(call, stub):
    await call("/v1/chat/completions", body=chat())
    assert stub.urls == [UPSTREAM_CHAT_URL]


@pytest.mark.asyncio
@pytest.mark.parametrize("field", [
    "base_url", "api_base", "url", "endpoint", "host",
    "provider", "route", "models", "transforms", "fallbacks", "order",
])
async def test_routing_and_url_fields_are_refused(call, stub, field):
    res = await call("/v1/chat/completions",
                     body=chat(**{field: "https://evil.example/v1"}))
    assert res.status_code == 400
    assert res.json()["error"]["type"] in (
        "routing_override_refused", "unsupported_field")
    assert stub.urls == [], "nothing may be forwarded when routing is attempted"


@pytest.mark.asyncio
async def test_upstream_redirect_is_refused_not_followed(call, stub):
    stub.status = 302
    stub.raw_body = b""
    res = await call("/v1/chat/completions", body=chat())
    assert res.status_code == 502
    assert res.json()["error"]["type"] == "upstream_redirect_refused"


@pytest.mark.asyncio
async def test_the_client_is_constructed_without_redirect_following():
    """A redirect must not be able to move the upstream without a code change."""
    from openrouter_relay.policy import RelayPolicy
    from openrouter_relay.upstream import UpstreamClient

    c = UpstreamClient(KEY, RelayPolicy())
    try:
        assert c._client.follow_redirects is False  # noqa: SLF001
    finally:
        await c.aclose()


@pytest.mark.asyncio
async def test_there_is_no_route_that_accepts_a_target(app):
    paths = {r.path for r in app.routes}
    assert paths == {"/v1/chat/completions", "/v1/models", "/healthz", "/metrics"}
    # No path parameters at all: nothing can be addressed through this service.
    assert not any("{" in p for p in paths)


# -- RELAY-02 fixed model -----------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("attempt", [
    "openai/gpt-4o", "anthropic/claude-3-opus", "nvidia/other-model",
    "", None, 12345, "nvidia/nemotron-3-ultra-550b-a55b",
])
async def test_any_requested_model_is_replaced_by_policy(call, stub, attempt):
    await call("/v1/chat/completions", body=chat(model=attempt))
    assert stub.last_request["model"] == PRIMARY_ID


@pytest.mark.asyncio
async def test_a_response_naming_another_model_fails_closed(call, stub):
    stub.response = {**GOOD_RESPONSE, "model": "openai/gpt-4o"}
    res = await call("/v1/chat/completions", body=chat())
    assert res.status_code == 502
    assert res.json()["error"]["type"] == "upstream_model_mismatch"


@pytest.mark.asyncio
async def test_the_dated_canonical_slug_is_accepted(call, stub):
    # OpenRouter reports the dated slug for the :free alias; same pinned route.
    # Derived from whatever the primary is rather than written out, because the
    # rule is the prefix match, not one recorded slug. For the shipped default
    # the real value is nvidia/nemotron-3-ultra-550b-a55b-20260604, recorded in
    # public-documentation/DEPENDENCY_RECORD.md section 4.
    stub.response = {**GOOD_RESPONSE,
                     "model": PRIMARY_ID.split(":", 1)[0] + "-20260604"}
    res = await call("/v1/chat/completions", body=chat())
    assert res.status_code == 200


@pytest.mark.asyncio
async def test_models_catalog_lists_exactly_one_model(call, stub):
    res = await call("/v1/models", method="GET")
    data = res.json()["data"]
    assert len(data) == 1
    assert data[0]["id"] == PRIMARY_ID
    # Synthesised, not proxied: the catalog costs no outbound call.
    assert stub.urls == []


# -- RELAY-03 bounded fallback, and still fail-closed --------------------

@pytest.mark.asyncio
async def test_every_configured_model_failing_still_stops_closed(call, stub):
    """RELAY-03: still no *arbitrary* fallback.

    The relay may walk the configured order. It may not try a model outside it,
    another provider, or another URL, and when the order is exhausted it stops
    rather than degrading into something unpinned.
    """
    stub.status = 500
    stub.raw_body = b'{"error":{"message":"boom"}}'
    res = await call("/v1/chat/completions", body=chat())

    assert res.status_code == 502
    assert res.json()["error"]["stop_reason"] == "upstream_unavailable"
    # One attempt per ordered model, no more, all to the one pinned URL.
    assert stub.urls == [UPSTREAM_CHAT_URL] * 3
    assert stub.models_requested == [PRIMARY_ID, FALLBACK_ID, THIRD_ID]


@pytest.mark.asyncio
async def test_connection_failure_stops_closed(call, stub):
    stub.raise_exc = httpx2.ConnectError("no route")
    res = await call("/v1/chat/completions", body=chat())
    assert res.status_code == 502
    assert res.json()["error"]["type"] == "upstream_unreachable"


def test_no_model_identifier_exists_in_the_source():
    """The source names no model at all, of any vendor.

    The ids live in the configuration file, so a `vendor/model` string
    appearing in a .py file means a default crept back into code where an
    operator cannot see or change it.
    """
    import re
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "src" / "openrouter_relay"
    for path in sorted(src.glob("*.py")):
        text = path.read_text()
        for foreign in ("gpt-4", "claude-", "llama", "mistral", "gemini",
                        "openai/", "anthropic/", "deepseek", "qwen"):
            assert foreign not in text, \
                f"a model identifier appears in {path.name}: {foreign}"
        # Any vendor/model shape inside a string literal. Three strings in this
        # service legitimately contain a slash and are not model ids: the
        # pinned upstream URL and the two media types. They are removed before
        # looking rather than pattern-matched around, so the check stays a flat
        # "no slashed identifier" rather than a growing set of exceptions.
        stripped = text
        for allowed in ("https://openrouter.ai/api/v1/chat/completions",
                        "application/json", "text/event-stream"):
            stripped = stripped.replace(allowed, "")
        named = re.findall(r"[\"'][a-z0-9._-]+/[a-z0-9._-]+(?::free)?[\"']",
                           stripped)
        assert named == [], f"a model id is hardcoded in {path.name}: {named}"


def test_the_packaged_default_is_the_three_verified_nvidia_models():
    """Where "the shipped set" is asserted, since the ids left the source.

    All three were verified against the live catalog and endpoint records and
    all three passed the tool-calling check; see
    public-documentation/DEPENDENCY_RECORD.md section 4. The order is the one a
    fresh clone plays in.
    """
    import tomllib
    from pathlib import Path

    packaged = (Path(__file__).resolve().parents[1] / "src"
                / "openrouter_relay" / "models.default.toml")
    data = tomllib.loads(packaged.read_text(encoding="utf-8"))
    assert data["order"] == [
        "nvidia/nemotron-3-ultra-550b-a55b:free",
        "nvidia/nemotron-3-super-120b-a12b:free",
        "nvidia/nemotron-3.5-lightning:free",
    ]
    models = data["models"]
    assert set(models) == set(data["order"]), "nothing defined but unordered"
    assert models["nvidia/nemotron-3-ultra-550b-a55b:free"]["providers"] == ["nvidia"]
    assert models["nvidia/nemotron-3-super-120b-a12b:free"]["providers"] == ["nvidia"]
    # Qualified by quantisation, which the provider pattern has to allow.
    assert models["nvidia/nemotron-3.5-lightning:free"]["providers"] == ["nvidia/nvfp4"]


# -- The fallback, and its boundaries -----------------------------------

@pytest.mark.asyncio
async def test_a_provider_5xx_falls_back_to_the_second_pinned_model(call, stub):
    stub.script = [(503, b'{"error":{"message":"upstream is having a day"}}'),
                   (200, dict(GOOD_RESPONSE, model=FALLBACK_ID))]

    res = await call("/v1/chat/completions", body=chat())

    assert res.status_code == 200
    assert stub.models_requested == [PRIMARY_ID, FALLBACK_ID]


@pytest.mark.asyncio
async def test_an_unreachable_endpoint_falls_back(call, stub):
    stub.script = [(0, httpx2.ConnectError("no route")),
                   (200, dict(GOOD_RESPONSE, model=FALLBACK_ID))]
    res = await call("/v1/chat/completions", body=chat())
    assert res.status_code == 200
    assert stub.models_requested == [PRIMARY_ID, FALLBACK_ID]


@pytest.mark.asyncio
async def test_rate_limiting_falls_back_because_free_limits_are_per_model(call, stub):
    stub.script = [(429, b'{"error":{"message":"slow down"}}'),
                   (200, dict(GOOD_RESPONSE, model=FALLBACK_ID))]
    res = await call("/v1/chat/completions", body=chat())
    assert res.status_code == 200
    assert stub.models_requested == [PRIMARY_ID, FALLBACK_ID]


@pytest.mark.asyncio
@pytest.mark.parametrize("status,why", [
    (400, "our request is malformed and the other model will refuse it too"),
    (401, "the credential is wrong; retrying it elsewhere is pointless"),
    (403, "forbidden is not an availability problem"),
])
async def test_client_errors_do_not_fall_back(call, stub, status, why):
    stub.status = status
    stub.raw_body = b'{"error":{"message":"nope"}}'

    res = await call("/v1/chat/completions", body=chat())

    assert res.status_code == 502
    assert stub.models_requested == [PRIMARY_ID], why


@pytest.mark.asyncio
async def test_a_malformed_response_does_not_fall_back(call, stub):
    # A correctness problem, not an availability one: fail closed and be looked
    # at rather than quietly asking someone else the same question.
    stub.status = 200
    stub.raw_body = b"this is not json"
    res = await call("/v1/chat/completions", body=chat())
    assert res.status_code == 502
    assert stub.models_requested == [PRIMARY_ID]


@pytest.mark.asyncio
async def test_the_fallback_response_is_validated_like_any_other(call, stub):
    # The second attempt gets the same scrutiny as the first: a model that
    # answers as something else is refused whichever attempt it was.
    stub.script = [(500, b'{"error":{}}'),
                   (200, dict(GOOD_RESPONSE, model="nvidia/something-else"))]
    res = await call("/v1/chat/completions", body=chat())
    assert res.status_code == 502
    assert res.json()["error"]["type"] == "upstream_model_mismatch"


@pytest.mark.asyncio
async def test_a_caller_cannot_ask_for_the_fallback_model(call, stub):
    # Knowing the pair is not permission to pick from it. The field is
    # discarded and policy substitutes the primary, as RELAY-02 requires.
    res = await call("/v1/chat/completions",
                     body=chat(model=FALLBACK_ID))
    assert res.status_code == 200
    assert stub.models_requested == [PRIMARY_ID]


@pytest.mark.asyncio
async def test_the_primary_is_never_contacted_twice_on_success(call, stub):
    res = await call("/v1/chat/completions", body=chat())
    assert res.status_code == 200
    assert stub.models_requested == [PRIMARY_ID], "no speculative second call"


@pytest.mark.asyncio
async def test_each_attempt_pins_its_providers_and_forbids_others(call, stub):
    # Every attempt names the provider it will accept and forbids any other,
    # so the party that processes a request is fixed by policy rather than
    # chosen by OpenRouter at request time.
    stub.script = [(503, b'{"error":{}}'),
                   (200, dict(GOOD_RESPONSE, model=FALLBACK_ID))]

    await call("/v1/chat/completions", body=chat())

    first, second = stub.requests
    # The provider *tag*, which is what routing matches on. The display name
    # ("Nvidia") is accepted and matches nothing, producing a 404.
    expected = {"order": ["nvidia"], "allow_fallbacks": False}
    assert first["provider"] == expected
    assert second["provider"] == expected


@pytest.mark.asyncio
async def test_a_caller_cannot_choose_a_provider(call, stub):
    # `provider` is a routing field, so an inbound one is refused outright
    # rather than merged with or overriding the relay's own pin. RELAY-01
    # already covered this; it matters more now that the relay sends the field
    # itself, because "policy sets it" must not become "the caller can too".
    res = await call("/v1/chat/completions",
                     body=chat(provider={"order": ["SomewhereElse"]}))
    assert res.status_code == 400
    assert res.json()["error"]["type"] == "routing_override_refused"
    assert stub.requests == [], "nothing was forwarded"


@pytest.mark.asyncio
async def test_a_hanging_primary_yields_to_the_fallback_on_a_deadline(stub):
    # The regression that made the fallback useless in practice. A queued
    # free-tier request is not a dead socket: the upstream keeps the connection
    # alive, so the read timeout never fires and the attempt hangs past any
    # caller's patience. The bound has to be wall-clock. Measured live, a
    # 30-second socket timeout had not fired after three minutes.
    from openrouter_relay.app import build_app
    from openrouter_relay.policy import RelayPolicy

    policy = replace(RelayPolicy(), primary_timeout_seconds=0.2,
                     upstream_timeout_seconds=5.0)
    stub.script = [(0, HANG), (200, dict(GOOD_RESPONSE, model=FALLBACK_ID))]
    app = build_app(api_key="test-key-not-real", policy=policy, client=stub)

    started = time.monotonic()
    async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app),
            base_url="http://relay") as client:
        res = await client.post("/v1/chat/completions", json=chat(), timeout=10)
    elapsed = time.monotonic() - started

    assert res.status_code == 200
    assert stub.models_requested == [PRIMARY_ID, FALLBACK_ID]
    assert elapsed < 3.0, f"the deadline did not bound the attempt ({elapsed:.1f}s)"


@pytest.mark.asyncio
async def test_the_last_attempt_gets_the_full_timeout_not_the_short_one(stub):
    # The short deadline exists to reach the fallback quickly, not to cut the
    # fallback itself short: a slow second model is still a working one.
    from openrouter_relay.app import build_app
    from openrouter_relay.policy import RelayPolicy

    policy = replace(RelayPolicy(), primary_timeout_seconds=0.2,
                     upstream_timeout_seconds=5.0)
    # One hang per ordered model: every attempt but the last is cut at the
    # short deadline, and only the last is allowed the full one.
    stub.script = [(0, HANG)] * len(policy.models)
    app = build_app(api_key="test-key-not-real", policy=policy, client=stub)

    started = time.monotonic()
    async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app),
            base_url="http://relay") as client:
        res = await client.post("/v1/chat/completions", json=chat(), timeout=20)
    elapsed = time.monotonic() - started

    assert res.status_code == 502
    assert elapsed > 4.0, "the fallback was cut off at the primary's deadline"


# -- RELAY-04 request allowlist -----------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("field", [
    "response_format", "stop", "frequency_penalty", "presence_penalty",
    "logit_bias", "n", "logprobs", "functions", "function_call", "seed_value",
    # In the relay's ceiling, but not in the effective allowlist for the
    # shipped set: Lightning does not advertise it, so no configured model
    # receives it. A field that leaves the allowlist that way is
    # refused with a reason rather than dropped in silence.
    "reasoning_effort",
])
async def test_fields_the_endpoint_does_not_advertise_are_refused(call, stub, field):
    res = await call("/v1/chat/completions", body=chat(**{field: 1}))
    assert res.status_code == 400
    assert res.json()["error"]["type"] == "unsupported_field"
    assert stub.urls == []


@pytest.mark.asyncio
async def test_advertised_parameters_are_forwarded(call, stub):
    await call("/v1/chat/completions", body=chat(
        temperature=0.4, top_p=0.9, seed=7, tool_choice="auto",
        tools=[{"type": "function",
                "function": {"name": "f", "parameters": {"type": "object"}}}],
    ))
    sent = stub.last_request
    for name in ("temperature", "top_p", "seed", "tool_choice", "tools"):
        assert name in sent, name


@pytest.mark.asyncio
async def test_oversize_request_is_refused(call, stub, policy):
    huge = "x" * (policy.max_request_bytes + 1024)
    res = await call("/v1/chat/completions",
                     body=chat(messages=[{"role": "user", "content": huge}]))
    assert res.status_code == 413
    assert stub.urls == []


@pytest.mark.asyncio
async def test_malformed_json_is_refused(call, stub):
    res = await call("/v1/chat/completions", raw=b"{not json")
    assert res.status_code == 400
    assert res.json()["error"]["type"] == "invalid_json"
    assert stub.urls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("messages", [None, [], "hello", 42, [{"no_role": 1}]])
async def test_invalid_messages_are_refused(call, stub, messages):
    body = chat()
    body["messages"] = messages
    res = await call("/v1/chat/completions", body=body)
    assert res.status_code == 400
    assert stub.urls == []


@pytest.mark.asyncio
async def test_missing_messages_is_refused(call, stub):
    res = await call("/v1/chat/completions", body={"model": PRIMARY_ID})
    assert res.status_code == 400
    assert res.json()["error"]["type"] == "missing_field"


# -- streaming: accepted downstream, never used upstream -----------------

@pytest.mark.asyncio
async def test_a_streaming_request_is_served_as_sse(call, stub):
    res = await call("/v1/chat/completions", body=chat(stream=True))
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/event-stream")
    body = res.text
    assert body.startswith("data: ")
    assert body.rstrip().endswith("data: [DONE]")
    assert "You see a temple." in body


@pytest.mark.asyncio
async def test_the_upstream_call_streams_by_the_relay_s_own_decision(call, stub):
    """Streamed upstream, still validated whole, and never the caller's call.

    This inverts the assertion it replaces. The upstream call used to be
    non-streaming so the body could be validated at once; it now streams so
    reasoning reaches the spectator's feed while the model is still thinking,
    and the body is assembled and validated at once instead. The property that
    matters is unchanged and is tested next door: nothing is emitted to the
    caller before the whole answer has been validated.

    `stream_options` is asserted because the usage block rides on it, and
    app.py's token ledger reads usage after every success. Losing it would show
    up as a spectator reporting 0 tokens in and 0 out forever, with nothing
    else looking wrong.
    """
    await call("/v1/chat/completions", body=chat(stream=True))
    sent = stub.last_request
    assert sent["stream"] is True
    assert sent["stream_options"] == {"include_usage": True}
    assert stub.urls == [UPSTREAM_CHAT_URL]


@pytest.mark.asyncio
async def test_a_caller_asking_for_no_stream_still_gets_a_streamed_upstream(call, stub):
    """The caller cannot switch the operator's reasoning feed off.

    Same rule as the model id and the reasoning fields: what the caller may not
    decide is policy's to decide. A non-streamed upstream call would produce no
    deltas and an empty feed.
    """
    await call("/v1/chat/completions", body=chat())
    assert stub.last_request["stream"] is True


@pytest.mark.asyncio
async def test_a_malformed_response_still_fails_closed_when_streaming(call, stub):
    """Nothing is emitted before the body has been validated in full."""
    stub.response = {"choices": [{"no_message": True}]}
    res = await call("/v1/chat/completions", body=chat(stream=True))
    assert res.status_code == 502
    assert "text/event-stream" not in res.headers.get("content-type", "")
    assert res.json()["error"]["stop_reason"] == "upstream_unavailable"


@pytest.mark.asyncio
async def test_reasoning_is_stripped_before_streaming_too(call, stub):
    stub.response = {
        **GOOD_RESPONSE,
        "choices": [{
            "index": 0, "finish_reason": "stop",
            "message": {"role": "assistant", "content": "Blue",
                        "reasoning": "private thoughts"},
        }],
    }
    res = await call("/v1/chat/completions", body=chat(stream=True))
    assert res.status_code == 200
    assert "private thoughts" not in res.text
    assert "Blue" in res.text


@pytest.mark.asyncio
async def test_tool_calls_survive_the_sse_encoding(call, stub):
    stub.response = {
        **GOOD_RESPONSE,
        "choices": [{
            "index": 0, "finish_reason": "tool_calls",
            "message": {"role": "assistant", "content": None,
                        "tool_calls": [{"id": "c1", "type": "function",
                                        "function": {"name": "mud_act",
                                                     "arguments": "{}"}}]},
        }],
    }
    res = await call("/v1/chat/completions", body=chat(stream=True))
    assert res.status_code == 200
    assert "mud_act" in res.text
    assert '"index":0' in res.text.replace(" ", "")
    assert "tool_calls" in res.text


# -- RELAY-05 budgets ----------------------------------------------------

@pytest.mark.asyncio
async def test_max_tokens_is_clamped_not_forwarded_raw(call, stub, policy):
    await call("/v1/chat/completions", body=chat(max_tokens=65536))
    assert stub.last_request["max_tokens"] == policy.max_output_tokens


@pytest.mark.asyncio
async def test_a_default_output_bound_is_always_applied(call, stub, policy):
    await call("/v1/chat/completions", body=chat())
    assert stub.last_request["max_tokens"] == policy.max_output_tokens


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [0, -1, "many", 1.5, True, None])
async def test_invalid_max_tokens_is_refused(call, stub, value):
    res = await call("/v1/chat/completions", body=chat(max_tokens=value))
    assert res.status_code == 400
    assert stub.urls == []


@pytest.mark.asyncio
async def test_the_rate_limit_paces_rather_than_refuses(stub, monkeypatch):
    """A full window is waited for, not refused.

    Twenty a minute is OpenRouter's ceiling for free variants, so it cannot be
    raised, only waited for. Before this, a model fast enough to spend the
    window in forty-six seconds ended the session with a 429 that read like a
    fault. Now the fourth request arrives late instead of not at all.

    The window is shortened for the test rather than waiting a real minute.
    """
    from openrouter_relay.app import build_app
    from openrouter_relay.budgets import BudgetLedger
    from openrouter_relay.policy import RelayPolicy

    monkeypatch.setattr(BudgetLedger, "WINDOW_SECONDS", 0.3)
    tight = RelayPolicy(max_requests_per_minute=3, max_rate_wait_seconds=5.0)
    app = build_app(api_key=KEY, policy=tight, client=stub)
    transport = httpx2.ASGITransport(app=app)

    started = time.monotonic()
    async with httpx2.AsyncClient(transport=transport,
                                  base_url="http://relay") as client:
        codes = [(await client.post("/v1/chat/completions", json=chat())).status_code
                 for _ in range(5)]
    elapsed = time.monotonic() - started

    assert codes == [200] * 5, "every request is served, some of them later"
    assert len(stub.urls) == 5
    assert elapsed >= 0.3, "the fourth request waited for the window to refill"

    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),
                                  base_url="http://relay") as client:
        rate = (await client.get("/metrics")).json()["rate"]
    assert rate["waits"] >= 1, "and the waiting is counted rather than silent"
    assert rate["seconds"] > 0


@pytest.mark.asyncio
async def test_a_wait_longer_than_the_bound_still_refuses(stub, monkeypatch):
    """The bound is what keeps waiting from becoming a worse failure.

    A caller kept waiting past its own patience learns nothing from a reply
    that arrives after it has given up, so past the bound this refuses exactly
    as it did before.
    """
    from openrouter_relay.app import build_app
    from openrouter_relay.budgets import BudgetLedger
    from openrouter_relay.policy import RelayPolicy

    monkeypatch.setattr(BudgetLedger, "WINDOW_SECONDS", 30.0)
    tight = RelayPolicy(max_requests_per_minute=2, max_rate_wait_seconds=0.5)
    app = build_app(api_key=KEY, policy=tight, client=stub)
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport,
                                  base_url="http://relay") as client:
        codes = [(await client.post("/v1/chat/completions", json=chat())).status_code
                 for _ in range(3)]

    assert codes == [200, 200, 429]
    assert len(stub.urls) == 2, "the refused request never reaches the upstream"


@pytest.mark.asyncio
async def test_rate_waiting_disabled_refuses_immediately(stub):
    """RELAY_MAX_RATE_WAIT=0 refuses a full window instead of pacing it.

    A switch that cannot reproduce the unpaced result is not a switch.
    """
    from openrouter_relay.app import build_app
    from openrouter_relay.policy import RelayPolicy

    tight = RelayPolicy(max_requests_per_minute=3, max_rate_wait_seconds=0.0)
    app = build_app(api_key=KEY, policy=tight, client=stub)
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport,
                                  base_url="http://relay") as client:
        codes = [(await client.post("/v1/chat/completions", json=chat())).status_code
                 for _ in range(5)]
    assert codes[:3] == [200, 200, 200]
    assert codes[3:] == [429, 429]
    assert len(stub.urls) == 3, "throttled requests must not reach the upstream"


@pytest.mark.asyncio
async def test_a_refused_field_never_waits_for_a_slot(stub, monkeypatch):
    """Validation first: a request that will be refused should not queue.

    Waiting for a slot it is never going to use would turn a fast, correct
    refusal into a slow one and spend window capacity a valid request wanted.
    """
    from openrouter_relay.app import build_app
    from openrouter_relay.budgets import BudgetLedger
    from openrouter_relay.policy import RelayPolicy

    monkeypatch.setattr(BudgetLedger, "WINDOW_SECONDS", 30.0)
    tight = RelayPolicy(max_requests_per_minute=1, max_rate_wait_seconds=25.0)
    app = build_app(api_key=KEY, policy=tight, client=stub)
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport,
                                  base_url="http://relay") as client:
        await client.post("/v1/chat/completions", json=chat())  # fills it
        started = time.monotonic()
        res = await client.post("/v1/chat/completions",
                                json=chat(response_format={"type": "json"}))
        elapsed = time.monotonic() - started

    assert res.status_code == 400
    assert res.json()["error"]["type"] == "unsupported_field"
    assert elapsed < 1.0, "refused on its shape, without waiting for a slot"


@pytest.mark.asyncio
async def test_a_wait_cannot_outlive_the_session_budget(stub, monkeypatch):
    """The session clock is re-checked after waking.

    A wait can carry a request across the end of a session, and reserving a
    slot on an expired session is the one case where waiting bought a worse
    answer than refusing would have.
    """
    from openrouter_relay.budgets import BudgetLedger
    from openrouter_relay.errors import RelayError
    from openrouter_relay.policy import RelayPolicy

    monkeypatch.setattr(BudgetLedger, "WINDOW_SECONDS", 0.4)
    policy = RelayPolicy(max_requests_per_minute=1, max_rate_wait_seconds=5.0,
                         max_session_seconds=0.2)
    ledger = BudgetLedger(policy=policy)
    await ledger.check_and_reserve()

    with pytest.raises(RelayError) as err:
        await ledger.check_and_reserve()
    assert err.value.reason == "session_duration_exhausted"


@pytest.mark.asyncio
async def test_session_request_cap_is_enforced(stub):
    from openrouter_relay.app import build_app
    from openrouter_relay.policy import RelayPolicy

    tight = RelayPolicy(max_requests_per_session=2, max_requests_per_minute=100)
    app = build_app(api_key=KEY, policy=tight, client=stub)
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport,
                                  base_url="http://relay") as client:
        codes = [(await client.post("/v1/chat/completions", json=chat())).status_code
                 for _ in range(4)]
    assert codes == [200, 200, 429, 429]
    body = None
    async with httpx2.AsyncClient(transport=transport,
                                  base_url="http://relay") as client:
        body = (await client.post("/v1/chat/completions", json=chat())).json()
    assert body["error"]["type"] == "session_requests_exhausted"
    assert body["error"]["stop_reason"] == "budget_exhausted"


@pytest.mark.asyncio
async def test_session_duration_cap_is_enforced(stub):
    from openrouter_relay.app import build_app
    from openrouter_relay.policy import RelayPolicy

    expired = RelayPolicy(max_session_seconds=0.1)
    app = build_app(api_key=KEY, policy=expired, client=stub)
    import asyncio
    await asyncio.sleep(0.2)
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport,
                                  base_url="http://relay") as client:
        res = await client.post("/v1/chat/completions", json=chat())
    assert res.status_code == 429
    assert res.json()["error"]["type"] == "session_duration_exhausted"
    assert stub.urls == []


@pytest.mark.asyncio
async def test_upstream_timeout_is_configured(policy):
    from openrouter_relay.upstream import UpstreamClient
    c = UpstreamClient(KEY, policy)
    try:
        assert c._client.timeout is not None  # noqa: SLF001
    finally:
        await c.aclose()


# -- RELAY-06 key secrecy ------------------------------------------------

@pytest.mark.asyncio
async def test_the_key_is_sent_upstream_and_nowhere_else(call, stub):
    res = await call("/v1/chat/completions", body=chat())
    # It must reach the upstream...
    assert stub.headers[-1]["Authorization"] == f"Bearer {KEY}"
    # ...and appear in nothing the caller can see.
    assert KEY not in res.text
    assert "Authorization" not in res.text


@pytest.mark.asyncio
async def test_no_error_path_reveals_the_key(call, stub):
    probes = [
        (dict(provider="x"), 400),
        (dict(response_format={"type": "json"}), 400),
        (dict(models=["a", "b"]), 400),
    ]
    for overrides, expected in probes:
        res = await call("/v1/chat/completions", body=chat(**overrides))
        assert res.status_code == expected
        assert KEY not in res.text

    stub.status = 500
    stub.raw_body = b'{"error":{"message":"key sk-or-v1-LEAKED is over quota"}}'
    res = await call("/v1/chat/completions", body=chat())
    # Upstream text is dropped entirely, so anything it contained goes with it.
    assert "sk-or-v1-LEAKED" not in res.text
    assert "over quota" not in res.text


@pytest.mark.asyncio
async def test_metrics_and_health_carry_no_secrets(call, stub):
    await call("/v1/chat/completions", body=chat())
    for path in ("/metrics", "/healthz"):
        res = await call(path, method="GET")
        assert KEY not in res.text
        assert "Bearer" not in res.text
        assert "openrouter.ai" not in res.text


@pytest.mark.asyncio
async def test_metrics_label_outcomes_from_a_closed_set(call, stub):
    await call("/v1/chat/completions", body=chat())
    await call("/v1/chat/completions", body=chat(stream=True))
    outcomes = (await call("/metrics", method="GET")).json()["outcomes"]
    assert set(outcomes) <= {"ok", "streaming_unsupported", "rate_limited",
                             "unsupported_field", "routing_override_refused",
                             "invalid_json", "request_too_large",
                             "invalid_messages", "missing_field",
                             "invalid_max_tokens", "upstream_error",
                             "upstream_unreachable", "upstream_redirect_refused",
                             "upstream_rate_limited", "upstream_malformed_json",
                             "upstream_malformed_shape", "upstream_missing_choices",
                             "upstream_malformed_choice", "upstream_model_mismatch",
                             "upstream_response_too_large",
                             "session_requests_exhausted",
                             "session_duration_exhausted"}


def test_no_environment_variable_is_used_for_the_key():
    from pathlib import Path
    app_src = (Path(__file__).resolve().parents[1]
               / "src" / "openrouter_relay" / "app.py").read_text()
    # The key comes from a file, so it cannot be read out of /proc/1/environ
    # or docker inspect.
    assert "RELAY_API_KEY_FILE" in app_src
    assert 'environ.get("OPENROUTER_API_KEY"' not in app_src


# -- RELAY-07 response validation ---------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    "not an object",
    123,
    {},
    {"choices": []},
    {"choices": "nope"},
    {"choices": [{"no_message": 1}]},
    {"choices": [{"message": "not an object"}]},
    {"error": {"message": "upstream said no"}},
])
async def test_malformed_upstream_responses_fail_closed(call, stub, payload):
    stub.response = payload
    res = await call("/v1/chat/completions", body=chat())
    assert res.status_code == 502
    assert res.json()["error"]["stop_reason"] == "upstream_unavailable"


@pytest.mark.asyncio
async def test_non_json_upstream_body_fails_closed(call, stub):
    stub.raw_body = b"<html>gateway error</html>"
    res = await call("/v1/chat/completions", body=chat())
    assert res.status_code == 502
    assert res.json()["error"]["type"] == "upstream_malformed_json"


@pytest.mark.asyncio
async def test_oversize_upstream_response_fails_closed(call, stub, policy):
    stub.raw_body = b"x" * (policy.max_response_bytes + 10)
    res = await call("/v1/chat/completions", body=chat())
    assert res.status_code == 502
    assert res.json()["error"]["type"] == "upstream_response_too_large"


# -- reasoning is the model's default, and the caller's business either way --

@pytest.mark.asyncio
async def test_reasoning_is_left_to_the_model_on_every_outbound_request(call, stub):
    """Neither field is sent, which is what selects the model's own default.

    This assertion is the inverse of the one it replaces. Reasoning used to be
    switched off at the source; it is now the spectator's live view, so the
    relay says nothing about it and the model applies its default effort.
    Verified against all three configured models by
    scripts/verify-openrouter-reasoning-stream, which observed delta.reasoning
    arriving with neither field in the request.
    """
    await call("/v1/chat/completions", body=chat())
    sent = stub.last_request
    assert "reasoning" not in sent
    assert "include_reasoning" not in sent


@pytest.mark.asyncio
async def test_a_caller_cannot_influence_reasoning(call, stub):
    """Still policy's decision, only a different decision than before.

    A caller that could set these could switch the operator's view off, which
    is the same class of thing as choosing its own model.
    """
    await call("/v1/chat/completions",
               body=chat(reasoning={"exclude": True, "effort": "high"},
                         include_reasoning=True))
    sent = stub.last_request
    assert "reasoning" not in sent
    assert "include_reasoning" not in sent


@pytest.mark.asyncio
async def test_reasoning_returned_anyway_is_stripped_from_the_response(call, stub):
    stub.response = {
        **GOOD_RESPONSE,
        "choices": [{
            "index": 0,
            "finish_reason": "stop",
            "reasoning": "top level private thoughts",
            "message": {
                "role": "assistant",
                "content": "You see a temple.",
                "reasoning": "private thoughts",
                "reasoning_content": "more private thoughts",
                "reasoning_details": [{"text": "and more"}],
            },
        }],
    }
    res = await call("/v1/chat/completions", body=chat())
    assert res.status_code == 200
    text = res.text
    for leak in ("private thoughts", "more private thoughts", "and more",
                 "top level private thoughts"):
        assert leak not in text
    assert "You see a temple." in text


# -- tool calls survive --------------------------------------------------

@pytest.mark.asyncio
async def test_tool_calls_pass_through_intact(call, stub):
    stub.response = {
        **GOOD_RESPONSE,
        "choices": [{
            "index": 0,
            "finish_reason": "tool_calls",
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call_1", "type": "function",
                    "function": {"name": "mud_act",
                                 "arguments": '{"command":"look"}'},
                }],
            },
        }],
    }
    res = await call("/v1/chat/completions", body=chat())
    assert res.status_code == 200
    body = res.json()
    call_ = body["choices"][0]["message"]["tool_calls"][0]
    assert call_["function"]["name"] == "mud_act"
    assert body["choices"][0]["finish_reason"] == "tool_calls"


@pytest.mark.asyncio
async def test_reasoning_token_counts_are_kept_but_content_is_not(call, stub):
    """A count is a metric; the text is chain-of-thought.

    The thinking now reaches the spectator's feed, but it still must not reach
    the caller's reply, so the split this test describes is unchanged: the
    count survives in usage.completion_tokens_details.reasoning_tokens and the
    text does not survive in the message. That number is exactly the sort of
    token metric the design asks the spectator view to show.
    """
    stub.response = {
        **GOOD_RESPONSE,
        "usage": {
            "prompt_tokens": 280,
            "completion_tokens": 78,
            "completion_tokens_details": {"reasoning_tokens": 56},
        },
        "choices": [{
            "index": 0,
            "finish_reason": "stop",
            "message": {"role": "assistant", "content": "Blue",
                        "reasoning": "I considered several colours"},
        }],
    }
    res = await call("/v1/chat/completions", body=chat())
    body = res.json()

    # The count survives...
    assert body["usage"]["completion_tokens_details"]["reasoning_tokens"] == 56
    # ...the content does not.
    assert "I considered several colours" not in res.text
    assert "reasoning" not in body["choices"][0]["message"]


# -- the inference gauge, and the reasoning feed -------------------------

@pytest.mark.asyncio
async def test_metrics_reports_a_call_in_flight(app, call, stub):
    """The signal the spectator's indicator is lit from.

    Nothing else in the system can distinguish a model that is thinking from a
    relay that is idle, and the median call against these models measured 48.8
    seconds, so a watcher without this sees a frozen screen for most of a
    minute per turn with no way to tell it from a hang.
    """
    import asyncio

    gate = asyncio.Event()
    original = stub.stream

    def gated(method, url, json=None, headers=None, **kwargs):
        class _Gated:
            async def __aenter__(self):
                await gate.wait()
                self._inner = original(method, url, json=json, headers=headers)
                return await self._inner.__aenter__()

            async def __aexit__(self, *exc):
                return await self._inner.__aexit__(*exc)
        return _Gated()

    stub.stream = gated
    task = asyncio.create_task(call("/v1/chat/completions", body=chat()))

    # Bounded wait rather than a bare sleep(0): the request has to traverse the
    # ASGI transport before the gauge can possibly have opened.
    for _ in range(200):
        await asyncio.sleep(0.005)
        if app.state.metrics.snapshot()["inference"]["in_flight"]:
            break

    during = (await call("/metrics", method="GET")).json()["inference"]
    assert during["in_flight"] == 1
    assert during["seconds"] >= 0.0

    gate.set()
    await task

    after = (await call("/metrics", method="GET")).json()["inference"]
    assert after["in_flight"] == 0
    assert after["idle_seconds"] >= 0.0


@pytest.mark.asyncio
async def test_the_gauge_ignores_refused_requests(call, stub):
    """Pinned to the upstream call, not to the endpoint.

    A request refused by policy never reaches a model, and an indicator that
    lit for one would be reporting thinking that is not happening. This is also
    what keeps a rate-limit wait out of the gauge: both are refused or paced
    before begin_inference is reached.
    """
    res = await call("/v1/chat/completions", body=chat(top_k=5))
    assert res.status_code >= 400
    inference = (await call("/metrics", method="GET")).json()["inference"]
    assert inference["in_flight"] == 0
    assert inference["idle_seconds"] == 0.0


@pytest.mark.asyncio
async def test_reasoning_reaches_the_feed_and_never_the_caller(tmp_path, stub):
    """The whole boundary, in one test.

    The thinking goes to a file inside this container, where the operator's
    spectator reads it. The caller gets an answer with no reasoning in it,
    which is now the only thing keeping the model's own chain-of-thought out of
    its next turn's context.
    """
    from openrouter_relay.app import build_app
    from openrouter_relay.policy import RelayPolicy
    from openrouter_relay.reasoninglog import ReasoningLog

    feed = ReasoningLog(tmp_path / "reasoning.log")
    stub.response = {
        **GOOD_RESPONSE,
        "choices": [{
            "index": 0, "finish_reason": "stop",
            "message": {"role": "assistant", "content": "You see a temple.",
                        "reasoning": "the temple is north, so I should go north"},
        }],
    }
    app = build_app(api_key=KEY, policy=RelayPolicy(), client=stub,
                    reasoning_log=feed)
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport,
                                  base_url="http://relay") as client:
        res = await client.post("/v1/chat/completions", json=chat())

    assert res.status_code == 200
    assert "the temple is north" not in res.text
    assert "You see a temple." in res.text

    written = (tmp_path / "reasoning.log").read_text()
    assert "the temple is north, so I should go north" in written
    # The marker a reader uses to tell one call's thinking from the next.
    assert "--- call 1" in written
    assert PRIMARY_ID in written


@pytest.mark.asyncio
async def test_streamed_tool_call_fragments_are_reassembled(call, stub):
    """The game runs on tool calls, and they arrive in pieces.

    `function.arguments` is streamed as fragments that are only valid JSON once
    concatenated. Reassembling them wrongly would not look like a transport
    bug, it would look like an agent that had stopped being able to act.
    """
    import json as _json

    chunks = [
        {"id": "x", "model": PRIMARY_ID, "choices": [
            {"index": 0, "delta": {"role": "assistant", "tool_calls": [
                {"index": 0, "id": "call_1", "type": "function",
                 "function": {"name": "mud_act", "arguments": '{"comm'}}]}}]},
        {"id": "x", "model": PRIMARY_ID, "choices": [
            {"index": 0, "delta": {"tool_calls": [
                {"index": 0, "function": {"arguments": 'and":"look"}'}}]}}]},
        {"id": "x", "model": PRIMARY_ID, "choices": [
            {"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
    ]
    body = "".join(f"data: {_json.dumps(c)}\n\n" for c in chunks)
    stub.raw_body = (body + "data: [DONE]\n\n").encode()

    res = await call("/v1/chat/completions", body=chat())
    assert res.status_code == 200
    message = res.json()["choices"][0]["message"]
    assert message["tool_calls"][0]["function"]["name"] == "mud_act"
    assert _json.loads(message["tool_calls"][0]["function"]["arguments"]) == {
        "command": "look"}
    assert res.json()["choices"][0]["finish_reason"] == "tool_calls"


@pytest.mark.asyncio
async def test_usage_from_the_final_chunk_reaches_the_ledger(app, call, stub):
    """The one that fails silently if it is wrong.

    app.py records the token budget from the assembled payload's usage. A
    streamed path that dropped it would leave the ledger recording zeros for
    every request while everything else looked healthy, and the spectator's
    token line would sit at 0 in / 0 out forever.
    """
    await call("/v1/chat/completions", body=chat())
    budgets = (await call("/metrics", method="GET")).json()["budgets"]
    assert budgets["prompt_tokens"] == GOOD_RESPONSE["usage"]["prompt_tokens"]
    assert budgets["completion_tokens"] == GOOD_RESPONSE["usage"]["completion_tokens"]


@pytest.mark.asyncio
async def test_an_empty_stream_is_an_availability_failure(call, stub):
    """A 200 that opens a stream and says nothing.

    Observed from a free endpoint that was not actually serving: it answered
    200 with a single empty chunk and streamed normally on the next attempt. It
    is treated as unreachable so the fallback still fires, rather than as a
    malformed answer, which would fail the turn outright.
    """
    stub.raw_body = b""
    res = await call("/v1/chat/completions", body=chat())
    assert res.status_code == 502
    assert res.json()["error"]["stop_reason"] == "upstream_unavailable"
