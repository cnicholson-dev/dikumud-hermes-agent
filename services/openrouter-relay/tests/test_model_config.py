"""The models are operator configuration, and still pinned.

Two halves. The first proves the feature: a file the operator wrote decides
which models the relay asks for and in what order, and every property RELAY-02
and RELAY-03 protect survives that. The second proves the guard rails: every
way of getting the file wrong is a startup failure with a reason, because a
relay that came up on half a policy would be a relay whose pin nobody could
state.

The ids used here are deliberately not the nvidia ones. Nothing about the
shipped models is special to the code, and a test that only ever sees them
cannot tell the difference between reading a file and remembering a literal.
"""
from __future__ import annotations

from pathlib import Path

import httpx2
import pytest

from conftest import GOOD_RESPONSE, chat
from openrouter_relay.app import build_app
from openrouter_relay.errors import ModelConfigError
from openrouter_relay.policy import (MAX_ORDERED_MODELS, PACKAGED_MODEL_CONFIG,
                                     RelayPolicy, load_models,
                                     model_config_path, models_catalog)

KEY = "test-key-not-real"

ALPHA = "acme/alpha-70b:free"
BETA = "acme/beta-9b:free"
GAMMA = "acme/gamma-3b:free"

#: Nine on Alpha, eight on Beta and Gamma. The difference is the point: the
#: effective allowlist is the intersection, so ordering Beta or Gamma anywhere
#: costs `reasoning_effort` for the whole configuration.
NINE = ['"include_reasoning"', '"max_tokens"', '"reasoning"',
        '"reasoning_effort"', '"seed"', '"temperature"', '"tool_choice"',
        '"tools"', '"top_p"']
EIGHT = [p for p in NINE if p != '"reasoning_effort"']


def model_table(identifier: str, *, providers='["acme"]', params=None,
                reasoning=True, omit=()) -> str:
    lines = [f'[models."{identifier}"]']
    if "providers" not in omit:
        lines.append(f"providers = {providers}")
    if "name" not in omit:
        lines.append(f'name = "Model {identifier}"')
    if "context_length" not in omit:
        lines.append("context_length = 128000")
    if "max_completion_tokens" not in omit:
        lines.append("max_completion_tokens = 4096")
    if "supported_parameters" not in omit:
        lines.append("supported_parameters = ["
                     + ", ".join(params if params is not None else NINE) + "]")
    if reasoning and "reasoning" not in omit:
        lines.append('reasoning_default_effort = "medium"')
        lines.append('reasoning_supported_efforts = ["medium", "low"]')
    return "\n".join(lines) + "\n"


def config(order, tables) -> str:
    listed = ",\n  ".join(f'"{o}"' for o in order)
    return (f"version = 2\n\norder = [\n  {listed},\n]\n\n" + "\n".join(tables))


VALID = config(
    [ALPHA, BETA],
    [model_table(ALPHA, params=NINE),
     model_table(BETA, params=EIGHT, providers='["acme", "acme-eu/fp8"]',
                 reasoning=False)],
)


def write_config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "models.toml"
    path.write_text(body, encoding="utf-8")
    return path


def policy_from(tmp_path: Path, body: str = VALID, **overrides) -> RelayPolicy:
    return RelayPolicy(models=load_models(write_config(tmp_path, body)),
                       **overrides)


async def post(app, body) -> httpx2.Response:
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport,
                                  base_url="http://relay") as client:
        return await client.post("/v1/chat/completions", json=body)


async def get(app, path: str) -> httpx2.Response:
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport,
                                  base_url="http://relay") as client:
        return await client.get(path)


# -- the feature ---------------------------------------------------------

def test_the_file_decides_the_set_and_the_order(tmp_path):
    models = load_models(write_config(tmp_path, VALID))
    assert [m.id for m in models] == [ALPHA, BETA]
    assert models[0].providers == ("acme",)
    assert models[1].providers == ("acme", "acme-eu/fp8")


