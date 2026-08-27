# Operations

This document covers running the stack, recovering the states that fail closed,
and the routine operator jobs: backup and restore, log retention, upgrade and
rollback, and changing which models play.

## 0. Running the stack

Prerequisites: the secrets directory described in `.env.example`, and a `.env`
copied from it with `SECRETS_DIR` set to a full path.

Three scripts run the whole lifecycle, and each one does the steps below in an
order that matters:

```
scripts/1.start-stack     # preflight, SEC-07 rules, start, health, 21 checks
scripts/2.start-agent     # play one session and watch it
scripts/3.stop-stack      # end the run at the boundary, stop in reverse order
```

`1.start-stack` takes `--build`, `--skip-egress`, `--skip-verify` and
`--timeout`; `2.start-agent` takes `--turns`, `-q` and `--no-watch`;
`3.stop-stack` takes `--stop-only` and `--timeout`. Each explains itself with
`-h`.

By hand, the same thing:

```
sudo scripts/restrict-relay-egress apply   # first; see section 0.1
docker compose up -d --build     # build and start, health-gated
docker compose ps                # all four should read (healthy)
scripts/verify-network-boundaries
docker compose logs -f mud-control
docker compose down              # stop; volumes survive
```

Start a supervised play session, which is the only way the agent acts:

```
docker compose exec hermes-player hermes chat -q "..." --max-turns 100
```

`hermes-player` idles by design, so "the stack is up" does not mean "the agent
is playing". Nothing runs unattended.

Startup order is enforced by health checks, not by luck: `dikumud` must be
healthy before `mud-control` starts, and both `mud-control` and
`openrouter-relay` before `hermes-player`. A dependency that never becomes
healthy stops the chain there, with `dependency failed to start` naming the
service.

### 0.1 The egress restriction

`SEC-07` requires the relay to reach the OpenRouter path and nothing else.
Compose cannot express that, so it is host firewall policy and it is **not**
applied by `docker compose up`:

```
sudo scripts/restrict-relay-egress apply
sudo scripts/restrict-relay-egress verify
```

**Apply before starting the stack, not after.** The relay self-tests for these
rules at startup and exits without them, so on a host that has rebooted, the
rules-after-`up` order fails: the relay exits and `hermes-player` reports
`dependency failed to start`. `scripts/1.start-stack` applies them first, and
re-applies on every start, which also refreshes an address set that rotates
behind Cloudflare. `--skip-egress` opts out for a deployment that restricts the
relay's egress some other way.

Re-apply if the upstream address set changes; the symptom is a relay that cannot
reach the endpoint, which fails closed with an explicit stop reason rather than
falling back to anything. `remove` takes the rules out again.

**You cannot forget this step silently.** The rules are host state and do not
survive a reboot on their own, so the relay checks at startup: it opens a
connection to an address it must not be able to reach, and exits if that
succeeds. A stack whose relay will not start with

```
EgressNotRestricted: This container reached 1.1.1.1:443, so its egress is not
restricted and SEC-07 is not satisfied.
```

needs the rules applied, not the check disabled. `RELAY_EGRESS_SELFTEST=0` exists
for deployments that restrict egress some other way, and turning it off to make a
red message go away puts you in exactly the state the check is for.

### 0.2 Verifying the whole boundary

```
scripts/verify-network-boundaries
```

21 checks across SEC-06 to SEC-09, run from inside the containers, no root
needed, non-zero exit on any failure. Run it after every deployment and after any
change to networks, ports or volumes. It tests the effective graph, which is the
only kind that counts: reading `compose.yaml` does not show you which peers can
actually reach each other.

### 0.3 First run after a rebuild

The game server takes a few seconds to accept logins after it starts, and a
`mud_connect` issued inside that window can land at the name prompt instead of in
the game. Section 4 covers the recovery. Health gating makes this rare, because
`mud-control` does not start ahead of a healthy game.

## 1. The learning store

`mud-control` owns two documents on the `vol_mudcontrol_learning` volume, mounted
at `/var/lib/mud-control/learning`:

| File | Holds |
| --- | --- |
| `facts.json` | Bounded observed facts, one record each, with a timestamp and a digest |
| `procedures.json` | Inert Markdown procedures, same metadata |

The agent reaches them only through the six learning tools on port 8766. It
cannot read the volume, and it cannot read the audit record that says what it
wrote. `DESIGN.md` section 8 covers why the store is here rather than in Hermes'
own memory and skills facilities.

