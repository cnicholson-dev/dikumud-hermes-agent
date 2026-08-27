# Copyright (C) 2026 Cody Nicholson
# SPDX-License-Identifier: LGPL-2.1-only
"""Immutable relay policy.

Everything here is trusted server-side configuration. Nothing in this module is
influenced by a request. The design states the relay "sends requests only to
the configured OpenRouter API host and exact model identifier" and "cannot
operate as a general proxy", so the upstream URL is a constant rather than
anything assembled from caller input.

The models are the one part of this policy an operator sets, and they are set in
a file rather than here. The identifiers live in trusted relay configuration.
What makes the pin worth having is not that the ids are Python literals, nor how
many of them there are: it is that they are fixed before the relay starts, that
a caller cannot choose among them, and that nothing the agent can reach may
change them. `load_models` is where that is enforced.

The file names a set of verified models and an order over them. Anything
defined but not ordered is configured and unused, which is how a model is
parked rather than deleted, and the order is what `scripts/set-model-order`
rewrites.

THE PARAMETER ALLOWLIST

`ALLOWED_PARAMETERS` below is the ceiling: the fields this relay knows how to
inspect or clamp. It stays in source so that a configuration file cannot widen
what gets forwarded past what RELAY-04 examines.

What is actually forwarded is narrower. The same validated body goes to
whichever model answers, so the effective set is the ceiling intersected with
what every ordered model advertises, computed at load by
`RelayPolicy.effective_parameters`. Intersecting can only remove, never add, so
the ceiling still bounds it; what the intersection buys is that a model which
does not advertise a parameter never receives it. That matters because a 4xx
from the endpoint deliberately does not fall back: one unsupported field would
end a session rather than move down the order.

Deliberately absent from the ceiling, because forwarding them invites a
provider 4xx and nothing here inspects them: response_format, stop,
frequency_penalty, presence_penalty, logit_bias, structured_outputs.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ModelConfigError

#: The only URL this relay will ever POST to. A constant, not a template.
UPSTREAM_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"

#: The ceiling on what may be forwarded. See the module docstring: the
#: effective set is this intersected with what every ordered model advertises.
ALLOWED_PARAMETERS = frozenset({
    "include_reasoning", "max_tokens", "reasoning", "reasoning_effort",
    "seed", "temperature", "tool_choice", "tools", "top_p",
})

#: Without these an agent cannot play at all: it narrates instead of acting.
#: Required of every model in the file, ordered or parked, because the order is
#: a script away from promoting any of them.
REQUIRED_MODEL_PARAMETERS = frozenset({"tools", "tool_choice"})

#: Accepted but replaced by policy rather than honoured. Rejecting outright
#: would break the OpenAI client, which always sends a model; the value is
#: discarded and the first ordered model substituted, which is what RELAY-02
#: permits ("rejected or replaced by immutable server policy").
OVERRIDDEN_PARAMETERS = frozenset({"model"})

#: Required in every request.
REQUIRED_PARAMETERS = frozenset({"messages"})

#: Fields whose presence means the caller is trying to steer routing. These are
#: refused rather than ignored, because silently dropping them would let a
#: caller believe it had changed provider behaviour.
ROUTING_PARAMETERS = frozenset({
    "provider", "route", "models", "transforms", "fallbacks", "order",
    "base_url", "api_base", "url", "endpoint", "host",
})

# ---------------------------------------------------------------------------
# The configured models, from a file
# ---------------------------------------------------------------------------

#: Names the operator's file. When it is set the file must load or the relay
#: refuses to start; when it is unset the packaged default beside this module
#: is used, which is what the unit tests and a bare `docker run` get. compose
#: always sets it, so a deployment cannot quietly run on the packaged set.
MODEL_CONFIG_ENV = "RELAY_MODEL_CONFIG"

#: Ships in the image via the Dockerfile's `COPY src/`. Same models as
#: config/relay/models.toml, which is the copy a deployment mounts and edits.
PACKAGED_MODEL_CONFIG = Path(__file__).with_name("models.default.toml")

#: Bumped when the file's shape changes incompatibly. Version 1 was a
#: [primary]/[fallback] pair; version 2 is an ordered set. A file written for
#: the other shape is refused rather than half-read.
MODEL_CONFIG_VERSION = 2

#: The most models one order may name. Not a statement about how many are
#: reasonable: it bounds the worst case, which is
#: (n - 1) * RELAY_PRIMARY_TIMEOUT + RELAY_UPSTREAM_TIMEOUT for a single turn.
#: At the shipped deadlines four models is 255 seconds of waiting before a turn
#: fails, which is already past what any watcher will sit through.
MAX_ORDERED_MODELS = 4

#: An allowlist, like the request validator's: an unrecognised key is refused
#: rather than ignored, so a typo in `providers` cannot silently leave the
#: provider order empty.
_TOP_LEVEL_KEYS = frozenset({"version", "order", "models"})
_MODEL_KEYS = frozenset({
    "providers", "name", "context_length", "max_completion_tokens",
    "supported_parameters", "pricing_prompt", "pricing_completion",
    "reasoning_default_effort", "reasoning_supported_efforts",
})

#: The id is the table key, and it goes into an outbound request body and into
#: a metrics label, so its shape is bounded here rather than wherever it lands.
_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._:-]+$")
_MAX_ID_LENGTH = 128

#: Provider *tags* (`endpoints[].tag`), which are lowercase and may be
#: qualified: the live catalog carries `nvidia`, `together`, and also
#: `nvidia/nvfp4`, `deepinfra/bf16`, `coreweave/bf16`, `venice/fp8`,
#: `baseten/fp4`. An earlier version of this pattern was written from a sample
#: of one and refused every qualified tag, which would have rejected a correct
#: configuration while telling the operator to use a tag rather than a display
#: name. Still lowercase and still bounded, so the display-name mistake
#: ("Nvidia") this check exists for is still refused.
_PROVIDER_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._/-]{0,63}$")
_MAX_PROVIDERS = 8


@dataclass(frozen=True, slots=True)
class PinnedModel:
    """One model this relay may request, and what it advertises about it."""

    id: str
    providers: tuple[str, ...]
    name: str
    context_length: int
    max_completion_tokens: int
    #: What the endpoint advertises. Required, because it is what the effective
    #: allowlist is computed from and what proves the model can call tools.
    supported_parameters: frozenset[str]
    pricing_prompt: str = ""
    pricing_completion: str = ""
    #: Optional as a pair. A model with no reasoning support gets no reasoning
    #: block in the catalog rather than an invented one; the relay sends
    #: `reasoning: {exclude: true}` on every request either way.
    reasoning_default_effort: str = ""
    reasoning_supported_efforts: tuple[str, ...] = ()


def model_config_path() -> Path:
    """The file the models will be read from, for the startup line and errors."""
    configured = os.environ.get(MODEL_CONFIG_ENV, "").strip()
    return Path(configured) if configured else PACKAGED_MODEL_CONFIG


def load_models(path: Path) -> tuple[PinnedModel, ...]:
    """Read the ordered set, or fail closed.

    Every refusal below is a startup failure with a message naming the file and
    the key at fault. There is no partial load: a relay that came up on half a
    policy would be a relay whose pin nobody could state.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        raise ModelConfigError(
            f"cannot read the model configuration at {path}") from None
    except UnicodeDecodeError:
        raise ModelConfigError(f"{path} is not UTF-8 text") from None

    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as err:
        raise ModelConfigError(f"{path} is not valid TOML: {err}") from None

    # Version first, before the key allowlist. A file written for version 1 is
    # full of keys this schema does not know, and telling its author that
    # `primary` is unrecognised describes a symptom while the version line
    # names the cause.
    version = data.get("version")
    if version != MODEL_CONFIG_VERSION:
        raise ModelConfigError(
            f"{path}: version must be {MODEL_CONFIG_VERSION}, got {version!r}. "
            "Version 1 was the [primary]/[fallback] pair; version 2 is an "
            "ordered set")

    unknown = sorted(set(data) - _TOP_LEVEL_KEYS)
    if unknown:
        raise ModelConfigError(
            f"{path}: unrecognised key(s) {', '.join(unknown)}; "
            f"only {', '.join(sorted(_TOP_LEVEL_KEYS))} are understood")

    defined = data.get("models")
    if not isinstance(defined, dict) or not defined:
        raise ModelConfigError(
            f"{path}: [models] must define at least one model")

    models = {identifier: _model(path, identifier, table)
              for identifier, table in defined.items()}

    order = data.get("order")
    if not isinstance(order, list) or not order:
        raise ModelConfigError(
            f"{path}: order must be a non-empty list of model ids")
    if len(order) > MAX_ORDERED_MODELS:
        raise ModelConfigError(
            f"{path}: order names {len(order)} models; at most "
            f"{MAX_ORDERED_MODELS} may be tried, because every attempt but the "
            "last costs a full RELAY_PRIMARY_TIMEOUT before the turn fails")
    if len(set(order)) != len(order):
        raise ModelConfigError(f"{path}: order names the same model twice")

    ordered = []
    for identifier in order:
        if not isinstance(identifier, str) or identifier not in models:
            raise ModelConfigError(
                f"{path}: order names {identifier!r}, which has no [models] "
                f"table. Defined: {', '.join(sorted(models))}")
        ordered.append(models[identifier])

    return tuple(ordered)


