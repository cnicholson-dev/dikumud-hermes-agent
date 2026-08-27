# Security Model

## 1. Objective

Permit the model to play one local DikuMUD character through five narrow MCP
tools while denying every unrelated capability. Controls are enforced by process,
container, network, protocol, and persistence boundaries; prompts are
supplementary, not primary security controls.

## 2. Protected Assets

| Asset | Required protection |
| --- | --- |
| OpenRouter API key | Relay-only; never model-visible or logged |
| DikuMUD credential | `mud-control`-only; never model-visible or logged |
| Host and Docker daemon | No socket, host mounts, privileged mode, or host networking |
| DikuMUD source/world/player data | Unavailable to Hermes and relay; game server authoritative |
| Hermes profile | Private dedicated volume; writable only through bounded framework facilities |
| Audit record | Append-only from runtime perspective; unavailable for agent modification |
| Tool and model configuration | Immutable to the model at all times, unreachable from its container, and writable by no service. The relay's model set and order are supplied by the operator before startup and read once |

## 3. Trust Boundaries

| Component | Trusted responsibilities | Must not expose |
| --- | --- | --- |
| `hermes-player` | Decision loop, identity, bounded memory and procedures | Shell, browser, arbitrary files/network, secrets, configuration mutation |
| `mud-control` | TinTin++ PTY, credential use, buffering, validation, state enforcement | PTY console, arbitrary target, generic subprocess or socket access |
| `dikumud` | Game rules, content, help, character persistence | Admin capability, source/world files, public listener |
| `openrouter-relay` | Inference against the ordered set of models in its trusted configuration file, and safe metrics | Credential, arbitrary proxying, model/provider override, caller-selected or open-ended fallback, the configuration file itself, any model outside the configured set |
| Docker host | Network enforcement, secrets, volumes, operations | Host filesystem or Docker control surface to runtime containers |

All game text and model output are untrusted data. Neither can alter system
policy, tool schemas, connection targets, secrets, or container configuration.

## 4. Mandatory Container Controls

Apply to every runtime service unless a documented technical requirement proves
otherwise:

- Dedicated non-root UID/GID.
- Read-only root filesystem.
- All Linux capabilities dropped.
- `no-new-privileges` enabled.
- No privileged mode, host PID/IPC/network namespace, or Docker socket.
- No host home, repository, source tree, or broad directory mounts. One
  exception, narrowly drawn: a service may take a single named file, read-only,
  carrying its own trusted policy. Today that is the relay's model configuration
  at `/etc/openrouter-relay/models.toml`, which is one file rather than a
  directory, is never writable, and may be sourced from outside the repository.
  A policy file baked into an image instead would make a model substitution a
  rebuild, and the point of holding the model set in configuration is that
  substituting one is an operator edit rather than a code change.
- Explicit writable `tmpfs` or narrow volume only where required.
- CPU, memory, PID, restart, health-check, and log-size limits.
- Immutable image reference by digest for releases.
- No public port publication. Spectator access, if present, binds to `127.0.0.1`.

## 5. Network Policy

Required logical flows:

| Source | Destination | Purpose |
| --- | --- | --- |
| `hermes-player` | `mud-control` | MCP only |
| `hermes-player` | `openrouter-relay` | Fixed inference API only |
| `mud-control` | `dikumud` | Fixed Telnet endpoint only |
| `openrouter-relay` | OpenRouter | Fixed HTTPS upstream only |

No other flow is authorized. The game network must be internal. Only the relay
attaches to an egress-capable network. Because ordinary Compose networks do not
express every peer ACL, verify the effective graph and use host/container
firewall rules or narrower pairwise networks where needed.

## 6. MCP Command Boundary

`mud_act` accepts exactly one printable bounded string while state is `READY`.

Reject:

- Newline, carriage return, null, escape, and control bytes.
- TinTin++ local commands, including `#` prefixes after normalization.
- Known TinTin++ or shell separators.
- Empty input, oversize input, encoded batches, or multiple commands.
- Calls made during `COMMAND_SENT`, `OBSERVING`, disconnected, faulted, or
  ambiguous state.