## 2. Quarantine: what it is and how to clear it

Every read revalidates every stored record. If a document fails to parse, fails
the schema, fails the content policy, or a record's digest does not match its
text, the store **quarantines that document**: reads and writes touching it are
refused, and the file is left exactly as found because it is evidence.

Quarantine is per document. A bad `facts.json` does not take procedures down with
it, but `learn_recall` loads both, so it fails while either is quarantined.

### 2.1 Recognising it

The service records the fault at startup and on the first read that hits it:

```
{"event":"learning_quarantined","kind":"facts","reason":"digest_mismatch","detail":"record fact-0002"}
{"event":"learning_unavailable","reason":"store_quarantined"}
```

The agent sees the same thing through any learning tool:

```
{"stored": false, "error": "store_quarantined",
 "detail": "the stored facts document was refused on load (digest_mismatch):
            record fact-0002. Learning is unavailable until an operator
            resolves it; nothing was changed."}
```

`reason` is the whole diagnosis. The ones you will see:

| Reason | Means |
| --- | --- |
| `malformed_json`, `malformed_document`, `malformed_record` | The file is not the shape a document has |
| `unknown_schema`, `wrong_kind` | The file is a document of some other version or kind |
| `digest_mismatch` | A record's text was changed without recomputing its digest: an edit or corruption on the volume |
| `invalid_on_load:<rule>` | The text is intact but the content policy now refuses it, usually because the validator was tightened after the record was written |
| `document_too_large`, `over_capacity`, `duplicate_id` | Bounds exceeded; suspect a bad write or an edit |
| `unreadable` | The file exists but could not be read. **Do not repair blind**: check permissions and disk first |

### 2.2 Deciding what happened

Before repairing, establish which of two situations you are in, because they call
for different responses.

1. **The policy changed.** `invalid_on_load:<rule>` after a deployment that
   touched `learning/validate.py` or `learning/schema.py`. The record was valid
   when written and the boundary is working as designed. Removing the record is
   the correct repair.
2. **The volume changed.** `digest_mismatch`, `malformed_json`, or anything at
   all when the code did not change. Something wrote to that volume that was not
   this service. Treat it as an incident: nothing in the design writes there
   except `mud-control`, so find out what did before repairing, and preserve a
   copy of the file first.

The audit record on `vol_mudcontrol_audit` is the timeline. Every accepted and
refused mutation is there by reason, size and digest, so it will tell you what
the store last accepted and when.

### 2.3 Repairing

The store is plain JSON and the repair is to remove the offending record. There
is no repair tool, deliberately: an operator looking at the actual file is a
better safeguard than an automatic fixer that could quietly discard learning.

Take a copy first, always:

```
docker cp mud-control:/var/lib/mud-control/learning/facts.json ./facts.json.bak
```

Edit a working copy, removing only the record named in `detail`. Leave every
other record byte-identical, including its digest: those digests are still valid,
and rewriting them by hand defeats the check.

Write it back **as the service's own uid**, so ownership and mode survive. A
`docker cp` back would land the file as another user and the service could then
fail to write it:

```
docker run --rm -i -u 10002 -v vol_mudcontrol_learning:/store \
  mud-control:<tag> sh -c 'cat > /store/facts.json' < ./facts.json
docker restart mud-control
```

Confirm the store loads. This line is the all-clear:

```
{"event":"learning_loaded","facts":3,"procedures":1}
```

If it quarantines again with a different record named, repeat: the reload stops
at the first bad record rather than listing them all.

### 2.4 Starting over

If the document is beyond repair, an empty store is valid and the service will
create it. Removing the file loses every fact in it, so copy it aside first.

```
docker run --rm -u 10002 -v vol_mudcontrol_learning:/store \
  mud-control:<tag> rm /store/facts.json
docker restart mud-control
```

The character's game progress is unaffected: that lives in DikuMUD's own volume,
and the agent's identity lives in `SOUL.md` on the Hermes volume. Only the
learned facts are lost.

## 3. Inspecting the store without the agent

Reading the volume directly is the operator's view and does not disturb the
session:

```
docker exec mud-control cat /var/lib/mud-control/learning/facts.json
docker exec mud-control cat /var/lib/mud-control/learning/procedures.json
docker exec mud-control grep learning_ /var/log/mud-control/audit.jsonl | tail
```

Both documents are small by construction: at most 64 facts of 240 characters and
12 procedures of 4000 characters.

