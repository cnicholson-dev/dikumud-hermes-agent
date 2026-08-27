# Copyright (C) 2026 Cody Nicholson
# SPDX-License-Identifier: LGPL-2.1-only
"""The MCP boundary: exactly five tools, and nothing else.

One rule is categorical about what must never appear here: no generic proxy, no
generic subprocess tool, no generic socket tool, no generic Telnet tool, and no
generic TinTin++ command tool. `SECURITY.md` section 3 states it as a trust
boundary this service must never expose.

So the surface is five named tools over private Streamable HTTP. There is no
tool that takes a host, a port, a path, a shell fragment, or a raw client
command. `mud_act` takes one game command and a short intent string, and the
command must survive `validate.validate()` unchanged.

Trusted configuration is read from the environment at startup and is never
influenced by a caller. No tool accepts a target, an identity, or a credential
source, which is what MCP-08 tests.
"""

from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from .audit import AuditLog
from .learning.server import TOOL_NAMES as LEARNING_TOOL_NAMES
from .learning.server import build_learning_server
from .learning.store import LearningStore
from .limits import SessionLimits, StopReason
from .playlog import DEFAULT_MAX_BYTES, PlayLog
from .session import MudSession, SessionError, TurnState
from .transport import TintinTransport, TransportConfig

#: The complete tool surface. Asserted in MCP-01 so an accidental addition
#: fails a release-blocking test rather than reaching the model.
TOOL_NAMES = ("mud_connect", "mud_observe", "mud_act", "mud_status",
              "mud_disconnect")


def build_server(session: MudSession, audit: AuditLog) -> MCPServer:
    """Register the five tools against an already-constructed session."""
    server = MCPServer(
        "mud-control",
        instructions=(
            "Play one DikuMUD character. Send exactly one game command per "
            "mud_act call, only while the turn state is READY. Observe the "
            "result before acting again."
        ),
    )

    @server.tool(
        name="mud_connect",
        description=(
            "Open the game session and authenticate. The character, host and "
            "port are fixed by trusted configuration and cannot be supplied."
        ),
    )
    async def mud_connect() -> dict:
        try:
            return await session.connect()
        except SessionError as err:
            return {"error": err.reason, "detail": err.detail}
        except Exception as err:  # noqa: BLE001
            # Fail closed with an explicit, non-leaking result. An exception is
            # never swallowed without producing a safe state, and the raw text
            # of an upstream error is untrusted.
            audit.record("connect_failed", error_type=type(err).__name__)
            return {"error": "connect_failed", "detail": type(err).__name__}

    @server.tool(
        name="mud_observe",
        description=(
            "Return new game output. Waits briefly for the world to settle. "
            "Observing never authorises an action."
        ),
    )
    async def mud_observe(
        session_id: Annotated[str, Field(description="Id from mud_connect")],
        wait_seconds: Annotated[float, Field(ge=0.0, le=30.0)] = 5.0,
    ) -> dict:
        try:
            obs = await session.observe(session_id, timeout=wait_seconds)
        except SessionError as err:
            return {"error": err.reason, "detail": err.detail}
        return {
            "text": obs.text,
            "turn_state": session.turn_state.value,
            "prompt": obs.prompt.value,
            "more_unread": obs.truncated,
            "transport_events": obs.events,
        }

    @server.tool(
        name="mud_act",
        description=(
            "Send exactly one DikuMUD command, only while the turn state is "
            "READY. Give a short statement of intent. Multiple commands, "
            "client commands and control characters are refused."
        ),
    )
    async def mud_act(
        session_id: Annotated[str, Field(description="Id from mud_connect")],
        command: Annotated[str, Field(max_length=80, description="One game command")],
        # Required, with a floor, and no default. The design requires every
        # gameplay command to carry "one concise visible statement of intent",
        # and Phase 5 showed that an optional field with a default is simply
        # not filled in: the model sent look, read board and look at board with
        # an empty intent every time. Enforced in the schema so the model
        # cannot omit it, rather than asked for in prompt text.
        intent: Annotated[str, Field(
            min_length=3, max_length=200,
            description="One short line, in character, saying why you are "
                        "doing this. Not your reasoning.")],
    ) -> dict:
        try:
            result = await session.act(session_id, command, intent=intent[:200])
        except SessionError as err:
            return {"error": err.reason, "detail": err.detail}
        if not result.accepted:
            return {
                "accepted": False,
                "error": result.reason,
                "detail": result.detail,
                "turn_state": result.turn_state.value,
            }
        return {"accepted": True, "turn_state": result.turn_state.value}

    @server.tool(
        name="mud_status",
        description=(
            "Report turn state, link state, prompt and whether unread output "
            "is waiting. Contains no credentials or connection details."
        ),
    )
    async def mud_status(
        session_id: Annotated[str, Field(description="Id from mud_connect")],
    ) -> dict:
        try:
            session._require_session(session_id)  # noqa: SLF001
        except SessionError as err:
            return {"error": err.reason, "detail": err.detail}
        return session.status()

    @server.tool(
        name="mud_disconnect",
        description="End the game session cleanly.",
    )
    async def mud_disconnect(
        session_id: Annotated[str, Field(description="Id from mud_connect")],
    ) -> dict:
        try:
            return await session.disconnect(session_id)
        except SessionError as err:
            return {"error": err.reason, "detail": err.detail}

    return server