def _model(path: Path, identifier: object, table: object) -> PinnedModel:
    if not isinstance(identifier, str) or not identifier:
        raise ModelConfigError(f"{path}: a [models] key is not a model id")
    if len(identifier) > _MAX_ID_LENGTH:
        raise ModelConfigError(
            f"{path}: [models.\"{identifier[:40]}...\"] is longer than "
            f"{_MAX_ID_LENGTH} characters")
    if not _ID_PATTERN.match(identifier):
        raise ModelConfigError(
            f"{path}: [models.\"{identifier}\"] is not a vendor/model "
            "identifier")

    where = f'[models."{identifier}"]'
    if not isinstance(table, dict):
        raise ModelConfigError(f"{path}: {where} must be a table")

    unknown = sorted(set(table) - _MODEL_KEYS)
    if unknown:
        raise ModelConfigError(
            f"{path}: {where} has unrecognised key(s) {', '.join(unknown)}")

    providers = table.get("providers")
    if not isinstance(providers, list) or not providers:
        # An empty order with allow_fallbacks false does not pin routing, it
        # frees it: OpenRouter would be at liberty to pick any provider.
        raise ModelConfigError(
            f"{path}: {where}.providers must be a non-empty list of provider "
            "tags")
    if len(providers) > _MAX_PROVIDERS:
        raise ModelConfigError(
            f"{path}: {where}.providers lists more than {_MAX_PROVIDERS} "
            "providers")
    for tag in providers:
        if not isinstance(tag, str) or not _PROVIDER_PATTERN.match(tag):
            # Almost always the display name `Nvidia` instead of the tag
            # `nvidia`, or its quantisation-qualified form, which the API
            # accepts and then matches nothing. Written with backticks rather
            # than quotes on purpose: a quoted vendor/thing in this source is
            # what test_no_model_identifier_exists_in_the_source refuses, and
            # that guard is worth more than the punctuation.
            raise ModelConfigError(
                f"{path}: {where}.providers must be lowercase provider tags "
                f"from endpoints[].tag, not {tag!r}")

    supported = table.get("supported_parameters")
    if (not isinstance(supported, list) or not supported
            or not all(isinstance(p, str) and p for p in supported)):
        raise ModelConfigError(
            f"{path}: {where}.supported_parameters must be a non-empty list "
            "of the parameter names the endpoint advertises")
    missing = sorted(REQUIRED_MODEL_PARAMETERS - set(supported))
    if missing:
        # The checklist in public-documentation/OPERATIONS.md section 12 says it
        # in words; this says it at startup, where it cannot be skipped.
        raise ModelConfigError(
            f"{path}: {where} does not advertise {', '.join(missing)}. A model "
            "that cannot emit a tool call cannot play the game at all")

    name = _string(path, where, "name", table.get("name"), required=True)
    context_length = _positive_int(path, where, "context_length",
                                   table.get("context_length"), required=True)
    max_completion_tokens = _positive_int(path, where, "max_completion_tokens",
                                          table.get("max_completion_tokens"),
                                          required=True)
    pricing_prompt = _string(path, where, "pricing_prompt",
                             table.get("pricing_prompt"), required=False)
    pricing_completion = _string(path, where, "pricing_completion",
                                 table.get("pricing_completion"),
                                 required=False)

    efforts = table.get("reasoning_supported_efforts")
    default_effort = table.get("reasoning_default_effort")
    if (efforts is None) != (default_effort is None):
        raise ModelConfigError(
            f"{path}: {where} needs both reasoning_supported_efforts and "
            "reasoning_default_effort, or neither")
    if efforts is not None:
        if (not isinstance(efforts, list) or not efforts
                or not all(isinstance(e, str) and e for e in efforts)):
            raise ModelConfigError(
                f"{path}: {where}.reasoning_supported_efforts must be a "
                "non-empty list of strings")
        if default_effort not in efforts:
            raise ModelConfigError(
                f"{path}: {where}.reasoning_default_effort must be one of "
                f"reasoning_supported_efforts, got {default_effort!r}")

    return PinnedModel(
        id=identifier,
        providers=tuple(providers),
        name=name,
        context_length=context_length,
        max_completion_tokens=max_completion_tokens,
        supported_parameters=frozenset(supported),
        pricing_prompt=pricing_prompt,
        pricing_completion=pricing_completion,
        reasoning_default_effort=default_effort or "",
        reasoning_supported_efforts=tuple(efforts or ()),
    )