## 4. A session that cannot reach the game

Two states look similar from the outside and are not.

**The session is open and healthy.** `mud_connect` returns the existing session
with `"resumed": true`. This is normal when a new agent process joins a session
an earlier one left open. Nothing to do.

**The login is stuck at the name prompt.** `mud_connect` returns
`"prompt": "name"` with `"turn_state": "OBSERVING"`, and commands are refused as
`not_ready`. This is a race in the login handshake, seen when the game server has
just restarted. The state machine is failing closed correctly; the session is
simply not usable.

Recovery is one round trip, and does not need a restart of anything:

```
mud_disconnect(session_id)   then   mud_connect()
```

which returns `"prompt": "game"` and `"turn_state": "READY"`. Health-gated
startup ordering is the durable fix; this is the recovery when it still happens.

## 5. Stopping a session

The agent runs as a `hermes chat` process in `hermes-player`, started detached by
`scripts/2.start-agent`:

```
docker exec hermes-player pkill -f "hermes chat"
```

Ctrl-C in the spectator `2.start-agent` opens does the same thing, because
watching and playing start from one command. `--no-watch` is the way to leave a
run playing after the script exits.

Stopping the client does not disturb the game session, the learning store, or the
character. `mud-control` keeps the session open, and the next agent process
resumes it per section 4.

### 5.1 What a teardown does to the character

Stopping the stack **saves the character**, and the order is what makes that
true. DikuMUD's `close_socket` runs when the link drops on a playing character:

```c
	if (d->character)
		if (d->connected == CON_PLYNG)
		{
			save_char(d->character, NOWHERE);
```

That is the same `save_char(ch, NOWHERE)` the `save` command makes and the same
one `quit` reaches through `extract_char`, and it is the only automatic one:
there is no autosave anywhere in the tick loops. So the player file is written
when the link goes, provided the game is still running to notice it going.

`scripts/3.stop-stack` stops `mud-control` before `dikumud`, with a settle
between them, so TinTin++ dies first and the game's select loop has a moment to
process the closed socket. Stopping both at once risks killing the game before it
runs that save.

Equipment is a separate matter and is not in the player file: `save_char` writes
a `char_file_u` record only. Objects persist through the inn's rent system, in
`pcobjs.obj`, which is a gameplay decision for the agent rather than an operator
step. A character that has not rented loses its gear across a teardown whichever
way the session ended.

## 6. Choosing a character

The stack plays one character at a time. Two scripts do this; the manual
equivalent is written out below them, because knowing what they do is how you
recognise a stack that was changed by hand.

```
scripts/4.load-character Wren     play as an existing character
scripts/5.new-character Bram      make a new one and load it
scripts/2.start-agent             resume whoever is loaded
```

**Why it is a script rather than two edits.** Two different values decide who
plays. The login name is the first line of the credential file. The learning
store is keyed by `MUD_CONTROL_CHARACTER`, which is read once at startup:

```python
    store = LearningStore(
        LearningStore.root_for(..., config.character), audit)
```

Nothing reconciles them. Change one and not the other and the stack logs in as
one character while writing another's notes, and the mistake surfaces later as a
character that "remembers" rooms it never walked into.
`scripts/4.load-character` writes both from one input, or refuses. It also
recreates `mud-control` rather than restarting it, because that environment value
is read in `build_from_env` and a restart would keep the old one.

**The shelf.** One file per character, outside the repository, beside the two
secrets:

```
${SECRETS_DIR}/characters/Wren      two lines: name, password
${SECRETS_DIR}/characters/Bram
```

Only `${SECRETS_DIR}/game-credential` is mounted into anything;
`4.load-character` copies the selected shelf file over it and keeps a dated copy
of what was there. Adopt a credential you already have with one copy:

```
cp ${SECRETS_DIR}/game-credential ${SECRETS_DIR}/characters/Wren
```

**Continue an existing character**: `scripts/4.load-character Wren`, or change
nothing at all if it is already loaded. Its learning lives under its own name in
the store, so it resumes with the facts and procedures it wrote, and its game
progress is in DikuMUD's player file.

**Start a new character**: `scripts/5.new-character Bram`. It generates the
password, refuses a name the game already knows, writes the shelf file, loads it,
and prints a first-session prompt to use instead of the default. Doing it by hand
is the same thing: a new name and password in `game-credential`, `MUD_CHARACTER`
set to match, and `mud-control` recreated.