Normalize once, validate once, and send the same validated value. Avoid
validation/parsing differences between layers.

## 7. Transport State

- Only `READY` authorizes `mud_act`.
- Sending a command atomically moves to `COMMAND_SENT`.
- Incoming data moves to or remains in `OBSERVING`.
- A verified prompt or documented quiet-window rule may restore `READY`.
- Timeout, prompt ambiguity, malformed Telnet, and disconnect fail closed.
- Automatic combat output never implicitly authorizes another command.
- Unread output remains ordered and buffered under bounded memory limits.

## 8. Secret Handling

- Supply secrets through service-scoped Docker secrets or an equivalent isolated
  mechanism.
- Never bake secrets into images, Compose files, `.env.example`, source,
  fixtures, screenshots, or recordings.
- Inject character credentials directly into `mud-control`; do not return them
  through MCP.
- Relay upstream authorization internally; redact headers and upstream errors.
- Test every log, exception, tool result, audit event, metric label, spectator
  field, the play log, and the reasoning feed for disclosure. The play log is the
  one file holding game content, so it is written from the already-cleaned stream
  and its command markers come from the validated-command path rather than from
  the transport write that types the password.
- The reasoning feed holds model-authored content and is tested for the
  credential like everything else. It cannot contain the game password by
  construction rather than by filtering: the relay never receives it, so it is
  absent from the model's context and the model cannot reason about it. It is
  written to a tmpfs inside the relay and read by `docker compose exec`, never
  served, because the spectator reads `/metrics` from inside `hermes-player` and
  an endpoint would therefore be readable by the agent.
- Rotate any credential used during a failed disclosure test.

## 9. Learning Boundary

Automatic persistence is restricted to schema-valid bounded plain text:

- Factual memory records observed game facts and lessons.
- Procedures are inert Markdown guidance.
- No scripts, binaries, code blocks intended for execution, imports, executable
  resources, TinTin++ automation, shell commands, arbitrary file paths, URLs, MCP
  definitions, model configuration, tool definitions, or capability-expansion
  instructions.
- Writes are atomic, timestamped, size-bounded, and externally audited.
- Persisted content is revalidated when loaded, not trusted merely because it
  already exists.

## 10. Primary Threats and Controls

| Threat | Primary controls |
| --- | --- |
| Model attempts shell or filesystem access | Tool removal, read-only container, no host mounts, non-root execution |
| Model attempts arbitrary Internet access | No network tool, internal network, relay-only egress, firewall verification |
| Command injection reaches TinTin++ | One-command schema, normalization, control/prefix/separator rejection, PTY isolation |
| Overlapping commands create scripted behavior | Enforced turn-state machine and fail-closed ambiguity |
| Game text injects instructions | Treat output as untrusted data; fixed system policy and tool authority |
| Learned skill expands capability | Inert schema, mutation validation, load-time validation, immutable tool configuration |
| Relay becomes an open proxy | Fixed upstream, closed allowlist of verified models in an operator-set order (at most four, one attempt each), field allowlist narrowed to what every ordered model advertises, request limits, negative tests |
| Secret appears in output or logs | Service-scoped secrets, redaction, disclosure tests, external log review |
| Compromised service reaches peers | Least-privilege containers, verified network graph, narrow listeners/firewall policy |
| Free model disappears or changes behavior | Compatibility probe, response validation, an ordered set of verified models with deadline-bounded retries, fail-closed stop once the order is exhausted, and an operator-editable set and order so substitution needs no code change |
| Legacy DikuMUD defect affects host | Isolated non-root container, private listener, resource limits, no outbound route |

## 11. Residual Risks

- Model pretraining may include DikuMUD knowledge; the claim is no external
  retrieval, not zero prior knowledge.
- A free inference endpoint is not an availability commitment.
- Docker and kernel isolation reduce risk but are not equivalent to a separate
  physical host.
- Stock single-player game content minimizes prompt injection but does not make
  all text trustworthy.
- Long autonomous sessions increase unknown-state and cost/quota risk; this
  release remains supervised and bounded.

## 12. Security Release Rule

A failed security test stops release. Do not replace a failed technical control
with a prompt instruction or warning in documentation.
