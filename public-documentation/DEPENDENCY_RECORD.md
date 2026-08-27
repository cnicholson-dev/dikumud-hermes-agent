# Dependency record

Every upstream revision, image digest, dependency lock and model this project
pins, and why each pin is the one it is. `THIRD_PARTY_NOTICES.md` rests on this
document for its licence claims.

## 1. Upstream source pins

| Component | Pin | Resolved from | License |
| --- | --- | --- | --- |
| DikuMUD | commit `81b74dce0436b782d08b19064e32013c73525b45` (2020-12-07) | GitHub API, `Seifert69/DikuMUD` default branch `master` HEAD | LGPL-2.1 elected; original license retained |
| TinTin++ | tag `2.02.61` = commit `010881e7689c5328096f0b63002b898104cd72f1` (2026-01-29) | GitHub API `git/ref/tags/2.02.61` | GPL-3.0-or-later |
| Hermes Agent | tag `v2026.8.18` = commit `e624e9fde561e1add9388384012b295fde669ade` (2026-08-18) | GitHub API `releases/latest` | MIT |
| MCP Python SDK | `mcp==2.0.0` (published 2026-07-28) | PyPI JSON API | MIT |

### Why these revisions

**DikuMUD: HEAD is the pin.** The repository's last push was 2020-12-07 and
`master` HEAD is the commit that added the LGPL license. The tree is dormant, so
HEAD is stable and there is no release tag to prefer.

**TinTin++: tagged release, deliberately not HEAD.** `master` HEAD carried
unreleased in-development work. The pin is the last tagged release, `2.02.61`.
Verified at that commit: `src/tintin.h` line 216 defines
`CLIENT_VERSION "2.02.61 "`, so the tag and the source agree.

**Hermes Agent: tagged release, deliberately not HEAD.** The project tags
releases every few days, so HEAD is not reproducible.

**MCP SDK: 2.0.0, corroborated by Hermes itself.** Both `1.29.0` and `2.0.0` were
published on 2026-07-28, four minutes apart, so "latest" alone was not a
sufficient reason. The deciding evidence is that Hermes Agent v2026.8.18 pins the
same version in its own `pyproject.toml`:

```
mcp = ["mcp==2.0.0", "httpx2==2.7.0", "starlette==1.3.1"]  # starlette: CVE-2026-48710
```

Matching that pin keeps the client and server sides of the MCP boundary on one
protocol and one set of types.

## 2. Container base image digests

| Purpose | Reference | Index digest (use in `FROM`) |
| --- | --- | --- |
| `mud-control`, `openrouter-relay` | `python:3.12-slim` | `sha256:2c941e860699f878900b0edc2403613c234d4b32eda3cc9fa7036991a2a63c4a` |
| `dikumud` build and runtime | `debian:bookworm-slim` | `sha256:abd67ffcfa541b485a3dff59865ab629aa048a6c613e639d36e7456b0b229241` |
| `hermes-player` | `nousresearch/hermes-agent:v2026.8.18` | `sha256:22e37bb4ed1b0f50cb6bd991dca7ecacd6c9f29df9b4a20fc989d32bc763ccf6` |

amd64 platform-specific digests, for reference: `python:3.12-slim` =
`sha256:876416ecde9aca2bcc90e1fb0c7a9500bbf749f5788b70f82d4c5a5c2357f8b4`;
`debian:bookworm-slim` =
`sha256:362e64223cc0da95422b3b13c045186fc0a81250e765d31c025fbddf257f6143`;
`nousresearch/hermes-agent:v2026.8.18` =
`sha256:d597ca1f766ff23ff86437fe5e0f36a6049166ce91df917d9577d7418f0767de`.

**`python:3.12-slim` contents, verified by execution** at the pinned digest:
Python 3.12.14 on Debian GNU/Linux 13 (trixie); the default user is `root`, so
every Dockerfile built on it adds its own non-root `USER`.

**Why `debian:bookworm-slim` for `dikumud` rather than trixie.** Verified
candidate compiler versions: bookworm offers `gcc 4:12.2.0-3`, trixie offers
`gcc 4:14.2.0-1`. GCC 14 promotes implicit function declarations and implicit
`int` to hard errors by default, which 1990-era K&R C triggers pervasively.
Building on GCC 12 keeps the patch series to genuine portability fixes instead of
a mass rewrite of the upstream source. The cost is that the project runs across
two Debian generations.

**The Hermes image is the official one.** The Docker Hub tag list for
`nousresearch/hermes-agent` matches the GitHub release tags one-for-one, which is
what corroborates the repository as the official publish target. No `ghcr.io`
equivalent is public.

## 3. Python dependency locks

Both locks are fully hash-pinned and were installed into clean Python 3.12 venvs
under `pip install --require-hashes`, which succeeded for both.

| Service | Lock | Direct pins | Total pinned | Hash lines |
| --- | --- | --- | --- | --- |
| `mud-control` | `services/mud-control/requirements.txt` | `mcp==2.0.0`, `starlette>=1.3.1` | 28 | 430 |
| `openrouter-relay` | `services/openrouter-relay/requirements.txt` | `starlette`, `uvicorn`, `httpx2`, `pydantic` | 14 | 146 |

Key resolved versions, identical across both services where shared:
`mcp==2.0.0`, `mcp-types==2.0.0`, `starlette==1.6.0`, `uvicorn==0.52.4`,
`httpx2==2.12.0`, `httpcore2==2.12.0`, `pydantic==2.13.4`, `anyio==4.14.2`,
`sse-starlette==3.4.8`.

### Deliberate floor above the upstream requirement