Either way, the game will not recognise a new name, so it walks the creation
path. `mud-control` answers only the identity prompts, which is where the secret
is: the name, the "did I get that right" confirmation, the new password, and the
retype. It then stops. Sex and class are gameplay choices and are left for the
agent to answer through `mud_act`, one command at a time. Nothing on the host can
do that part for it: DikuMUD writes no character record until creation finishes,
so a character only exists once the agent has answered.

The name check `5.new-character` runs has three outcomes, and the third is the
one to recognise. A free name reports free; a name in the player file reports
that it exists; the character that is **currently linked** reports neither,
because the game answers a second connection using an in-game name with no prompt
the probe can read. The script refuses in that case rather than guessing free.

The new character starts with an **empty** learning store, because it has not
played yet, and the previous character's notes stay untouched under their own
name. Both are on `vol_mudcontrol_learning`:

```
/var/lib/mud-control/learning/Wren/facts.json
/var/lib/mud-control/learning/Wren/procedures.json
```

Switching back to a previous character is the same operation in reverse: its name
and password in the credential file, and its notes are still there.

## 7. Backup and restore

Four volumes hold everything that is not rebuildable from the repository.

| Volume | Holds | Lose it and |
| --- | --- | --- |
| `vol_dikumud_data` | The world and the player file | The character's level, gold and equipment are gone |
| `vol_mudcontrol_learning` | Facts and procedures, per character | The agent's learned experience is gone |
| `vol_mudcontrol_audit` | The append-only record | The evidence of what happened is gone |
| `vol_hermes_state` | `SOUL.md`, sessions, state database | The agent's identity and session history are gone |

Back them up with the stack stopped, so nothing is mid-write:

```
docker compose stop
for v in vol_dikumud_data vol_mudcontrol_learning vol_mudcontrol_audit vol_hermes_state; do
  docker run --rm -v "$v":/src:ro -v "$PWD/backups":/out alpine \
    tar czf "/out/$v-$(date +%Y%m%d).tar.gz" -C /src .
done
docker compose start
```

Restore one the same way, writing **as the owning uid** so the service can still
write afterwards: 10001 for the game, 10002 for mud-control, 10000 for Hermes.

```
docker run --rm -u 10002 -v vol_mudcontrol_learning:/dst \
  -v "$PWD/backups":/in:ro alpine \
  sh -c 'cd /dst && tar xzf /in/vol_mudcontrol_learning-YYYYMMDD.tar.gz'
```

After restoring the learning store, restart `mud-control` and check the load
line. The store revalidates every record, so a restore that brings back content
the current validator refuses will quarantine rather than load, and that is the
correct outcome:

```
{"event":"learning_loaded","facts":4,"procedures":1}
```

A volume archive is credential-bearing in one respect: a DikuMUD player record
carries a DES hash of the character's password, so
`vol_dikumud_data` archives are not something to attach to a report. `backups/`
is git-ignored for that reason.

Backups otherwise contain no credential. Both secrets live outside the volumes,
in `${SECRETS_DIR}` on the host, and are the operator's to back up separately.

### 7.1 Rotating the game password

1. Stop `mud-control` so the character is not holding a session, and restart
   `dikumud` if the character was connected, or the login will reconnect straight
   into the game instead of showing the account menu.
2. Log in as the character and choose **4) Change password**. It asks for the new
   password twice and never for the old one: reaching the menu already proved you
   know it.

   The host has no route to the game and must not have one, which is SEC-06 and
   is tested from inside the containers, so the way in is a throwaway container
   on the game's own network:

```
docker run --rm -it --network net_mcp_game busybox telnet dikumud 4000
```

   It leaves nothing behind and joins no other network. Type the character's name
   and current password at the prompts to reach the menu.
3. The server answers `Done. You must enter the game to make the change final`.
   That is literal. Choose **1) Enter the game**, then `save`, then `quit`.
   Exiting from the menu instead leaves the change unsaved.
4. Update the second line of `${SECRETS_DIR}/game-credential`, **and of
   `${SECRETS_DIR}/characters/<Name>` if that character is on the shelf.** Two
   files hold it (section 6). Updating only the live one works until the next
   `scripts/4.load-character`, which would copy the stale password back over it,
   and the failure then looks like a game that stopped accepting a password that
   has not changed.
5. Start `mud-control` and confirm the agent connects.

DikuMUD's change-password menu does not work without patch `0007`; unpatched it
answers `Passwords don't match.` for every password. See
`services/dikumud/patches/README.md`.

