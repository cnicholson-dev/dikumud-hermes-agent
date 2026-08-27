# Clean-room reproduction

Everything needed to go from an empty machine to a supervised demonstration.
Written to be followed literally: if a step here is not enough to carry out, that
is a defect in this document, not something to work out from the code.

## What you need

- Linux with Docker and Compose v2 (`docker compose version` >= 2.20).
- Root on the host, once, for the SEC-07 firewall rules.
- About 5 GB of disk: the agent image is ~3.9 GB, the rest are small.
- An OpenRouter API key with access to every model named in
  `config/relay/models.toml`. A clean clone ships three, in this order:
  `nvidia/nemotron-3-ultra-550b-a55b:free`,
  `nvidia/nemotron-3-super-120b-a12b:free`,
  `nvidia/nemotron-3.5-lightning:free`. The relay tries them in that order and
  needs the later ones when an earlier one is unavailable. The first model's free
  endpoint has reported a degraded status before now, so expect any of them to
  answer; `/metrics` and the spectator both name whichever one did. To reproduce
  this document exactly, leave that file alone: it is where the set and the order
  are kept, and `scripts/set-model-order` is what changes which model leads.

No Python, no build tools and no MUD client on the host. Everything compiles
inside the images, including DikuMUD and TinTin++ from pinned commits.

## 1. Get the repository

```bash
git clone https://github.com/cnicholson-dev/dikumud-hermes-agent.git
cd dikumud-hermes-agent
```

## 2. Create the secrets

Two files outside the repository. The directory is the access control, which is
why it is 0700 and the files are not; `.env.example` explains why in full.

```bash
mkdir -p -m 700 ~/.config/dikumud-hermes

# The character. The name is not secret; the password on the second line is.
# DikuMUD rejects passwords longer than 10 characters.
printf '%s\n%s\n' 'Wren' 'pick-a-pass' > ~/.config/dikumud-hermes/game-credential

# The API key, one line.
printf '%s\n' 'sk-or-v1-REPLACE-ME' > ~/.config/dikumud-hermes/openrouter.key

chmod 644 ~/.config/dikumud-hermes/game-credential ~/.config/dikumud-hermes/openrouter.key
```

The character need not exist yet. If the game does not know the name, the agent
creates it during the first session: `mud-control` answers the identity prompts
and the model chooses sex and class itself.

## 3. Configure

```bash
cp .env.example .env
```

Edit `.env` and set `SECRETS_DIR` to the full path of the directory from step 2
(no `~`, Compose does not expand it), and `MUD_CHARACTER` to the name on the
first line of `game-credential`.

## 4. Build and start

```bash
docker compose up -d --build
```

First build takes several minutes: it compiles DikuMUD and TinTin++ from pinned
commits and pulls the agent image by digest. When it finishes, all four services
should read `(healthy)`:

```bash
docker compose ps
```

If `hermes-player` never starts and Compose says a dependency is unhealthy, that
is the health gating working. Read the failing service's logs; the most likely
cause is step 5.

## 5. Apply the egress restriction

```bash
sudo scripts/restrict-relay-egress apply
```

This is required, not optional. The relay checks at startup that it cannot reach
the open internet and exits if it can, so without this the stack will not run. It
is host firewall state and does not survive a reboot on its own; `OPERATIONS.md`
section 10 has a systemd unit for that.

## 6. Verify the boundary

```bash
scripts/verify-network-boundaries
```

21 checks from inside the containers, and it must exit 0. This is the evidence
behind the project's isolation claims, and it takes about a minute.

Optionally, run the test suites from the images the deployment runs:

```bash
docker build -q -f services/mud-control/Dockerfile.test -t mud-control:test services/mud-control
docker run --rm -v "$PWD/tests:/tests:ro" mud-control:test python -m pytest
docker build -q -f services/openrouter-relay/Dockerfile.test -t openrouter-relay:test services/openrouter-relay
docker run --rm openrouter-relay:test python -m pytest
```

Expect 341 and 166 passing. The `mud-control` mount is required: its
`conftest.py` resolves the PTY fixtures to `/tests/fixtures/tintin`, which live
at the repository root, and eleven tests fail with `FileNotFoundError` without
it.

## 7. Play a session

```bash
docker compose exec hermes-player hermes chat --max-turns 30 \
  -q "Read your notes with learn_recall, then connect and explore. Before you stop, store anything worth remembering."
```

In another terminal, watch it:

```bash
scripts/spectate
```

The spectator shows the commands with their stated intents, the session state and
stop reason, what the agent has learned, and the model's request and token
counts. It shows no credentials, because its sources contain none.

A first session on a new character spends its first two commands answering the
game's questions about sex and class.

## 8. Prove that the learning persists

Stop and restart everything, then ask the agent who it is:

```bash
docker compose restart
docker compose exec hermes-player hermes chat --max-turns 8 \
  -q "Who are you and what are you trying to do? Use learn_recall to ground it."
```

It should describe itself, name the places and facts it actually recorded, and
name nothing else. The store it reads was revalidated record by record on load.

## What can go wrong

The endpoint is a free tier. Sessions ending with HTTP 502, or with
`session_duration_exhausted` after the relay has been up two hours, are the
external dependency rather than the stack. On an availability failure the relay
retries once per model down the operator's order, and fails closed with an
explicit stop reason once that order is exhausted; it never reaches for a model
outside the configured set. A session that is slow rather than stopped may be
being paced instead: the spectator prints a `paced` line once the relay has
waited on the upstream's rate window. Restart the relay to begin a new bounded
session.

`OPERATIONS.md` section 11 has the rest of the symptoms.