def test_a_model_defined_but_not_ordered_is_parked(tmp_path):
    """Configured and unused, which is how a model is kept without playing."""
    body = config([ALPHA], [model_table(ALPHA), model_table(BETA)])
    models = load_models(write_config(tmp_path, body))
    assert [m.id for m in models] == [ALPHA]


async def test_the_configured_order_is_what_gets_requested(tmp_path, stub):
    """The test this whole change exists for.

    A different order, and the relay asks different upstreams in that order,
    each with its own provider pin, walking down the list as each fails in a
    retryable way.
    """
    body = config([GAMMA, ALPHA, BETA],
                  [model_table(GAMMA, providers='["acme-us/nvfp4"]'),
                   model_table(ALPHA), model_table(BETA)])
    stub.script = [(503, b'{"error":{}}'),
                   (0, httpx2.ConnectError("no route")),
                   (200, dict(GOOD_RESPONSE, model=BETA))]
    app = build_app(api_key=KEY, policy=policy_from(tmp_path, body), client=stub)

    res = await post(app, chat())

    assert res.status_code == 200
    assert stub.models_requested == [GAMMA, ALPHA, BETA]
    assert stub.requests[0]["provider"] == {"order": ["acme-us/nvfp4"],
                                            "allow_fallbacks": False}
    assert stub.requests[2]["provider"] == {"order": ["acme"],
                                            "allow_fallbacks": False}


async def test_a_caller_still_cannot_pick_from_the_configured_set(tmp_path,
                                                                  stub):
    # RELAY-02 against a file the caller might have seen: naming a model that
    # is in the order is treated exactly like naming GPT-4, which is to say
    # ignored.
    stub.response = dict(GOOD_RESPONSE, model=ALPHA)
    app = build_app(api_key=KEY, policy=policy_from(tmp_path), client=stub)

    res = await post(app, chat(model=BETA))

    assert res.status_code == 200
    assert stub.models_requested == [ALPHA]


async def test_the_catalog_is_synthesised_from_the_first_model(tmp_path, stub):
    app = build_app(api_key=KEY, policy=policy_from(tmp_path), client=stub)

    data = (await get(app, "/v1/models")).json()["data"]

    assert len(data) == 1, "the models behind the first are never advertised"
    assert data[0]["id"] == ALPHA
    assert data[0]["context_length"] == 128000
    assert BETA not in (await get(app, "/v1/models")).text
    assert stub.urls == [], "still synthesised, not proxied"


async def test_healthz_names_the_first_model_and_not_the_rest(tmp_path, stub):
    app = build_app(api_key=KEY, policy=policy_from(tmp_path), client=stub)
    body = (await get(app, "/healthz")).json()
    assert body["model"] == ALPHA
    assert BETA not in str(body)


def test_the_effective_allowlist_is_the_intersection(tmp_path):
    """A parameter one ordered model lacks is forwarded to none of them.

    The same validated body goes to whichever model answers, and a 4xx does not
    fall back, so one unsupported field would end a session rather than move
    down the order.
    """
    both = policy_from(tmp_path)  # Alpha has nine, Beta has eight
    assert "reasoning_effort" not in both.effective_parameters
    assert len(both.effective_parameters) == 8

    alone = policy_from(tmp_path, config([ALPHA], [model_table(ALPHA)]))
    assert "reasoning_effort" in alone.effective_parameters
    assert len(alone.effective_parameters) == 9


async def test_a_parameter_outside_the_intersection_is_refused(tmp_path, stub):
    app = build_app(api_key=KEY, policy=policy_from(tmp_path), client=stub)

    res = await post(app, chat(reasoning_effort="high"))

    assert res.status_code == 400
    assert res.json()["error"]["type"] == "unsupported_field"
    assert stub.urls == [], "nothing is forwarded when a field is refused"