### 7.2 Rotating the OpenRouter key

Create the new key **before** revoking the old one, so there is no window where
neither works.

1. Create a second key in the provider account.
2. Copy it over `${SECRETS_DIR}/openrouter.key`, keeping a dated copy of the old
   one.
3. `docker compose up -d --force-recreate openrouter-relay`, then make a live
   call and confirm it answers.
4. **Now revoke the old key** and make another live call. It must still work.
   This step is the one that proves the rotation took: without it, a stack that
   silently kept using the old key looks identical to one that rotated.
5. Delete the dated copy of the old key once you are satisfied.

A revoked key produces `The model endpoint returned status 401` from the relay,
which reports the upstream status without passing its response body through. A
401 is deliberately not a reason to try the next ordered model: a bad credential
is not an availability problem, and retrying it elsewhere would turn one rejected
request into two. So a failed rotation fails immediately and visibly rather than
being masked by a fallback.

The new key needs access to **every** model in `config/relay/models.toml`, not
just the first. A key that can reach only the first passes step 3 and then fails
the first time that model is unavailable, which may be days later. Check each id
in that file before revoking the old key:

```
for m in $(scripts/set-model-order --list | awk '/^ +[0-9]+\.|^ {6}nvidia/ {print $NF}'); do
  OPENROUTER_API_KEY_FILE=${SECRETS_DIR}/openrouter.key \
    scripts/verify-openrouter-tool-calling "$m"
done
```

which for a clean clone is Ultra, Super and 3.5 Lightning. A parked model is
checked too: the order is one `scripts/set-model-order` away from promoting it.

## 8. Logs and retention

Container logs are capped by compose at 10 MB per file, three files per service,
so the stack cannot fill a disk with output. `docker compose logs` is the usual
view.

The audit record is **not** a log and is not rotated. It is append-only evidence
on its own volume, it grows slowly (one line per command, per refusal and per
learning mutation), and it is what the spectator reads. If it ever needs
trimming, archive the file rather than truncating it in place, and do it with the
service stopped.

Neither the relay nor the MCP server writes request bodies or game text to a
container log: the relay's access logging is off because request lines carry game
state and headers carry the credential, and the audit record stores reasons,
digests and lengths rather than content.

### 8.1 Session captures

Every watched session leaves a directory in `sessions/`, written by
`scripts/capture-session`:

| File | What it is |
| --- | --- |
| `session_id` | The session this capture belongs to, so two scripts cannot capture it twice |
| `play.log` | This session's game text, sliced out of the rolling play log |
| `transcript.txt` | One line per command and refusal, from the audit record |
| `frame.txt` | The final spectator frame: state, learning, cost, stop reason |
| `client.log` | `hermes chat`'s own output, which is where a session that failed to start says why |
| `reasoning.log` | What the model was thinking, as the relay streamed it. It lives on a tmpfs in that container, so this copy is the only one that outlives the session |
| `session.cast` | The asciicast of the spectator, when the run was watched |
| `session.gif` | Converted from the cast with `agg` |

A capture is **not evidence**. The audit record on its own volume is, and a
capture is a readable copy assembled from it and from the play log. Losing one
costs nothing; `sessions/` is git-ignored for the same reason `backups/` is.

A run started with `--no-watch` has no spectator to record, so its text bundle is
written by `scripts/3.stop-stack` when that ends the session. The `--if-new` flag
it passes means a session already captured by `2.start-agent` is not captured a
second time.

The capture runs the disclosure checks from `DEMO.md` over everything it wrote. A
credential match anywhere, or reasoning content in the files that come from the
spectator's own sources, renames the directory to `-REVIEW` and exits non-zero.
The directory is kept deliberately: a disclosure is something to look at, not
something to delete. Reasoning in `client.log` is a warning rather than a
failure, because that file is the client's own output rather than part of the
reviewed surface, and it is diagnostic rather than publishable.

### 8.2 The play log

One file does hold content, deliberately: `/var/log/mud-control/play.log` on the
audit volume, the game's own output as the transport cleaned it, plus each
accepted command written after the prompt the game sent, so the file reads as the
session looked. It exists because `DESIGN.md` section 10 asks the spectator to
show live game output and nothing else carried it. It is what fills the
right-hand column of `scripts/spectate`.