def build_from_env() -> tuple[MCPServer, MCPServer, MudSession, AuditLog,
                              LearningStore]:
    """Assemble both boundaries from trusted configuration only.

    The learning store is a second MCP server on its own port. It
    is built here so both surfaces share one audit record, and it is given a
    directory and that audit log and nothing else: no transport, no session, no
    credential.
    """
    config = TransportConfig.from_env()
    audit = AuditLog(Path(os.environ.get("MUD_CONTROL_AUDIT",
                                         "/var/log/mud-control/audit.jsonl")))
    # The credential is registered for redaction before anything can be
    # written, so no audit event can contain it even if a call site slips.
    try:
        lines = [ln.strip() for ln
                 in config.credential_path.read_text().splitlines() if ln.strip()]
        if len(lines) >= 2:
            audit.register_secret(lines[1])
    except OSError:
        pass

    # The spectator's live view of the game (design section 10). Content
    # rather than evidence, so it is bounded and rotated, and an empty path
    # turns it off for a deployment that does not want a content log at all.
    play_path = os.environ.get("MUD_CONTROL_PLAY_LOG",
                               "/var/log/mud-control/play.log")
    play_log = PlayLog(
        Path(play_path),
        max_bytes=int(os.environ.get("MUD_CONTROL_PLAY_LOG_BYTES",
                                     str(DEFAULT_MAX_BYTES))),
    ) if play_path else None

    transport = TintinTransport(config, play_log=play_log)
    session = MudSession(transport, audit=audit,
                         limits=SessionLimits.from_env(),
                         play_log=play_log)

    # Per character, so a new character starts with nothing to remember and a
    # continuing one picks up its own notes. See LearningStore.root_for.
    store = LearningStore(
        LearningStore.root_for(
            os.environ.get("MUD_CONTROL_LEARNING",
                           "/var/lib/mud-control/learning"),
            config.character,
        ),
        audit,
    )
    return (build_server(session, audit), build_learning_server(store),
            session, audit, store)


async def _serve(mud_server: MCPServer, learning_server: MCPServer) -> None:
    """Run both endpoints in one process.

    Two servers rather than one with eleven tools: the MUD surface stays
    exactly five tools, which is what MCP-01 asserts, and each endpoint has its
    own inventory test. Both bind to the container's own interface on the
    private network; no port is published to the host, and this service sits on
    net_hermes_mcp, which only hermes-player shares.
    """
    host = os.environ.get("MUD_CONTROL_BIND", "0.0.0.0")
    await asyncio.gather(
        mud_server.run_streamable_http_async(
            host=host,
            port=int(os.environ.get("MUD_CONTROL_MCP_PORT", "8765")),
        ),
        learning_server.run_streamable_http_async(
            host=host,
            port=int(os.environ.get("MUD_CONTROL_LEARN_PORT", "8766")),
        ),
    )


def main() -> None:
    mud_server, learning_server, session, audit, store = build_from_env()
    audit.record("service_starting", tools=",".join(TOOL_NAMES),
                 learning_tools=",".join(LEARNING_TOOL_NAMES))
    # Load the store once before serving, so a store that was tampered with
    # while the service was down is found now and recorded, rather than at the
    # moment the agent first asks what it knows.
    store.startup_check()

    # An operator stop that ends the session at the boundary.
    #
    # An operator has to be able to stop a session immediately. Killing the
    # agent process does that, but it leaves the game session open and records
    # nothing about why play ended.
    # This ends it here instead: the next mud_act is refused with
    # operator_stop, the reason reaches the audit and the spectator, and the
    # game link is not left waiting on a client that has gone.
    #
    #     docker compose kill -s USR1 mud-control
    #
    # SIGUSR1 rather than SIGTERM, because stopping a session is not stopping
    # the service: the operator can start another without a restart.
    signal.signal(signal.SIGUSR1,
                  lambda *_: session.stop(StopReason.OPERATOR))

    asyncio.run(_serve(mud_server, learning_server))


if __name__ == "__main__":
    main()
