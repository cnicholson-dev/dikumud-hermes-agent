# Copyright (C) 2026 Cody Nicholson
# SPDX-License-Identifier: LGPL-2.1-only
"""The relay's HTTP surface: two endpoints, no forwarding.

POST /v1/chat/completions  the one inference path
GET  /v1/models            a synthesised single-model catalog
GET  /healthz              liveness, no secrets
GET  /metrics              safe counters for the spectator view

There is no route that takes a URL, a path fragment, or a host. Nothing here
forwards an arbitrary request. The catalog is built from policy rather than
fetched, so the only outbound call this service can make is the fixed chat
completion in upstream.py.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from . import egress
from .budgets import BudgetLedger
from .errors import ModelConfigError, RelayError
from .metrics import Metrics
from .policy import RelayPolicy, model_config_path, models_catalog
from .reasoninglog import DEFAULT_MAX_BYTES, ReasoningLog
from .upstream import UpstreamClient
from .sse import completion_to_sse
from .validate import validate_request, wants_stream


def _read_api_key() -> str:
    """Read the credential from a file, never from an environment variable.

    An environment variable is visible in `docker inspect`, in `/proc/1/environ`
    to anything in the namespace, and in crash dumps. A file lets the key be
    delivered as a Docker secret and read once at startup.
    """
    path = os.environ.get("RELAY_API_KEY_FILE", "/run/secrets/openrouter_api_key")
    try:
        key = Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        key = ""
    if not key:
        # Fail closed and loudly, but without naming the path contents.
        raise RuntimeError(
            "No OpenRouter credential available; set RELAY_API_KEY_FILE.")
    return key


def build_app(api_key: str | None = None, policy: RelayPolicy | None = None,
              client=None, reasoning_log: ReasoningLog | None = None) -> Starlette:
    policy = policy or RelayPolicy()
    ledger = BudgetLedger(policy=policy)
    metrics = Metrics()

    # The spectator's live view of what the model is thinking, written as the
    # deltas arrive. A file inside this container rather than an endpoint, and
    # deliberately so: scripts/spectate reads /metrics from inside
    # hermes-player, so anything this service serves over HTTP is readable by
    # the agent as well, and reasoning served that way would be handed straight
    # back to the player. It lives on the tmpfs compose already mounts at /tmp,
    # so it needs no volume and never touches disk. An empty path turns the
    # feed off for a deployment that does not want one.
    if reasoning_log is None:
        feed = os.environ.get("RELAY_REASONING_LOG", "/tmp/reasoning.log")
        reasoning_log = ReasoningLog(
            Path(feed),
            max_bytes=int(os.environ.get("RELAY_REASONING_LOG_BYTES",
                                         str(DEFAULT_MAX_BYTES))),
        ) if feed else None

    upstream = UpstreamClient(api_key or _read_api_key(), policy, client=client,
                              on_fallback=metrics.record_fallback,
                              reasoning_log=reasoning_log)

    def _fail(err: RelayError) -> JSONResponse:
        ledger.record_refusal()
        metrics.record_reason(err.reason)
        return JSONResponse(err.to_body(), status_code=err.status)

    async def chat_completions(request: Request) -> JSONResponse:
        raw = await request.body()
        if len(raw) > policy.max_request_bytes:
            return _fail(RelayError(413, "request_too_large",
                                    "Request body exceeds the configured limit.",
                                    stop_reason="policy_violation"))
        try:
            body = json.loads(raw) if raw else None
        except (ValueError, TypeError):
            return _fail(RelayError(400, "invalid_json",
                                    "Request body is not valid JSON.",
                                    stop_reason="policy_violation"))

        try:
            outgoing = validate_request(body, policy)
            stream_requested = wants_stream(body)
            # After validation on purpose: a request that is going to be
            # refused for an unsupported field should not first wait for a
            # rate-limit slot it will never use.
            waited = await ledger.check_and_reserve()
        except RelayError as err:
            return _fail(err)

        if waited:
            metrics.record_rate_wait(waited)

        started = time.perf_counter()
        # Opened here rather than around check_and_reserve above, because a
        # request waiting for a rate-limit slot is not a model
        # thinking, and the spectator's indicator would otherwise light up for
        # the free tier's pacing. That wait is already reported separately as
        # `rate`.
        token = metrics.begin_inference()
        try:
            payload = await upstream.complete(outgoing)
        except RelayError as err:
            metrics.record_latency((time.perf_counter() - started) * 1000)
            return _fail(err)
        finally:
            # However it ended. A gauge that only closed on success would stick
            # on after the first failure and report a call in flight forever.
            metrics.end_inference(token)

        metrics.record_latency((time.perf_counter() - started) * 1000)
        metrics.record_reason("ok")
        metrics.record_served(upstream.served_model)
        usage = payload.get("usage") or {}
        ledger.record_success(usage.get("prompt_tokens", 0) or 0,
                              usage.get("completion_tokens", 0) or 0)

        if stream_requested:
            # Already complete and already validated; emitted as SSE only
            # because that is the shape the client asked for.
            return StreamingResponse(
                completion_to_sse(payload),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache",
                         "X-Accel-Buffering": "no"},
            )
        return JSONResponse(payload)

    async def list_models(_request: Request) -> JSONResponse:
        # Synthesised from policy. No outbound call, and exactly one entry, so
        # the agent cannot learn that another model exists. The configured
        # fallback is never named here, whatever the file says.
        return JSONResponse(models_catalog(policy))

    async def healthz(_request: Request) -> JSONResponse:
        # The primary only, and deliberately. This endpoint sits on the agent's
        # network, so naming the pair here would hand over the very fact
        # /v1/models exists to conceal. The operator gets the pair on the
        # startup line instead, which the agent cannot read.
        return JSONResponse({"status": "ok", "model": policy.model})

    async def metrics_endpoint(_request: Request) -> JSONResponse:
        return JSONResponse({**metrics.snapshot(), "budgets": ledger.snapshot()})

    app = Starlette(routes=[
        Route("/v1/chat/completions", chat_completions, methods=["POST"]),
        Route("/v1/models", list_models, methods=["GET"]),
        Route("/healthz", healthz, methods=["GET"]),
        Route("/metrics", metrics_endpoint, methods=["GET"]),
    ])
    app.state.policy = policy
    app.state.ledger = ledger
    app.state.metrics = metrics
    app.state.upstream = upstream
    app.state.reasoning_log = reasoning_log
    return app


def main() -> None:
    import uvicorn

    # Before anything is served. A relay whose egress is unrestricted looks
    # identical to one whose egress is restricted, because the upstream URL is
    # pinned in policy either way, so the absence of SEC-07's firewall rules
    # would otherwise be invisible. See egress.py.
    print(egress.enforce(), flush=True)

    # Same argument for the model pair. A relay running on the packaged
    # default and one running on the operator's file behave identically until
    # the ids differ, so the ids and the file they came from are stated once,
    # here, where `docker compose logs openrouter-relay` shows them. This is
    # the operator's view of the pair; nothing on the agent's network names
    # anything but the primary.
    try:
        policy = RelayPolicy()
    except ModelConfigError as err:
        # Fail closed and loudly, exactly as a missing credential does. A relay
        # that started on a policy it could not read would be a relay whose pin
        # nobody could state.
        raise SystemExit(f"openrouter-relay: {err}") from None

    print(f"models: {model_config_path()} -> "
          f"{', '.join(m.id for m in policy.models)}", flush=True)

    uvicorn.run(
        build_app(policy=policy),
        host=os.environ.get("RELAY_BIND", "0.0.0.0"),
        port=int(os.environ.get("RELAY_PORT", "8080")),
        log_level=os.environ.get("RELAY_LOG_LEVEL", "warning"),
        # Access logs would record request lines; the bodies carry game state
        # and the headers carry the credential. Off by default.
        access_log=False,
    )


if __name__ == "__main__":
    main()