| Property | How |
| --- | --- |
| No credential | Written from the cleaned stream, after echo suppression and redaction. Markers come from the session layer, which only ever holds a validated model command, never the login handshake |
| Bounded | Rotates once at `MUD_CONTROL_PLAY_LOG_BYTES` (512 KB) to `play.log.1`. One generation, so a long session cannot fill the volume the audit record lives on |
| Not evidence | Unlike the audit record it can be deleted freely. It is a view of a session, not a record of one |
| Optional | `MUD_CONTROL_PLAY_LOG=` (empty) turns it off, and the spectator then shows the panels alone |

Read it directly when the spectator is not what you want:

```
docker exec mud-control tail -f /var/log/mud-control/play.log
```

## 9. Upgrade and rollback

**The stack.** Rebuild and restart; the volumes carry the state across.

```
git pull
scripts/1.start-stack --build
```

That rebuilds, restarts health-gated and runs the boundary checks, which is the
same sequence as `docker compose up -d --build` followed by
`scripts/verify-network-boundaries`.

Roll back by checking out the previous revision and rebuilding the same way. The
images are rebuilt from source, so a rollback is a source rollback.

**Retiring old image tags.** Tagged images accumulate, and an image with no
running container is easy to lose track of: a container built from a superseded
tag can stay running for days with its own network and its own volume, holding a
`restart: on-failure` policy that carries it through a reboot. Once the stack
runs a newer tag, remove the older ones **by explicit tag**:

```
docker image rm <service>:<old-tag>
```

**Never `docker image prune -a` here.** A host may also carry images belonging to
other projects, and a prune takes anything without a running container, which on
a machine that runs one stack at a time is most of them. Remove tags you named.

**The agent image.** It is pinned by digest in `.env`, because a tag can move.
Changing it is not routine, and it has a specific consequence: a built-in toolset
Hermes has not seen before arrives **enabled**, so a new image can silently widen
the agent's tool surface. After any image bump, re-run the tool inventory,
including the `tool_search` probe, and update `known_builtin_toolsets` in the
profile seed.

**Upstream game or client.** `DikuMUD` and `TinTin++` are pinned by commit in
their Dockerfiles and verified at build time; changing either means re-running
the `GAME-*` and `PTY-*` suites, not just the build.

## 10. Making the egress rules survive a reboot

`scripts/restrict-relay-egress` writes host firewall state, and nothing reapplies
it at boot. The relay refuses to start without it, so the stack fails loudly
rather than silently running unrestricted, but on a machine that reboots
unattended that means the stack stays down until someone runs a sudo command by
hand.

One command, once:

```
sudo scripts/install-egress-unit
```

It writes `/etc/systemd/system/relay-egress.service` with this repository's path
in it, enables it, starts it, and prints the resulting rules. After that
`scripts/1.start-stack` notices the unit is active and stops asking for sudo.

```
systemctl status relay-egress     what it did, and when
systemctl restart relay-egress    re-resolve the upstream and reinstall
sudo scripts/install-egress-unit --remove
```

Three details in the unit each describe a way it fails quietly if you write it by
hand instead:

- **`After=network-online.target`.** `apply` resolves `openrouter.ai` and refuses
  to install a rule set if it cannot, because a set with no allowed destination
  denies everything and stops the relay. Ordering only after `docker.service` can
  run before DNS works.
- **It waits for `DOCKER-USER`.** The rules hang off Docker's own chain, which
  `dockerd` creates during startup, and having started is not the same as having
  created it. Installing into a chain that is not there yet leaves the
  `RELAY-EGRESS` chain present but unreferenced: rules that exist and filter
  nothing.
- **`PartOf=docker.service`.** Restarting the Docker daemon flushes its chains
  and takes the reference with it. `PartOf` reinstalls the rules when that
  happens rather than letting them disappear.

The upstream is Cloudflare-fronted and its address set rotates. `apply`
re-resolves every time it runs, which is every boot and every daemon restart; if
the relay starts failing to reach the endpoint, that is the symptom section 0.1
describes, and `systemctl restart relay-egress` is the fix.

Verify after a reboot with `scripts/verify-network-boundaries`, which fails on
the SEC-07 rows if the rules did not come back. That check runs from inside the
containers, so it tests the effect rather than the unit.

## 11. Troubleshooting

