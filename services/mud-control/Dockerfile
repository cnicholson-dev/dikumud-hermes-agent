# syntax=docker/dockerfile:1
#
# mud-control - owns the TinTin++ client and the PTY that drives it.
#
# Trust boundary: holds the game credential, talks to dikumud over net_mcp_game
# and serves MCP on net_hermes_mcp. It has no egress and never returns
# credentials. See public-documentation/DESIGN.md sections 4.4 and 5.2.
#
# Two MCP endpoints on net_hermes_mcp, neither published to the host:
#   8765  the five MUD tools
#   8766  the six learning tools

ARG PYTHON_BASE=python:3.12-slim@sha256:2c941e860699f878900b0edc2403613c234d4b32eda3cc9fa7036991a2a63c4a

# ---------------------------------------------------------------------------
# Builder: compile the pinned TinTin++
# ---------------------------------------------------------------------------
FROM ${PYTHON_BASE} AS tintin-builder

ARG TINTIN_COMMIT=010881e7689c5328096f0b63002b898104cd72f1
ARG TINTIN_REPO=https://github.com/scandum/tintin.git

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      build-essential git ca-certificates \
      zlib1g-dev libpcre2-dev libssl-dev \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /build
RUN git init -q . \
 && git remote add origin "${TINTIN_REPO}" \
 && git fetch -q --depth 1 origin "${TINTIN_COMMIT}" \
 && git checkout -q FETCH_HEAD \
 && test "$(git rev-parse HEAD)" = "${TINTIN_COMMIT}" \
 && echo "verified TinTin++ commit ${TINTIN_COMMIT}"

# The tag and the source must agree; CLIENT_VERSION is the authority.
RUN grep -q '#define CLIENT_VERSION *"2\.02\.61 *"' src/tintin.h \
 && cd src && ./configure >/dev/null && make >/dev/null \
 && test -x tt++ && ./tt++ -V | head -2

# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------
FROM ${PYTHON_BASE}

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      zlib1g libpcre2-8-0 libssl3 \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --gid 10002 mudctl \
 && useradd --uid 10002 --gid 10002 --no-create-home --shell /usr/sbin/nologin mudctl

COPY --from=tintin-builder /build/src/tt++ /opt/mud-control/bin/tt++

# TinTin++ is GPL-3.0-or-later. Preserve its licence and notices; see
# THIRD_PARTY_NOTICES.md for the source-availability obligation that attaches
# if this image is ever distributed.
COPY --from=tintin-builder /build/COPYING /opt/mud-control/licenses/tintin/COPYING
COPY --from=tintin-builder /build/CREDITS /opt/mud-control/licenses/tintin/CREDITS

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --require-hashes -r /tmp/requirements.txt \
 && rm /tmp/requirements.txt

COPY config/ /opt/mud-control/config/
COPY src/ /opt/mud-control/src/

# TinTin++ reads no default rc file, but an empty HOME removes any doubt.
# It is also where the harness writes the generated session file, so it must
# be a tmpfs at runtime.
ENV HOME=/tmp/mudctl \
    PYTHONPATH=/opt/mud-control/src \
    PYTHONUNBUFFERED=1 \
    MUD_CONTROL_TINTIN=/opt/mud-control/bin/tt++ \
    MUD_CONTROL_SESSION_CONFIG=/opt/mud-control/config/session.tin \
    MUD_CONTROL_AUDIT=/var/log/mud-control/audit.jsonl \
    MUD_CONTROL_LEARNING=/var/lib/mud-control/learning \
    MUD_CONTROL_MCP_PORT=8765 \
    MUD_CONTROL_LEARN_PORT=8766

# The audit record lives on its own volume, mounted only into this service.
# hermes-player never receives it, which is what SEC-09 checks.
RUN mkdir -p /var/log/mud-control && chown 10002:10002 /var/log/mud-control
VOLUME ["/var/log/mud-control"]

# The learning store is a separate volume from the audit record:
# one is append-only evidence, the other is rewritten on every mutation, and
# neither belongs in the other's failure domain. hermes-player receives
# neither; the agent reaches this store only through the MCP tools on 8766.
RUN mkdir -p /var/lib/mud-control/learning \
 && chown -R 10002:10002 /var/lib/mud-control
VOLUME ["/var/lib/mud-control/learning"]

USER 10002:10002
WORKDIR /opt/mud-control
EXPOSE 8765 8766

# Both endpoints, because a service that serves one surface and not the other
# is not healthy for this stack: hermes-player is configured with two MCP
# servers and fails its own startup discovery if either refuses a connection.
# A TCP accept is the right depth here. An MCP call would need a session and a
# request id, which is a lot of moving parts to run every fifteen seconds, and
# the process serves both ports from one event loop, so a port that accepts is
# a loop that is running.
# Probes whatever the service was told to bind, not loopback. Phase 7 binds
# the listeners to this container's address on the agent's network so the game
# container cannot reach them, and a health check hardcoded to 127.0.0.1 then
# reports unhealthy against a service that is working perfectly.
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import os,socket;h=os.environ.get('MUD_CONTROL_BIND','127.0.0.1');h='127.0.0.1' if h=='0.0.0.0' else h;[socket.create_connection((h,p),3).close() for p in (8765,8766)]" \
      || exit 1

CMD ["python", "-m", "mud_control.server"]
