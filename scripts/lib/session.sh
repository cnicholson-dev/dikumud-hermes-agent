#!/usr/bin/env bash
# Copyright (C) 2026 Cody Nicholson
# SPDX-License-Identifier: LGPL-2.1-only
#
# Ending a play session, shared by the scripts that have to end one.
#
# Extracted from scripts/3.stop-stack, which was the only caller until
# scripts/4.load-character needed the same three steps: a character switch has
# to close the session that is open before it changes who the credential file
# names, for the same reason a teardown does.
#
# The reasoning that was in 3.stop-stack's header belongs with the code:
#
#   A session that is still open when its containers stop ends as a transport
#   fault, because that is what it is: the client process went away
#   mid-connection. mud-control has a signal for the other case, from
#   services/mud-control/src/mud_control/server.py:
#
#       signal.signal(signal.SIGUSR1,
#                     lambda *_: session.stop(StopReason.OPERATOR))
#
#   so a stop that goes through here is recorded as `operator` in the audit
#   record and shown as such by the spectator. That signal does not close the
#   game link, and it cannot: `quit` is sent only by `mud_disconnect`, which is
#   an MCP tool the host has no route to.
#
# Sourced, not executed. Callers provide `say` and `fail`, which they all have
# already, and are expected to have cd'd to the repository root.

AUDIT="${AUDIT:-/var/log/mud-control/audit.jsonl}"

running() {
  [ "$(docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null || echo false)" = true ]
}

audit_tail() {
  docker compose exec -T mud-control sh -c "tail -n 400 $AUDIT" 2>/dev/null || true
}

#: The id of the session the audit record is currently in, or empty. Every
#: event carries one, so this is what a capture is named after and what tells
#: two scripts they are looking at the same session rather than at two.
current_session_id() {
  audit_tail |
    grep -oE '"session_id":"[A-Za-z0-9_-]+"' |
    tail -n 1 | cut -d'"' -f4 || true
}

#: The character mud-control is configured for, read from the container rather
#: than from .env: the container's value is the one that is actually playing.
running_character() {
  docker compose exec -T mud-control printenv MUD_CONTROL_CHARACTER 2>/dev/null |
    tr -d '\r\n' || true
}

# `hermes chat` is a foreground process in the agent container, one per
# session. Stopping it first means no further command can be submitted while
# the boundary is being closed down.
stop_client() {
  if ! running hermes-player; then
    say "client" "hermes-player is not running"
    return
  fi
  if ! docker exec hermes-player pgrep -f "hermes chat" >/dev/null 2>&1; then
    say "client" "no session process"
    return
  fi

  say "client" "stopping hermes chat"
  docker exec hermes-player pkill -f "hermes chat" >/dev/null 2>&1 || true
  local _attempt
  for _attempt in $(seq 1 20); do
    if ! docker exec hermes-player pgrep -f "hermes chat" >/dev/null 2>&1; then
      say "" "stopped"
      return
    fi
    sleep 0.5
  done
  docker exec hermes-player pkill -9 -f "hermes chat" >/dev/null 2>&1 || true
  say "" "stopped, after SIGKILL"
}

# The audit record is the only authority on whether a session is open: the
# lifecycle events are session_opened, session_resumed, session_closed and
# session_stopped, and the most recent of them says which state the boundary is
# in. This is the same source scripts/spectate reads.
end_session() {
  if ! running mud-control; then
    say "session" "mud-control is not running"
    return
  fi

  local last
  last="$(audit_tail |
    grep -oE '"event":"session_(opened|resumed|closed|stopped)"' |
    tail -n 1 || true)"

  case "$last" in
    *session_opened*|*session_resumed*) ;;
    "")
      say "session" "no session in the audit record"
      return
      ;;
    *)
      say "session" "already ended"
      return
      ;;
  esac

  say "session" "open; recording an operator stop at the boundary"
  docker compose kill -s USR1 mud-control >/dev/null 2>&1 ||
    fail "could not signal mud-control"
  sleep 2

  local reason
  reason="$(audit_tail |
    grep '"event":"session_stopped"' | tail -n 1 |
    grep -oE '"reason":"[a-z_]+"' | cut -d'"' -f4 || true)"
  if [ -n "$reason" ]; then
    say "" "recorded stop reason: $reason"
  else
    # Not a failure: a session that was already stopped for another reason, or
    # one whose stop was recorded before this tail window, both land here.
    say "" "no stop reason recorded; continuing"
  fi
}
