# TinTin++ transport fixtures

Byte-accurate captures of the raw PTY stream between the harness and TinTin++,
covering the fifteen conditions required by
`public-documentation/TEST_PLAN.md` section 2.

Captured against the `dikumud` image (pinned commit `81b74dce`, patch series
0001-0006) through `mud-control` (TinTin++ 2.02.61) on the internal
`net_mcp_game` network.

## Provenance

Most files are live recordings taken through the transport's raw sink. Four are
constructed, because the condition cannot be produced on demand against a
healthy local server. Constructed files are marked below and are built from
real captures or real DikuMUD message formats, never invented prose.

| File | Condition | Source |
| --- | --- | --- |
| `01-connect-and-telnet-negotiation.bin` | 1. Connect + Telnet negotiation | live |
| `02-existing-character-authentication.bin` | 2. Existing-character auth | live |
| `03-first-use-creation-prompts.bin` | 3. First-use creation prompts | live |
| `04-normal-command-and-prompt.bin` | 4. Normal command + prompt | live |
| `05-fragmented-across-reads.bin` | 5. Output split across reads | constructed |
| `06-multiple-messages-one-read.bin` | 6. Several messages in one read | constructed |
| `07-delayed-output-before-prompt.bin` | 7. Delayed output before prompt | live |
| `08-automatic-combat-rounds.bin` | 8. Automatic combat rounds | live |
| `09-prompt-like-text-in-output.bin` | 9. Prompt-like text in prose | live |
| `10-ansi-and-control-sequences.bin` | 10. ANSI / control sequences | live |
| `11-graceful-shutdown.bin` | 11. Graceful shutdown | live |
| `12-abrupt-disconnect.bin` | 12a. Abrupt disconnect | live |
| `12b-reconnect-after-loss.bin` | 12b. Reconnect after loss | live |
| `13-quiet-timeout-no-prompt.bin` | 13. Quiet timeout, no prompt | live |
| `14-oversize-output-overflow.bin` | 14. Oversize output / overflow | constructed |
| `15-malformed-telnet.bin` | 15. Malformed Telnet data | constructed |

Notes on the constructed four:

* **05** is byte-identical to fixture 04. The condition under test is delivery,
  not content, so the test feeds these exact bytes one at a time and asserts
  the reassembled text matches the whole-read result.
* **06** concatenates real DikuMUD combat lines and a prompt with no read
  boundary between them.
* **14** is 360 KB, above the 256 KB unread bound, with `OLDEST-MARKER` and
  `NEWEST-MARKER` sentinels so a test can prove overflow discards oldest-first.
* **15** carries a truncated `IAC SB` with no `SE`, an unknown option byte, and
  a trailing bare `IAC`. TinTin++ normally consumes negotiation, so these
  reaching the harness is already abnormal; the requirement is only that they
  do not corrupt the surrounding game text.

Fixture 08 records a real engagement (`You miss the Cityguard with your hit.`)
but only one round, because the fight was interrupted and stock mobs wander.
The multi-message buffering case is covered by fixture 06.

## Credential hygiene

No credential material appears in any file. The transport redacts registered
secrets from the raw stream before the sink sees them, which was added after
Phase 2 verification showed TinTin++ echoes typed input: the character password
was present in the raw tap while absent from observations. Fixture 03 was
additionally scrubbed because its throwaway creation password was sent directly
rather than through the authenticating path.

Re-check with:

```
grep -l '<your test password>' *.bin
```