| Symptom | Cause | What to do |
| --- | --- | --- |
| `openrouter-relay` exits with `EgressNotRestricted` | The SEC-07 firewall rules are absent | `sudo scripts/restrict-relay-egress apply`. Do not set `RELAY_EGRESS_SELFTEST=0` to silence it |
| `dependency failed to start: container openrouter-relay is unhealthy` | The relay could not read its key, or has no egress | `docker compose logs openrouter-relay`. A 0600 key file owned by another user is the usual cause; see `.env.example` |
| `No OpenRouter credential available` | The key file is missing, empty, or unreadable by uid 10003 | Check `${SECRETS_DIR}/openrouter.key` exists and is mode 644 inside a 700 directory |
| Login sits at `prompt: name`, `turn_state: OBSERVING` | A connect raced the game server's startup | `mud_disconnect` then `mud_connect` |
| `mud_act` returns `not_ready` repeatedly | The world has not settled, or a command is still in flight | Call `mud_observe`. If `link_state` is not `connected`, the session has stopped; read `stop_reason` |
| `mud_act` returns `no_progress` or `rejection_loop` | The session controller stopped a loop | Reconnect to start a fresh budget. Look at what the model was repeating before raising the limits |
| `learn_*` returns `store_quarantined` | Stored content failed revalidation | Section 2 |
| The agent says it cannot reach the game | It never could: `hermes-player` shares no network with `dikumud` | This is `SEC-06` working. The game is reached only through MCP |
| Sessions end with HTTP 502 or 429 | The free endpoint is unavailable or a relay budget is spent | 502 is upstream; retry later. 429 with `session_*_exhausted` means restart the relay to begin a new bounded session |
| Turns are about three seconds apart and `/metrics` shows `rate.waits` climbing | The free tier allows 20 requests a minute and the agent wants more, so the relay is pacing rather than refusing | Working as designed. The spectator's model panel says `paced`. Raising `RELAY_MAX_RPM` does not help: 20 is OpenRouter's cap for free variants, so a higher number only moves the refusal upstream. A paid variant has no platform request cap |
| Sessions die after about a minute with `rate limited` in the client log | A wait longer than `RELAY_MAX_RATE_WAIT` still refuses, or waiting is disabled | Check `RELAY_MAX_RATE_WAIT` in `.env`; 0 means refuse immediately. Otherwise the demand is far past 20 a minute and the model is answering faster than the tier allows |
| Every turn takes ~45s longer than usual, and `/metrics` shows `fallbacks` climbing | A model ahead in the order is unavailable and each turn waits out its deadline before a later one answers | Working as designed. Check `models.last` to see which model is answering, and `scripts/set-model-order` to put it first while the other is down. `RELAY_PRIMARY_TIMEOUT=5` cuts the wasted wait instead; restore it afterwards. Note that this cost is paid once per attempt, so it doubles with three models ordered |
| The relay returns `The model endpoint returned status 404` immediately | A pinned model or its provider tag no longer matches the catalog | Check both with `curl -s https://openrouter.ai/api/v1/models/<model>/endpoints \| jq '.data.endpoints[].tag'`. Routing matches the tag (`nvidia`), not the display name (`Nvidia`); a wrong tag 404s exactly like a missing model |
| `hermes chat` reports a missing toolset or a new tool | The agent image changed | Re-run the tool inventory. A toolset Hermes has not seen arrives enabled |
| `openrouter-relay` exits at once with a line naming `models.toml` | The model configuration is missing or invalid | The message names the file and the key. Section 12. A bind source that does not exist arrives as a directory, which is the usual cause |
| `1.start-stack` fails saying the relay and the profile name different models | The seed profile was not updated | Section 12, step 5 |

## 12. Changing the pinned models

The set the relay may request is `config/relay/models.toml`, mounted read-only
into the relay and read once at startup. There is no model identifier in the
relay's source, so this file is the only place a model is named, and
`RELAY_MODEL_CONFIG_FILE` in `.env` can point it outside the repository.

### 12.1 Check the model before you pin it

The relay will request whatever this file names. It cannot tell you that a model
is unsuitable, and most of the ways a model is unsuitable look like a broken
agent rather than a configuration problem. Work through this first, the way
`DEPENDENCY_RECORD.md` does for the shipped set:

