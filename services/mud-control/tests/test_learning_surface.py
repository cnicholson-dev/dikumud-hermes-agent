"""The learning MCP surface: six tools, no escape hatch, every result audited.

The MUD endpoint's inventory is asserted in `test_server_surface.py` and stays
at five. This file does the same job for the second endpoint, so neither surface
can grow without a test failing.
"""
import json

import pytest

from mud_control.audit import AuditLog
from mud_control.learning import schema
from mud_control.learning.server import TOOL_NAMES, build_learning_server
from mud_control.learning.store import LearningStore

FACT = "The cityguard in Temple Square kills a level 1 character in four rounds."
PROCEDURE = """## After dying

- Wake in the temple and check score before anything else.
- Rest until hit points are comfortable, not merely positive.
- Walk back and take everything from the corpse in one go.
"""


@pytest.fixture
def store(tmp_path):
    audit = AuditLog(tmp_path / "audit.jsonl")
    yield LearningStore(tmp_path / "learning", audit)
    audit.close()


@pytest.fixture
def server(store):
    return build_learning_server(store)


async def call(_server, _tool, **arguments):
    # Underscored parameters because two of the tools take an argument called
    # "name", which would otherwise collide with this helper's own signature.
    result = await _server.call_tool(_tool, arguments)
    assert result.is_error in (False, None), result.content
    # A tool returning a plain dict is delivered as JSON text with the dict
    # under "result" in structured_content. Phase 3 recorded the same shape.
    payload = result.structured_content
    if payload is None:
        return json.loads(result.content[0].text)
    return payload.get("result", payload)


def audit_events(store):
    path = store._audit.path  # noqa: SLF001
    return [json.loads(line) for line in path.read_text().splitlines() if line]


# -- inventory ----------------------------------------------------------

@pytest.mark.asyncio
async def test_exactly_six_learning_tools_are_exposed(server):
    names = sorted(t.name for t in await server.list_tools())
    assert names == sorted(TOOL_NAMES)
    assert len(names) == 6


@pytest.mark.asyncio
async def test_no_generic_escape_hatch_tool_exists(server):
    names = {t.name for t in await server.list_tools()}
    forbidden = {
        "shell", "exec", "run", "system", "subprocess", "proxy", "fetch",
        "http", "request", "socket", "telnet", "tintin", "send_raw",
        "read_file", "write_file", "eval", "python", "skill_manage",
    }
    assert not (names & forbidden)
    for name in names:
        assert name.startswith("learn_")


@pytest.mark.asyncio
async def test_no_tool_accepts_a_path_target_or_credential(server):
    forbidden = {
        "host", "hostname", "address", "ip", "port", "url", "endpoint",
        "target", "password", "credential", "secret", "token", "path",
        "file", "filename", "directory", "config", "command", "argv", "env",
        "format", "template",
    }
    for tool in await server.list_tools():
        params = set((tool.input_schema or {}).get("properties", {}))
        assert not (params & forbidden), f"{tool.name} exposes {params & forbidden}"


@pytest.mark.asyncio
async def test_the_schema_bounds_content_before_our_code_sees_it(server):
    tools = {t.name: (t.input_schema or {}) for t in await server.list_tools()}

    remember = tools["learn_remember"]["properties"]
    assert remember["fact"]["maxLength"] == schema.MAX_FACT_CHARS
    assert remember["fact"]["minLength"] == schema.MIN_FACT_CHARS
    assert "fact" in tools["learn_remember"].get("required", [])

    save = tools["learn_procedure_save"]["properties"]
    assert save["guidance"]["maxLength"] == schema.MAX_PROCEDURE_CHARS
    assert save["name"]["maxLength"] == 32
    assert set(tools["learn_procedure_save"].get("required", [])) == {
        "name", "title", "guidance"}


@pytest.mark.asyncio
async def test_recall_takes_no_arguments(server):
    tool = next(t for t in await server.list_tools() if t.name == "learn_recall")
    assert (tool.input_schema or {}).get("properties", {}) == {}


# -- behaviour through the tools ----------------------------------------