def test_the_catalog_hides_efforts_it_cannot_accept(tmp_path):
    """No advertised effort scale when no way to express one.

    Alpha advertises efforts, but with Beta ordered behind it the allowlist has
    no `reasoning_effort`, so offering the scale would invite exactly the
    request the test above shows being refused.
    """
    with_beta = models_catalog(policy_from(tmp_path))["data"][0]
    assert "reasoning" not in with_beta

    alone = models_catalog(
        policy_from(tmp_path, config([ALPHA], [model_table(ALPHA)])))["data"][0]
    assert alone["reasoning"]["supported_efforts"] == ["medium", "low"]


def test_a_model_without_a_reasoning_block_advertises_none(tmp_path):
    body = config([BETA], [model_table(BETA, params=EIGHT, reasoning=False)])
    entry = models_catalog(policy_from(tmp_path, body))["data"][0]
    assert "reasoning" not in entry, "an absent block is not an invented one"
    assert "pricing" not in entry, "no price is claimed for an unpriced model"


async def test_an_order_of_one_makes_one_attempt_and_stops_closed(tmp_path,
                                                                  stub):
    body = config([ALPHA], [model_table(ALPHA), model_table(BETA)])
    stub.status = 500
    stub.raw_body = b'{"error":{"message":"boom"}}'
    app = build_app(api_key=KEY, policy=policy_from(tmp_path, body), client=stub)

    res = await post(app, chat())

    assert res.status_code == 502
    assert res.json()["error"]["stop_reason"] == "upstream_unavailable"
    assert stub.models_requested == [ALPHA], "there is nothing to fall back to"


async def test_relay_fallback_off_uses_only_the_first(tmp_path, stub):
    stub.status = 500
    stub.raw_body = b'{"error":{}}'
    policy = policy_from(tmp_path, fallback_enabled=False)
    app = build_app(api_key=KEY, policy=policy, client=stub)

    res = await post(app, chat())

    assert res.status_code == 502
    assert stub.models_requested == [ALPHA]


# -- where the file comes from -------------------------------------------

def test_an_unset_env_uses_the_packaged_default(monkeypatch):
    monkeypatch.delenv("RELAY_MODEL_CONFIG", raising=False)
    assert model_config_path() == PACKAGED_MODEL_CONFIG
    assert RelayPolicy().models[0].id == load_models(PACKAGED_MODEL_CONFIG)[0].id


def test_the_env_var_names_the_file_that_is_used(monkeypatch, tmp_path):
    path = write_config(tmp_path, VALID)
    monkeypatch.setenv("RELAY_MODEL_CONFIG", str(path))
    assert model_config_path() == path
    assert [m.id for m in RelayPolicy().models] == [ALPHA, BETA]


def test_a_named_file_that_is_missing_fails_closed(monkeypatch, tmp_path):
    # The one that matters in a deployment: compose always sets the variable,
    # so a bind mount that did not happen must stop the relay rather than
    # silently serving the packaged set.
    monkeypatch.setenv("RELAY_MODEL_CONFIG", str(tmp_path / "absent.toml"))
    with pytest.raises(ModelConfigError) as err:
        RelayPolicy()
    assert "absent.toml" in str(err.value)


# -- the guard rails -----------------------------------------------------

def test_a_version_1_file_is_refused(tmp_path):
    """The pair schema and the ordered schema are not read as each other."""
    body = ('version = 1\n[primary]\nid = "acme/alpha:free"\n'
            'providers = ["acme"]\n')
    with pytest.raises(ModelConfigError) as err:
        load_models(write_config(tmp_path, body))
    assert "version must be 2" in str(err.value)