def _string(path: Path, where: str, key: str, value: object,
            *, required: bool) -> str:
    if value is None:
        if required:
            raise ModelConfigError(f"{path}: {where}.{key} is required")
        return ""
    if not isinstance(value, str) or not value:
        raise ModelConfigError(
            f"{path}: {where}.{key} must be a non-empty string")
    return value


def _positive_int(path: Path, where: str, key: str, value: object,
                  *, required: bool) -> int:
    if value is None:
        if required:
            raise ModelConfigError(f"{path}: {where}.{key} is required")
        return 0
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ModelConfigError(
            f"{path}: {where}.{key} must be a positive integer")
    return value


def _configured_models() -> tuple[PinnedModel, ...]:
    return load_models(model_config_path())


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return max(0.1, float(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


def _env_rate_wait(name: str, default: float) -> float:
    """Like _env_float, but zero is meaningful rather than clamped away.

    Every other duration here has a floor because a timeout of zero is a
    misconfiguration. This one has a zero that means something: do not wait at
    all, refuse the moment the window is full, rather than pacing.
    """
    try:
        value = float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    return 0.0 if value <= 0 else value


@dataclass(frozen=True, slots=True)
class RelayPolicy:
    """Budgets and shape limits. Every value has a reason, not a round number."""

    #: The models this relay may request, in the order it tries them. Read once
    #: at construction: a running relay's set cannot change.
    models: tuple[PinnedModel, ...] = field(default_factory=_configured_models)
    upstream_url: str = UPSTREAM_CHAT_URL

    #: Largest request body accepted. The endpoint's context window is a
    #: million tokens, but this agent sends one MUD observation and a short
    #: history; a megabyte is already far past anything legitimate and keeps a
    #: runaway prompt from becoming a bill or a latency stall.
    max_request_bytes: int = field(
        default_factory=lambda: _env_int("RELAY_MAX_REQUEST_BYTES", 1 << 20))

    #: Ceiling on max_tokens. The endpoints permit 65536 and more, which for a
    #: game decision is absurd; a capped value bounds both latency and quota
    #: burn. A request asking for more is clamped, not refused, because the ask
    #: is not hostile, merely wasteful.
    max_output_tokens: int = field(
        default_factory=lambda: _env_int("RELAY_MAX_OUTPUT_TOKENS", 4096))

    #: Requests per rolling minute. Twenty is not a number this project chose:
    #: it is OpenRouter's documented cap for free model variants, the same in
    #: both credit tiers, so raising it here would only move the refusal
    #: upstream to an endpoint that says no for the same reason. Credits change
    #: the daily cap, not this.
    max_requests_per_minute: int = field(
        default_factory=lambda: _env_int("RELAY_MAX_RPM", 20))

    #: How long a request may wait for a rate-limit slot before being refused.
    #: A rate is a statement about pace, and a caller that can be paced should
    #: be paced: the alternative is that a fast model exhausts the minute in
    #: forty-six seconds and the session ends with an error that reads like a
    #: fault.
    #:
    #: Bounded because an unbounded sleep moves the failure to the client,
    #: which has its own patience and no idea why the relay went quiet. Thirty
    #: seconds is half a window: long enough that a full minute of demand
    #: drains rather than fails, short enough that a caller waiting on one turn
    #: still has a session worth watching.
    #:
    #: Zero disables waiting and refuses immediately instead. It is read
    #: without the _env_float floor for that reason: that helper clamps to 0.1,
    #: which would leave no way to express "do not wait".
    max_rate_wait_seconds: float = field(
        default_factory=lambda: _env_rate_wait("RELAY_MAX_RATE_WAIT", 30.0))

    #: Total requests permitted in one relay session.
    max_requests_per_session: int = field(
        default_factory=lambda: _env_int("RELAY_MAX_SESSION_REQUESTS", 400))

    #: Wall-clock seconds a relay session may last before it stops closed.
    max_session_seconds: float = field(
        default_factory=lambda: _env_float("RELAY_MAX_SESSION_SECONDS", 7200.0))

    #: Whether a retryable failure may be retried on the next ordered model. On
    #: by default because the reason it exists is that the free endpoints fail;
    #: set RELAY_FALLBACK=0 to pin behaviour back to the first model alone. An
    #: order of one is single-model either way.
    fallback_enabled: bool = field(
        default_factory=lambda: os.environ.get("RELAY_FALLBACK", "1")
        not in ("0", "false", "no", "off", ""))

    #: How long every attempt but the last may hang before the next ordered
    #: model is tried. Shorter than the overall timeout on purpose.
    #:
    #: A fallback that waits the full upstream timeout before switching is a
    #: fallback nobody ever receives: measured against a stalling primary, the
    #: relay sat for 120 seconds and every caller gave up first. The point of
    #: the fallback is continuing to play, which requires discovering a model
    #: is unwell quickly rather than eventually. 45 seconds is above the
    #: observed p95 of 21 seconds and the observed max of 57, so a merely slow
    #: model is still used; a hung one is abandoned in time to matter. With
    #: three models ordered this is paid twice before the last attempt, which
    #: is why lowering it is recommended; see .env.example.
    primary_timeout_seconds: float = field(
        default_factory=lambda: _env_float("RELAY_PRIMARY_TIMEOUT", 45.0))

    #: Upstream request timeout, and the deadline on the last attempt.
    upstream_timeout_seconds: float = field(
        default_factory=lambda: _env_float("RELAY_UPSTREAM_TIMEOUT", 120.0))

    #: Largest upstream response body accepted before failing closed.
    max_response_bytes: int = field(
        default_factory=lambda: _env_int("RELAY_MAX_RESPONSE_BYTES", 8 << 20))

    @property
    def model(self) -> str:
        """The first model's id: what a request is rewritten to, and /healthz."""
        return self.models[0].id

    @property
    def attempt_order(self) -> tuple[PinnedModel, ...]:
        """The models to try, in order. One when the fallback is off."""
        return self.models if self.fallback_enabled else self.models[:1]

    @property
    def effective_parameters(self) -> frozenset[str]:
        """What may actually be forwarded, for this configuration.

        The ceiling intersected with what every model in the attempt order
        advertises. Narrowing only: a file cannot add a parameter the relay
        does not know how to handle, and a model never receives a field it does
        not advertise, which matters because a 4xx does not fall back.
        """
        effective = set(ALLOWED_PARAMETERS)
        for model in self.attempt_order:
            effective &= model.supported_parameters
        return frozenset(effective)


#: The single-model catalog served at /v1/models.
#:
#: Synthesised, never proxied. Two reasons. It removes an entire forwarding
#: path, so there is no request shape that reaches OpenRouter other than the
#: fixed chat completion. And the agent cannot discover that any other model
#: exists, because as far as this relay is concerned none does: the models
#: behind the first are never named here, whatever the configuration says.
#:
#: The reasoning block matters: the Hermes OpenRouter profile clamps its
#: requested effort to the catalog's supported_efforts, so these values must
#: match what the real endpoint advertises or the clamp will be wrong. They
#: come from the configuration for that reason, and a model configured without
#: a reasoning block gets no reasoning block here rather than an invented one.
def models_catalog(policy: RelayPolicy) -> dict:
    model = policy.models[0]
    entry = {
        "id": model.id,
        "name": model.name,
        "context_length": model.context_length,
        # A statement about this relay's contract rather than about the
        # endpoint: only chat messages are forwarded and only text comes back,
        # whatever else a configured model may be capable of.
        "architecture": {
            "modality": "text->text",
            "input_modalities": ["text"],
            "output_modalities": ["text"],
        },
        "top_provider": {
            "context_length": model.context_length,
            "max_completion_tokens": model.max_completion_tokens,
            # The relay adds no moderation layer of its own.
            "is_moderated": False,
        },
        # What this relay will actually forward, which is not what any one
        # model advertises: see RelayPolicy.effective_parameters.
        "supported_parameters": sorted(policy.effective_parameters),
    }
    if model.pricing_prompt and model.pricing_completion:
        entry["pricing"] = {"prompt": model.pricing_prompt,
                            "completion": model.pricing_completion}
    # Advertised only when the client could actually act on it. A catalog that
    # offers supported_efforts while the effective allowlist has dropped
    # `reasoning_effort` invites the one request the relay would then refuse:
    # the client reads an effort scale, sends an effort, and gets a policy
    # violation for a field this configuration cannot forward. When every
    # ordered model supports the parameter, the block is honest and useful.
    if (model.reasoning_supported_efforts
            and "reasoning_effort" in policy.effective_parameters):
        entry["reasoning"] = {
            "mandatory": False,
            "default_enabled": True,
            "supports_max_tokens": True,
            "supported_efforts": list(model.reasoning_supported_efforts),
            "default_effort": model.reasoning_default_effort,
        }
    return {"data": [entry]}
