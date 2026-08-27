"""E2E-01: the agent can answer creation prompts, and only those.

Design section 9 splits a first login in two. The bootstrap owns the part that
carries the secret, the name and password, and the agent makes "all non-secret
character-creation choices - such as race, class, sex, and other ordinary game
prompts - through the same one-command MCP boundary".

Phase 8 found the second half was unreachable: only the in-game prompt returned
READY, so `mud_act` refused every attempt at a sex or class prompt as not_ready
and a new character could never be finished. These tests pin the narrow opening
that fixes it, and the boundary around it.
"""
import pytest

from mud_control.audit import AuditLog
from mud_control.prompts import PromptKind
from mud_control.session import MudSession, TurnState

from test_session import FakeTransport


@pytest.fixture
def audit(tmp_path):
    return AuditLog(tmp_path / "audit.jsonl")


async def at_prompt(kind, audit):
    """A connected session sitting at one prompt."""
    transport = FakeTransport(prompt=kind)
    session = MudSession(transport, audit=audit)
    await session.connect()
    await session.observe(session.session_id, timeout=0)
    return session, transport


# -- the opening --------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [PromptKind.SEX, PromptKind.CLASS])
async def test_a_creation_prompt_gives_the_agent_a_turn(kind, audit):
    session, transport = await at_prompt(kind, audit)
    assert session.turn_state is TurnState.READY

    result = await session.act(session.session_id, "m",
                               intent="choosing how this character starts")

    assert result.accepted
    assert transport.writes == ["m"]


# -- and its boundary ---------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [
    PromptKind.NAME,
    PromptKind.NAME_CONFIRM,
    PromptKind.PASSWORD,
    PromptKind.PASSWORD_NEW,
    PromptKind.PASSWORD_CONFIRM,
])
async def test_an_identity_prompt_never_gives_the_agent_a_turn(kind, audit):
    """The secret-bearing half stays with the trusted side. A turn here would
    invite the model to type at a password prompt."""
    session, transport = await at_prompt(kind, audit)

    assert session.turn_state is TurnState.OBSERVING
    result = await session.act(session.session_id, "guess",
                               intent="trying the identity prompt")

    assert not result.accepted
    assert result.reason == "not_ready"
    assert transport.writes == []


# -- the plumbing the agent cannot answer -------------------------------

@pytest.mark.asyncio
async def test_the_motd_keypress_is_answered_for_the_agent(audit):
    """`mud_act` cannot send an empty line: the validator refuses it. Without
    this the session would sit at the MOTD one keypress short of playable."""
    transport = FakeTransport(prompt=PromptKind.PRESS_RETURN)
    session = MudSession(transport, audit=audit)
    await session.connect()

    # Answering the keypress lands on the menu, and answering that lands in
    # the game, which is the whole two-step sequence.
    transport.set_prompt_sequence([PromptKind.MENU, PromptKind.GAME])
    await session.observe(session.session_id, timeout=0)

    assert transport.writes == ["", "1"]
    assert session.turn_state is TurnState.READY


@pytest.mark.asyncio
async def test_plumbing_is_bounded_and_does_not_type_forever(audit):
    # A server stuck at the menu must not produce an unbounded stream of "1".
    transport = FakeTransport(prompt=PromptKind.MENU)
    session = MudSession(transport, audit=audit)
    await session.connect()

    await session.observe(session.session_id, timeout=0)

    assert transport.writes == ["1", "1"], "two steps, then it gives up"


@pytest.mark.asyncio
async def test_answering_plumbing_is_recorded(audit):
    transport = FakeTransport(prompt=PromptKind.PRESS_RETURN)
    session = MudSession(transport, audit=audit)
    await session.connect()
    transport.set_prompt_sequence([PromptKind.GAME])
    await session.observe(session.session_id, timeout=0)

    # The trusted side typed into the game, so the record says so.
    assert "plumbing_answered" in audit.path.read_text()


@pytest.mark.asyncio
async def test_plumbing_does_not_spend_a_turn(audit):
    transport = FakeTransport(prompt=PromptKind.PRESS_RETURN)
    session = MudSession(transport, audit=audit)
    await session.connect()
    transport.set_prompt_sequence([PromptKind.GAME])
    await session.observe(session.session_id, timeout=0)

    # Turns count the agent's play, not the login sequence.
    assert session.status()["turns"] == 0


@pytest.mark.asyncio
async def test_connect_and_observe_agree_about_a_creation_prompt(audit):
    """They disagreed until Phase 8: a first login stopping at the sex prompt
    reported OBSERVING from connect and READY from the next observe, telling
    the agent it may not act and then that it may."""
    transport = FakeTransport(prompt=PromptKind.SEX)
    session = MudSession(transport, audit=audit)

    opened = await session.connect()
    assert opened["turn_state"] == TurnState.READY.value

    await session.observe(session.session_id, timeout=0)
    assert session.turn_state is TurnState.READY
