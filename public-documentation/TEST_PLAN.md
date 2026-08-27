# Test and acceptance plan

## 1. Test principles

- Prefer deterministic unit and transcript-fixture tests before live-model tests.
- Test every trust boundary from the hostile side.
- Treat credential disclosure, arbitrary network reachability, arbitrary
  process/filesystem access, command injection, model override, and executable
  persistence as blocking failures.
- Record safe evidence: test identifier, build revision, result, timestamp, and
  sanitized failure details.

## 2. Required TinTin++ fixtures

Sanitized byte-accurate fixtures exist for:

1. Initial connection and Telnet negotiation.
2. Existing-character authentication.
3. First-use character creation prompts.
4. Normal command and prompt.
5. Output fragmented across multiple reads.
6. Multiple messages received in one read.
7. Delayed output before prompt.
8. Automatic combat rounds and prompt return.
9. Prompt-like text inside ordinary game output.
10. ANSI/control sequences.
11. Server warning and graceful shutdown.
12. Abrupt disconnect and reconnect.
13. Quiet timeout with no recognized prompt.
14. Oversized output and unread-buffer overflow behavior.
15. Malformed or unexpected Telnet data.

Fixtures contain no real API key, password, host-secret path, or private user
data.

## 3. Test matrix

### Game and persistence

| ID | Test | Pass condition |
| --- | --- | --- |
| GAME-01 | Clean DikuMUD build | Pinned source and documented patches build reproducibly |
| GAME-02 | Private listener | Game is unreachable from unauthorized host interfaces |
| GAME-03 | Mortal character | Demo character has no admin, builder, debug, or privileged commands |
| GAME-04 | Restart persistence | Character state survives controlled container restart |
| GAME-05 | Help boundary | Stock in-game help is usable through ordinary commands |

### PTY and output

| ID | Test | Pass condition |
| --- | --- | --- |
| PTY-01 | Startup/shutdown | TinTin++ starts and terminates without orphaned process |
| PTY-02 | Partial output | Fragmented reads reconstruct ordered game text |
| PTY-03 | ANSI cleanup | Controls are stripped without corrupting meaningful text |
| PTY-04 | Prompt recognition | Real prompts settle; prompt-like prose does not |
| PTY-05 | Combat output | Automatic rounds buffer correctly without command unlock |
| PTY-06 | Disconnect | Loss is explicit, buffered data is retained, state fails closed |
| PTY-07 | Output bound | Oversize output is bounded with deterministic unread overflow behavior |

### MCP and turn state

| ID | Test | Pass condition |
| --- | --- | --- |
| MCP-01 | Tool inventory | Exactly five intended MUD tools are exposed |
| MCP-02 | One command | One valid call writes exactly one game command |
| MCP-03 | State rejection | `mud_act` fails outside `READY` without PTY write |
| MCP-04 | Atomic transition | Successful write moves state before a second call can enter |
| MCP-05 | Multiline injection | LF, CR, CRLF, null, escape, and encoded variants are rejected |
| MCP-06 | TinTin++ injection | `#system`, whitespace/case variants, aliases, and client prefixes are rejected |
| MCP-07 | Separator injection | Supported shell/client separators and command batches are rejected |
| MCP-08 | Fixed target | Agent cannot set host, port, identity, or credential source |
| MCP-09 | Credential secrecy | No tool response, exception, audit event, or log exposes credentials |
| MCP-10 | Session isolation | Stale or wrong session identifiers cannot control the PTY |

### Relay

| ID | Test | Pass condition |
| --- | --- | --- |
| RELAY-01 | Fixed upstream | Arbitrary URL and redirect attempts are rejected |
| RELAY-02 | Fixed model | Any model/provider override is rejected or replaced by immutable server policy |
| RELAY-03 | Bounded fallback | Endpoint failure retries at most once per model in the configured order, and only for an availability failure (unreachable, 5xx, 429); a hanging attempt is cut at a wall-clock deadline; 4xx, a malformed response and a model mismatch stop closed without a further attempt; the order being exhausted stops cleanly with an explicit reason, and no model outside the configured set is ever contacted |
| RELAY-04 | Request allowlist | Unsupported fields and oversize payloads are rejected |
| RELAY-05 | Budgets | Rate, token, request, timeout, and session caps are enforced. The rate limit may defer within a bound before refusing; the session budgets refuse immediately |
| RELAY-06 | Key secrecy | Authorization material is absent from responses, logs, metrics, and errors |
| RELAY-07 | Response validation | Malformed or incompatible upstream response fails closed |

### Learning

| ID | Test | Pass condition |
| --- | --- | --- |
| LEARN-01 | Factual persistence | Valid bounded observed fact survives restart |
| LEARN-02 | Procedure persistence | Valid inert Markdown procedure survives restart |
| LEARN-03 | Executable rejection | Scripts, executable resources, and automation content are rejected |
| LEARN-04 | Capability rejection | Tool/MCP/model configuration and expansion instructions are rejected |
| LEARN-05 | Reference rejection | Arbitrary paths, imports, and network references are rejected |
| LEARN-06 | Atomic mutation | Invalid write leaves prior state unchanged |
| LEARN-07 | Load validation | Tampered persisted content is rejected on reload |
| LEARN-08 | Audit | Accepted and rejected mutations create sanitized external events |

### Container and network security

| ID | Test | Pass condition |
| --- | --- | --- |
| SEC-01 | Identity | Every service runs non-root |
| SEC-02 | Filesystem | Roots are read-only; only declared narrow paths are writable |
| SEC-03 | Capabilities | All capabilities are dropped and `no-new-privileges` is active |
| SEC-04 | Host isolation | No Docker socket, host home, repository, broad mount, or host namespace exists |
| SEC-05 | Public exposure | No runtime service is published publicly |
| SEC-06 | Hermes egress | Hermes cannot directly reach public IPs, DNS targets, or OpenRouter |
| SEC-07 | Relay egress | Relay reaches only the required OpenRouter path |
| SEC-08 | Peer graph | Unauthorized service-to-service connections fail |
| SEC-09 | Volume isolation | Hermes cannot read DikuMUD, audit, relay, or another agent's state |
| SEC-10 | Resource limits | CPU, memory, PID, restart, log, and session bounds are effective |

### End-to-end behavior

| ID | Test | Pass condition |
| --- | --- | --- |
| E2E-01 | First character | Bootstrap hides secrets; model makes non-secret creation choices manually |
| E2E-02 | Manual play | Every game command maps to one visible model intent and one `mud_act` call |
| E2E-03 | In-game learning | Agent requests help and retains an observed useful fact without external retrieval |
| E2E-04 | Role continuity | Identity and goal remain recognizable across restart |
| E2E-05 | Failure stop | Quota, repetition, timeout, disconnect, and safety fault produce explicit stop reasons |
| E2E-06 | Reproduction | Independent operator follows documentation and completes supervised demo |

## 4. Non-blocking evaluation metrics

These describe the demo but do not authorize optimization automation:

- Valid-command rate.
- Repeated-command and no-progress rate.
- Prompt-settlement latency.
- Model request latency and token use.
- Disconnect/recovery count.
- Memory and procedure mutation count.
- Character survival, discoveries, and coherent goal completion.

XP/hour and gold/hour are not success metrics.