| Check | Why it matters |
| --- | --- |
| The id matches exactly in `GET /api/v1/models` | A near-miss id is a 404, which is indistinguishable from an outage |
| `tools` and `tool_choice` are in `supported_parameters` | An agent that cannot emit a tool call cannot play at all: it narrates instead of acting |
| So are the other seven: `include_reasoning`, `max_tokens`, `reasoning`, `reasoning_effort`, `seed`, `temperature`, `top_p` | The same validated body goes to whichever model answers, so every ordered model must accept the same parameters |
| `reasoning` is supported, and what its `supported_efforts` are | The relay sends `reasoning: {exclude: true}`; the client clamps its effort to what the catalog advertises |
| The provider **tag** from `endpoints[].tag` | Routing matches the tag (`nvidia`), never the display name (`Nvidia`), which is accepted and matches nothing |
| Context length and max completion tokens | They become the catalog entry the client reads |
| Pricing | A `:free` id is not a promise, and a paid id spends real money every turn |
| Endpoint status and recent uptime | The reason the fallback exists |

Then prove the part that matters most against the real endpoint:

```
OPENROUTER_API_KEY_FILE=${SECRETS_DIR}/openrouter.key \
  scripts/verify-openrouter-tool-calling <model-id>
```

A `RESULT: PASS` line means that model emitted a well-formed tool call through
the same request shape the relay uses. Run it for every id you are about to put
in the file, including the ones later in the order.

### 12.2 Make the change

**To change which model leads, that is the whole job:**

```
scripts/set-model-order
```

With no arguments it asks, offering every ordering of the configured models with
the current one marked:

```
  1)  nemotron-3-ultra-550b-a55b, then nemotron-3-super-120b-a12b, then nemotron-3.5-lightning   (current)
  2)  nemotron-3-ultra-550b-a55b, then nemotron-3.5-lightning, then nemotron-3-super-120b-a12b
  3)  nemotron-3-super-120b-a12b, then nemotron-3-ultra-550b-a55b, then nemotron-3.5-lightning
  4)  nemotron-3-super-120b-a12b, then nemotron-3.5-lightning, then nemotron-3-ultra-550b-a55b
  5)  nemotron-3.5-lightning, then nemotron-3-ultra-550b-a55b, then nemotron-3-super-120b-a12b
  6)  nemotron-3.5-lightning, then nemotron-3-super-120b-a12b, then nemotron-3-ultra-550b-a55b
  q)  leave it as it is
```

Three models is six orderings, which fits on a screen. Four would be twenty-four,
which does not, so above a threshold the menu falls back to one option per model,
leading, and the rest stay reachable by naming them. Name an exact order when you
want fewer models playing than the file defines:

```
scripts/set-model-order lightning ultra
```

It rewrites the `order` line, sets the agent profile's `model.default` to match,
recreates the relay and prints the ids it came back with. Models left out of the
order stay configured and do not play. Nothing else below is needed.

**To add a model to the set:**

1. Work through 12.1 for it, including the tool-calling check.
2. Add a `[models."<id>"]` table to `config/relay/models.toml` with the values you
   just verified, including `supported_parameters`, and put its id in `order`
   with `scripts/set-model-order`. A model that does not advertise `tools` and
   `tool_choice` is refused at startup rather than at the first turn, and an id
   in `order` with no table is refused too.
3. Restart the relay and read the first lines of its log. It prints the file it
   loaded and the ids it holds, which is the only place both are stated:

```
docker compose up -d --force-recreate openrouter-relay
docker compose logs openrouter-relay | head -5
```

4. If the relay exits instead, the log line names the file and the key at fault.
   It never starts on a policy it could not read.
5. Update the agent's model to match, in both copies, if the new model is now
   first. `scripts/set-model-order` writes the seed at
   `config/hermes-profile/config.yaml` for you; the authoritative runtime copy is
   `$HERMES_HOME/config.yaml` on the `hermes-player` volume, which Hermes
   normalises and rewrites at startup, and which no script touches. Edit it in
   place and restart the agent, or reseed it the way section 9 describes; then
   confirm what took effect:

```
docker compose exec hermes-player sh -c 'grep -A2 "^model:" "$HERMES_HOME/config.yaml"'
docker compose restart hermes-player
```

   `scripts/1.start-stack` compares the seed with the first ordered model and
   refuses to start the stack when they disagree, so a forgotten edit stops the
   next start rather than the next session.

6. Record it: the capability table above for the new endpoint, added to
   `DEPENDENCY_RECORD.md`. That record, not a rebuild, is the change control.

### 12.3 What does not change

The agent still sees one model. `/v1/models` advertises the first ordered model
alone, `/healthz` names it alone, and the configuration file is not visible from
`hermes-player`, which `scripts/verify-network-boundaries` tests as an SEC-09
row. `/metrics` names the model that served each request, which is deliberate:
the operator has to be able to see which one answered.
