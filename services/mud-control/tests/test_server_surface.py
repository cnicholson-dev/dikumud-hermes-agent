"""MCP-01 and MCP-08: the exposed surface is exactly five tools with no target.

A generic proxy, subprocess, socket, Telnet or TinTin++ command tool must never
appear on this surface. These tests fail if one does, so the prohibition is
enforced by the suite rather than by intention.
"""
from pathlib import Path

import pytest

from mud_control.audit import AuditLog
from mud_control.server import TOOL_NAMES, build_server
from mud_control.session import MudSession
from mud_control.transport import TintinTransport, TransportConfig


@pytest.fixture
def server(tmp_path):
    cfg = TransportConfig(host="dikumud", port=4000, character="Wren",
                          credential_path=tmp_path / "cred")
    (tmp_path / "cred").write_text("Wren\nnot-a-real-password\n")
    audit = AuditLog(tmp_path / "audit.jsonl")
    return build_server(MudSession(TintinTransport(cfg), audit=audit), audit)


@pytest.mark.asyncio
async def test_exactly_five_tools_are_exposed(server):
    names = sorted(t.name for t in await server.list_tools())
    assert names == sorted(TOOL_NAMES)
    assert len(names) == 5


@pytest.mark.asyncio
async def test_no_generic_escape_hatch_tool_exists(server):
    names = {t.name for t in await server.list_tools()}
    forbidden = {
        "shell", "exec", "run", "system", "subprocess", "proxy", "fetch",
        "http", "request", "socket", "telnet", "tintin", "send_raw",
        "read_file", "write_file", "eval", "python",
    }
    assert not (names & forbidden)
    # Nothing whose name merely hints at a raw channel, either.
    for name in names:
        assert name.startswith("mud_")


@pytest.mark.asyncio
async def test_no_tool_accepts_a_target_identity_or_credential(server):
    """MCP-08: connection details stay in trusted configuration."""
    forbidden = {
        "host", "hostname", "address", "ip", "port", "url", "endpoint",
        "target", "character", "name", "user", "username", "password",
        "credential", "credentials", "secret", "token", "path", "file",
        "filename", "config", "shell", "argv", "env",
    }
    for tool in await server.list_tools():
        params = set((tool.input_schema or {}).get("properties", {}))
        assert not (params & forbidden), f"{tool.name} exposes {params & forbidden}"


@pytest.mark.asyncio
async def test_mud_connect_takes_no_arguments_at_all(server):
    tool = next(t for t in await server.list_tools() if t.name == "mud_connect")
    assert (tool.input_schema or {}).get("properties", {}) == {}


@pytest.mark.asyncio
async def test_mud_act_bounds_the_command_in_its_schema(server):
    tool = next(t for t in await server.list_tools() if t.name == "mud_act")
    props = (tool.input_schema or {}).get("properties", {})
    assert set(props) == {"session_id", "command", "intent"}
    # Schema-level bound as well as the validator, so an oversize command is
    # refused before it reaches any of our code.
    assert props["command"].get("maxLength") == 80


@pytest.mark.asyncio
async def test_every_tool_requires_a_session_except_connect(server):
    for tool in await server.list_tools():
        params = set((tool.input_schema or {}).get("properties", {}))
        if tool.name == "mud_connect":
            assert params == set()
        else:
            assert "session_id" in params, tool.name


@pytest.mark.asyncio
async def test_mud_act_requires_an_intent_in_its_schema(server):
    tool = next(t for t in await server.list_tools() if t.name == "mud_act")
    schema = tool.input_schema or {}
    required = set(schema.get("required", []))
    assert {"session_id", "command", "intent"} <= required, (
        "intent must be required, not optional with a default: Phase 5 showed "
        "an optional intent is never supplied"
    )
    assert schema["properties"]["intent"].get("minLength", 0) >= 1