@pytest.mark.asyncio
async def test_a_fact_stored_through_the_tool_comes_back_through_recall(server):
    stored = await call(server, "learn_remember", fact=FACT, tags=["danger"])
    assert stored["stored"] is True
    assert stored["id"] == "fact-0001"

    recalled = await call(server, "learn_recall")
    assert recalled["facts"][0]["text"] == FACT
    assert recalled["facts_used"] == 1


@pytest.mark.asyncio
async def test_a_procedure_round_trips_through_the_tools(server):
    saved = await call(server, "learn_procedure_save", name="after-dying",
                       title="After dying", guidance=PROCEDURE)
    assert saved == {"stored": True, "name": "after-dying", "replaced": False,
                     "updated_at": saved["updated_at"]}

    read = await call(server, "learn_procedure_read", name="after-dying")
    assert read["guidance"] == PROCEDURE
    assert read["title"] == "After dying"


@pytest.mark.asyncio
async def test_a_refused_write_returns_a_reason_the_model_can_act_on(server):
    result = await call(server, "learn_remember",
                        fact="Use sudo to heal before walking home again now.")
    assert result["stored"] is False
    assert result["error"] == "executable_content"
    assert "inert prose" in result["detail"]

    result = await call(server, "learn_procedure_save", name="escape-plan",
                        title="Escaping",
                        guidance=PROCEDURE + "\nThen run curl to get the map.\n")
    assert result["error"] == "executable_content"


@pytest.mark.asyncio
async def test_a_hostile_procedure_name_never_reaches_the_filesystem(server, store):
    result = await call(server, "learn_procedure_save", name="../../etc/passwd",
                        title="Escaping", guidance=PROCEDURE)
    assert result["error"] == "bad_name"
    # Only the two fixed documents exist, and neither was created by this call.
    assert [p.name for p in store.root.iterdir()] == []


@pytest.mark.asyncio
async def test_a_quarantined_store_fails_closed_through_every_tool(server, store):
    await call(server, "learn_remember", fact=FACT, tags=["danger"])
    store.facts_path.write_text("{not json")

    for name, arguments in (
        ("learn_recall", {}),
        ("learn_remember", {"fact": FACT}),
        ("learn_forget", {"fact_id": "fact-0001"}),
    ):
        result = await call(server, name, **arguments)
        assert result["error"] == "store_quarantined", name
        assert "operator" in result["detail"]


# -- LEARN-08 audit -----------------------------------------------------

@pytest.mark.asyncio
async def test_every_mutation_through_the_tools_is_audited(server, store):
    await call(server, "learn_remember", fact=FACT, tags=["danger"])
    await call(server, "learn_remember",
               fact="Use sudo to heal before walking home again now.")
    await call(server, "learn_procedure_save", name="after-dying",
               title="After dying", guidance=PROCEDURE)
    await call(server, "learn_procedure_delete", name="after-dying")
    await call(server, "learn_forget", fact_id="fact-0001")

    events = [e["event"] for e in audit_events(store)]
    assert events == [
        "learning_fact_added",
        "learning_rejected",
        "learning_procedure_saved",
        "learning_procedure_removed",
        "learning_fact_removed",
    ]


@pytest.mark.asyncio
async def test_reading_is_not_audited_but_writing_is(server, store):
    await call(server, "learn_recall")
    await call(server, "learn_procedure_read", name="nothing-here")
    assert audit_events(store) == []


@pytest.mark.asyncio
async def test_no_stored_or_refused_content_appears_in_the_audit(server, store):
    hostile = "Use sudo to heal before walking home again now, every time."
    await call(server, "learn_remember", fact=FACT, tags=["danger"])
    await call(server, "learn_remember", fact=hostile)
    await call(server, "learn_procedure_save", name="after-dying",
               title="After dying", guidance=PROCEDURE)

    raw = store._audit.path.read_text()  # noqa: SLF001
    assert FACT not in raw
    assert hostile not in raw
    assert "corpse" not in raw          # from the procedure body
    assert "sudo" not in raw
    # What is there instead: reason, size and digest.
    rejected = [e for e in audit_events(store) if e["event"] == "learning_rejected"][0]
    assert rejected["digest"].startswith("sha256:")
    assert rejected["chars"] == len(hostile)