`mcp==2.0.0` declares `starlette>=0.27` for Python < 3.14. That floor is wide
enough to admit a starlette carrying CVE-2026-48710 (GHSA-86qp-5c8j-p5mr,
"missing Host header validation that poisons `request.url.path`, bypassing
path-based security checks") on some future rebuild. Both `requirements.in` files
therefore raise the floor to `starlette>=1.3.1`.

Vulnerability status checked against OSV at lock time: `starlette==1.6.0`, the
resolved version, has no known advisories, and `starlette==1.3.1`, the version
Hermes pins for its own image, also has none. Per `SECURITY.md`, this floor may
be raised later but must not be lowered.

## 4. Model verification

The tables below are the verification record for the models this project ships
with, and the template for verifying any model an operator adds. The models live
in `config/relay/models.toml` rather than in the relay's source, so adding one is
a configuration change whose change control is exactly this record: the same
properties, checked the same way, added here, plus a passing
`scripts/verify-openrouter-tool-calling` for the new id. `OPERATIONS.md` section
12 is the procedure. Changing the *order* of models already recorded here is not
a change-control event; it is `scripts/set-model-order`.

### 4.1 `nvidia/nemotron-3-ultra-550b-a55b:free`

| Property | Value | Source |
| --- | --- | --- |
| Model id | `nvidia/nemotron-3-ultra-550b-a55b:free` | `GET /api/v1/models`, exact-match |
| Canonical slug | `nvidia/nemotron-3-ultra-550b-a55b-20260604` | same |
| Context length | 1,000,000 | same |
| Max completion tokens | 65,536 | same |
| Pricing | prompt `0`, completion `0` | same |
| Tool calling | `tools` and `tool_choice` in `supported_parameters` | catalog and endpoint record |
| Providers | exactly one: `Nvidia`, tag `nvidia` | `GET /api/v1/models/.../endpoints` |
| Reasoning efforts | `["high", "medium"]` | catalog |
| Endpoint status | `0` (normal), uptime last 30m 99.78% | same |
| Announced expiry | `expiration_date: null` | catalog |

### 4.2 `nvidia/nemotron-3-super-120b-a12b:free`

| Property | Value | Source |
| --- | --- | --- |
| Model id | `nvidia/nemotron-3-super-120b-a12b:free` | `GET /api/v1/models`, exact-match |
| Canonical slug | `nvidia/nemotron-3-super-120b-a12b-20230311` | same |
| Context length | 262,144 | same |
| Max completion tokens | 262,144 | same |
| Pricing | prompt `0`, completion `0` | same |
| Tool calling | `tools` and `tool_choice` in `supported_parameters` | catalog |
| Providers | exactly one: `Nvidia`, tag `nvidia` | `GET /api/v1/models/.../endpoints` |
| Endpoint status | `0` (normal), uptime last 30m 99.67% | same |
| Announced expiry | `expiration_date: null` | catalog |

Its context window is 262,144 rather than the first model's 1,000,000. Nothing in
the session budget approaches either, so they are interchangeable for this
workload; a future change that assumes a million-token window would not be.

Its reasoning metadata matters because `/v1/models` is synthesised from whichever
model leads, and the client clamps its requested effort to what that catalog
advertises:

```
reasoning: {"mandatory": false, "default_enabled": true,
            "supports_max_tokens": true,
            "supported_efforts": ["medium", "low"],
            "default_effort": "medium"}
```

These are not the first model's `["high", "medium"]`. Two models in the same
family disagree about the effort scale, which is why the catalog block comes from
the configuration rather than from a constant.

### 4.3 `nvidia/nemotron-3.5-lightning:free`

| Property | Value | Source |
| --- | --- | --- |
| Model id | `nvidia/nemotron-3.5-lightning:free` | `GET /api/v1/models`, exact-match |
| Context length | 1,000,000 | same |
| Max completion tokens | 65,536 | same |
| Pricing | prompt `0`, completion `0` | same |
| Tool calling | `tools` and `tool_choice` in `supported_parameters` | catalog and endpoint record |
| Providers | exactly one: `Nvidia`, tag **`nvidia/nvfp4`** | endpoints |
| Endpoint status | `0` (normal), uptime last 30m 99.51% | same |
| Announced expiry | `expiration_date: null` | catalog |
| Reasoning metadata | `{"mandatory": false}` and nothing else | catalog |

Two properties of this model shaped the relay's code rather than its
configuration.

**Its provider tag is qualified by quantisation:** `nvidia/nvfp4`, not `nvidia`.
Provider tags are not always a bare vendor name, and a validator that assumes so
refuses a correct configuration.

**It advertises no `reasoning_effort`.** The relay forwards the intersection of
what every ordered model advertises, rather than a fixed allowlist, because the
same validated body goes to whichever model answers and a 4xx does not fall back.
One unsupported field would end a session rather than move down the order.

### 4.4 Tool-calling compatibility

Each model was checked against the real endpoint with the same request shape the
relay uses, and each returned a well-formed tool call. The check is:

```
OPENROUTER_API_KEY_FILE=${SECRETS_DIR}/openrouter.key \
  scripts/verify-openrouter-tool-calling <model-id>
```

A passing run looks like this:

```
served model : nvidia/nemotron-3-super-120b-a12b:free
finish reason: tool_calls
tool call    : get_weather
arguments    : {"city":"Copenhagen"}
RESULT: PASS - pinned model emitted a well-formed tool call.
```

The id is a required argument rather than a default, so the check tests the model
the operator is about to pin rather than a copy of an identifier held somewhere
in the script.

An agent that cannot emit a tool call cannot play at all: it narrates instead of
acting. This is why a model that does not advertise `tools` and `tool_choice`
stops the relay at startup rather than failing at the first turn.