@pytest.mark.parametrize("body,expected", [
    ("", "version must be 2"),
    ("version = 2\n", "must define at least one model"),
    (f"version = 2\nmodel = 'x'\n{model_table(ALPHA)}", "unrecognised key(s) model"),
    (f"version = 2\norder = []\n[models]\n{model_table(ALPHA)}",
     "must be a non-empty list"),
    (config([ALPHA, ALPHA], [model_table(ALPHA)]), "same model twice"),
    (config([GAMMA], [model_table(ALPHA)]), "which has no [models] table"),
    (config([ALPHA], [model_table(ALPHA, omit=("providers",))]),
     "providers must be a non-empty list"),
    (config([ALPHA], [model_table(ALPHA, providers="[]")]),
     "providers must be a non-empty list"),
    (config([ALPHA], [model_table(ALPHA, providers='["Nvidia"]')]),
     "lowercase provider tags"),
    (config([ALPHA], [model_table(ALPHA, omit=("name",))]), "name is required"),
    (config([ALPHA], [model_table(ALPHA, omit=("context_length",))]),
     "context_length is required"),
    (config([ALPHA], [model_table(ALPHA, omit=("supported_parameters",))]),
     "supported_parameters must be a non-empty list"),
    (config([ALPHA], [model_table(ALPHA, params=['"tools"'])]),
     "does not advertise tool_choice"),
    # Half a reasoning block: efforts without a default. The other half is
    # covered by the default-not-in-efforts case below it.
    (config([ALPHA], [model_table(ALPHA, reasoning=False)
                      + 'reasoning_supported_efforts = ["high"]\n']),
     "or neither"),
    (config([ALPHA], [model_table(ALPHA, reasoning=False)
                      + 'reasoning_supported_efforts = ["medium"]\n'
                      + 'reasoning_default_effort = "high"\n']),
     "must be one of reasoning_supported_efforts"),
    ("this is not toml", "not valid TOML"),
])
def test_a_bad_file_is_refused_with_a_reason(tmp_path, body, expected):
    with pytest.raises(ModelConfigError) as err:
        load_models(write_config(tmp_path, body))
    assert expected in str(err.value)


def test_a_qualified_provider_tag_is_accepted(tmp_path):
    """The live catalog carries `nvidia/nvfp4`, `deepinfra/bf16`, `venice/fp8`.

    An earlier pattern was written from a sample of one and refused every
    qualified tag, which would have rejected a correct configuration while
    telling the operator to use a tag rather than a display name.
    """
    body = config([ALPHA], [model_table(ALPHA, providers='["nvidia/nvfp4"]')])
    assert load_models(write_config(tmp_path, body))[0].providers == \
        ("nvidia/nvfp4",)


def test_an_order_past_the_cap_is_refused(tmp_path):
    """The cap bounds the latency arithmetic, not the number of ideas.

    Every attempt but the last costs a full RELAY_PRIMARY_TIMEOUT before the
    turn fails.
    """
    ids = [f"acme/model-{n}:free" for n in range(MAX_ORDERED_MODELS + 1)]
    body = config(ids, [model_table(i) for i in ids])
    with pytest.raises(ModelConfigError) as err:
        load_models(write_config(tmp_path, body))
    assert f"at most {MAX_ORDERED_MODELS}" in str(err.value)


def test_a_model_that_cannot_call_tools_is_refused(tmp_path):
    """public-documentation/OPERATIONS.md section 12 says it in words; this says
    it at startup.

    Checked for every defined model rather than only the ordered ones, because
    the order is one `scripts/set-model-order` away from promoting any of them.
    """
    body = config([ALPHA], [model_table(ALPHA),
                            model_table(BETA, params=['"max_tokens"'])])
    with pytest.raises(ModelConfigError) as err:
        load_models(write_config(tmp_path, body))
    assert "cannot emit a tool call" in str(err.value)


def test_the_directory_case_a_missing_bind_mount_produces(tmp_path):
    # Docker creates a directory when a bind source does not exist, so the
    # relay must refuse a directory as clearly as a missing file.
    with pytest.raises(ModelConfigError) as err:
        load_models(tmp_path)
    assert "cannot read" in str(err.value)
