"""MCP-02, MCP-03, MCP-04, MCP-09, MCP-10: the turn protocol from the hostile side.

These drive MudSession against a fake transport that records every write, so a
test can assert not only that a call was refused but that nothing reached the
PTY as a side effect. A refusal that still wrote a byte would pass a naive
"returns an error" assertion and fail the actual requirement.
"""
import asyncio

import pytest

from mud_control.audit import AuditLog
from mud_control.playlog import PlayLog
from mud_control.prompts import PromptKind
from mud_control.session import MudSession, SessionError, TurnState
from mud_control.state import LinkState, TransportFault
from mud_control.transport import Observation

# A stand-in, never a real credential. The repository contains none, and
# these tests only ever assert this value is ABSENT from output.
SECRET = "not-a-real-password"


class FakeTransport:
    """Records writes; never touches a real process."""

    def __init__(self, prompt=PromptKind.GAME, state=LinkState.IDLE):
        self.writes: list[str] = []
        # Starts IDLE like the real transport, so connect() exercises the same
        # start()/reconnect() branch the service takes in production.
        self.state = state
        self.unread = 0
        self._prompt = prompt
        self.started = False
        self.closed = False
        self.starts = 0
        self.reconnects = 0
        self.write_delay = 0.0

    async def start(self):
        self.started = True
        self.starts += 1
        self.state = LinkState.CONNECTED

    async def reconnect(self):
        self.reconnects += 1
        self.state = LinkState.CONNECTED

    async def authenticate(self):
        # The credential is used here and never returned to a caller.
        return self._prompt

    async def send_line(self, line):
        if self.state in (LinkState.DISCONNECTED, LinkState.FAULTED,
                          LinkState.CLOSED):
            raise TransportFault("link gone", self.state)
        if self.write_delay:
            await asyncio.sleep(self.write_delay)
        self.writes.append(line)
        # A written line is what moves a real server on to its next prompt, so
        # the queue advances here rather than on observe. Advancing on observe
        # would consume a prompt before the code under test had seen it.
        if getattr(self, "_queued", None):
            self._prompt = self._queued.pop(0)

    async def observe(self, timeout=None, limit=8192):
        return Observation(text="", prompt=self._prompt, state=self.state)

    async def disconnect(self):
        self.closed = True
        self.state = LinkState.CLOSED

    def set_prompt(self, prompt):
        self._prompt = prompt

    def set_prompt_sequence(self, prompts):
        """Queue what the next observations report, in order.

        The login plumbing walks through several prompts in one observe call,
        so a fake with a single fixed prompt cannot express it: answering the
        MOTD keypress has to land somewhere different from where it started.
        The last queued prompt sticks once the queue empties.
        """
        self._queued = list(prompts)


@pytest.fixture
def audit(tmp_path):
    return AuditLog(tmp_path / "audit.jsonl")


async def open_session(transport, audit):
    session = MudSession(transport, audit=audit)
    await session.connect()
    return session


# -- MCP-02 one call writes exactly one command -------------------------

@pytest.mark.asyncio
async def test_one_call_writes_exactly_one_command(audit):
    t = FakeTransport()
    s = await open_session(t, audit)

    result = await s.act(s.session_id, "look", intent="orienting")

    assert result.accepted
    assert t.writes == ["look"], "exactly one line, exactly the validated value"


@pytest.mark.asyncio
async def test_rejected_command_writes_nothing(audit):
    t = FakeTransport()
    s = await open_session(t, audit)

    for hostile in ("look\nkill guard", "#system ls", "look;kill", "", "x" * 200):
        result = await s.act(s.session_id, hostile, intent="test intent")
        assert not result.accepted

    assert t.writes == [], "a refused command must never reach the PTY"


# -- the spectator feed sees accepted commands only ---------------------

@pytest.mark.asyncio
async def test_an_accepted_command_is_marked_in_the_play_log(audit, tmp_path):
    t = FakeTransport()
    log = PlayLog(tmp_path / "play.log")
    s = MudSession(t, audit=audit, play_log=log)
    await s.connect()

    await s.act(s.session_id, "north", intent="heading for the altar")
    log.close()

    assert (tmp_path / "play.log").read_text() == "north\n"


@pytest.mark.asyncio
async def test_a_refused_command_is_not_marked(audit, tmp_path):
    # The feed is a record of what the game saw. A command the boundary
    # refused never reached it, so showing it would misdescribe the session.
    t = FakeTransport()
    log = PlayLog(tmp_path / "play.log")
    s = MudSession(t, audit=audit, play_log=log)
    await s.connect()

    for hostile in ("#system ls", "look;kill", "", "x" * 200):
        result = await s.act(s.session_id, hostile, intent="test intent")
        assert not result.accepted
    log.close()

    assert (tmp_path / "play.log").read_text() == ""


# -- MCP-03 state rejection, with no PTY write --------------------------

@pytest.mark.asyncio
async def test_act_is_refused_outside_ready_without_writing(audit):
    t = FakeTransport()
    s = await open_session(t, audit)

    first = await s.act(s.session_id, "look", intent="test intent")
    assert first.accepted
    assert s.turn_state is TurnState.COMMAND_SENT

    second = await s.act(s.session_id, "north", intent="test intent")

    assert not second.accepted
    assert second.reason == "not_ready"
    assert t.writes == ["look"], "the second command must not have been written"


@pytest.mark.asyncio
async def test_observing_state_also_refuses_act(audit):
    t = FakeTransport()
    s = await open_session(t, audit)
    await s.act(s.session_id, "look", intent="test intent")
    t.set_prompt(PromptKind.NONE)
    await s.observe(s.session_id)          # COMMAND_SENT -> OBSERVING
    assert s.turn_state is TurnState.OBSERVING

    result = await s.act(s.session_id, "north", intent="test intent")

    assert not result.accepted and result.reason == "not_ready"
    assert t.writes == ["look"]


@pytest.mark.asyncio
async def test_combat_output_does_not_unlock_the_turn(audit):
    """Unsolicited output must not authorise another command."""
    t = FakeTransport()
    s = await open_session(t, audit)
    await s.act(s.session_id, "kill cityguard", intent="test intent")

    # Rounds keep arriving with no prompt: still not the model's turn.
    t.set_prompt(PromptKind.NONE)
    for _ in range(3):
        await s.observe(s.session_id)
        assert s.turn_state is TurnState.OBSERVING
        assert not (await s.act(s.session_id, "flee", intent="test intent")).accepted

    assert t.writes == ["kill cityguard"]


@pytest.mark.asyncio
async def test_only_the_game_prompt_returns_ready(audit):
    t = FakeTransport()
    s = await open_session(t, audit)
    await s.act(s.session_id, "look", intent="test intent")

    # A login or menu prompt is not a licence to play.
    for prompt in (PromptKind.PASSWORD, PromptKind.MENU, PromptKind.NAME,
                   PromptKind.PRESS_RETURN):
        t.set_prompt(prompt)
        await s.observe(s.session_id)
        assert s.turn_state is TurnState.OBSERVING, prompt

    t.set_prompt(PromptKind.GAME)
    await s.observe(s.session_id)
    assert s.turn_state is TurnState.READY


# -- MCP-04 atomic transition -------------------------------------------

@pytest.mark.asyncio
async def test_concurrent_acts_produce_exactly_one_write(audit):
    """The state must leave READY before another caller can test it."""
    t = FakeTransport()
    t.write_delay = 0.05          # widen the window a racy implementation needs
    s = await open_session(t, audit)

    results = await asyncio.gather(
        s.act(s.session_id, "look", intent="test intent"),
        s.act(s.session_id, "north", intent="test intent"),
        s.act(s.session_id, "south", intent="test intent"),
        s.act(s.session_id, "east", intent="test intent"),
    )

    accepted = [r for r in results if r.accepted]
    assert len(accepted) == 1, "only one concurrent act may be accepted"
    assert len(t.writes) == 1, "exactly one command may reach the PTY"


@pytest.mark.asyncio
async def test_many_concurrent_acts_still_write_once(audit):
    t = FakeTransport()
    t.write_delay = 0.01
    s = await open_session(t, audit)

    results = await asyncio.gather(*[
        s.act(s.session_id, "look", intent="test intent") for _ in range(25)
    ])

    assert sum(1 for r in results if r.accepted) == 1
    assert len(t.writes) == 1


# -- MCP-10 session isolation -------------------------------------------

@pytest.mark.asyncio
async def test_wrong_session_id_cannot_act(audit):
    t = FakeTransport()
    s = await open_session(t, audit)

    for bogus in ("", "not-the-session", s.session_id + "x", s.session_id[:-1]):
        with pytest.raises(SessionError):
            await s.act(bogus, "look", intent="test intent")
    assert t.writes == []


@pytest.mark.asyncio
async def test_non_string_session_id_is_refused(audit):
    t = FakeTransport()
    s = await open_session(t, audit)
    for bogus in (None, 42, ["x"], {"id": "x"}):
        with pytest.raises(SessionError):
            await s.act(bogus, "look", intent="test intent")
    assert t.writes == []


@pytest.mark.asyncio
async def test_stale_session_id_after_disconnect_is_refused(audit):
    t = FakeTransport()
    s = await open_session(t, audit)
    stale = s.session_id
    await s.disconnect(stale)

    with pytest.raises(SessionError):
        await s.act(stale, "look", intent="test intent")
    with pytest.raises(SessionError):
        await s.observe(stale)
    assert t.writes == []


@pytest.mark.asyncio
async def test_session_ids_are_unpredictable(audit):
    seen = set()
    for _ in range(20):
        t = FakeTransport()
        s = await open_session(t, audit)
        seen.add(s.session_id)
        await s.disconnect(s.session_id)
    assert len(seen) == 20
    assert all(len(sid) >= 16 for sid in seen)


# -- fail closed --------------------------------------------------------

@pytest.mark.asyncio
async def test_lost_link_makes_the_session_unavailable(audit):
    t = FakeTransport()
    s = await open_session(t, audit)
    t.state = LinkState.DISCONNECTED

    result = await s.act(s.session_id, "look", intent="test intent")

    assert not result.accepted
    assert result.reason == "link_unavailable"
    assert s.turn_state is TurnState.UNAVAILABLE
    assert t.writes == []


@pytest.mark.asyncio
async def test_unavailable_never_becomes_ready_on_its_own(audit):
    t = FakeTransport()
    s = await open_session(t, audit)
    t.state = LinkState.FAULTED
    await s.act(s.session_id, "look", intent="test intent")
    assert s.turn_state is TurnState.UNAVAILABLE

    t.set_prompt(PromptKind.GAME)
    await s.observe(s.session_id)
    assert s.turn_state is TurnState.UNAVAILABLE, "a fault must not self-clear"


# -- MCP-09 credential secrecy ------------------------------------------

@pytest.mark.asyncio
async def test_status_exposes_no_connection_details_or_secrets(audit):
    t = FakeTransport()
    s = await open_session(t, audit)
    status = s.status()

    blob = repr(status)
    for forbidden in (SECRET, "password", "host", "port", "credential"):
        assert forbidden not in blob


@pytest.mark.asyncio
async def test_audit_contains_no_credential(tmp_path):
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    log.register_secret(SECRET)
    t = FakeTransport()
    s = await open_session(t, log)

    await s.act(s.session_id, "look", intent="orienting")
    await s.act(s.session_id + "x", "look", intent="test intent") if False else None
    log.record("with_secret_inline", note=f"the password is {SECRET}")

    text = path.read_text()
    assert SECRET not in text
    assert "<redacted>" in text


@pytest.mark.asyncio
async def test_audit_refuses_forbidden_fields(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    entry = log.record("probe", password="hunter2", host="dikumud",
                       reasoning="my private thoughts", command="look")
    assert entry["password"] == "<refused: forbidden field>"
    assert entry["host"] == "<refused: forbidden field>"
    assert entry["reasoning"] == "<refused: forbidden field>"
    assert entry["command"] == "look"


@pytest.mark.asyncio
async def test_audit_records_rejections_by_reason_not_content(tmp_path):
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    t = FakeTransport()
    s = await open_session(t, log)

    await s.act(s.session_id, "look;" + "SECRETPAYLOAD" * 3, intent="test intent")

    text = path.read_text()
    assert "command_rejected" in text
    assert "SECRETPAYLOAD" not in text, "hostile input must not be copied in"


@pytest.mark.asyncio
async def test_audit_is_append_only(tmp_path):
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    log.record("first")
    log.record("second")
    log.record("third")
    lines = path.read_text().strip().split("\n")
    assert len(lines) == 3
    assert "first" in lines[0] and "third" in lines[2]


@pytest.mark.asyncio
async def test_a_second_session_can_be_opened_after_disconnect(audit):
    """Regression: the service must not be single-use.

    transport.start() only works from IDLE, so after a disconnect the second
    connect has to go through reconnect(). Before this was fixed, every
    mud_connect after the first returned a transport fault for the lifetime of
    the process.
    """
    t = FakeTransport()
    s = MudSession(t, audit=audit)

    first = await s.connect()
    assert t.starts == 1 and t.reconnects == 0

    await s.disconnect(first["session_id"])
    second = await s.connect()

    assert second["session_id"]
    assert second["session_id"] != first["session_id"]
    # The second open had to go through reconnect(), not start().
    assert t.starts == 1 and t.reconnects == 1


@pytest.mark.asyncio
async def test_a_command_without_an_intent_is_refused(audit):
    """Phase 5 finding: an optional intent field is simply never filled.

    The model sent look, read board and look at board with intent="" every
    time. The design requires one concise visible statement of intent per
    command, so it is enforced here rather than requested in prompt text.
    """
    t = FakeTransport()
    s = await open_session(t, audit)

    for empty in ("", "   ", "\t", None, 42):
        result = await s.act(s.session_id, "look", intent=empty)
        assert not result.accepted
        assert result.reason == "intent_required"

    assert t.writes == [], "a command with no intent must not reach the PTY"


@pytest.mark.asyncio
async def test_a_command_with_an_intent_is_accepted(audit):
    t = FakeTransport()
    s = await open_session(t, audit)
    result = await s.act(s.session_id, "look", intent="getting my bearings")
    assert result.accepted
    assert t.writes == ["look"]


# -- resuming an open session across agent restarts ---------------------

@pytest.mark.asyncio
async def test_connect_returns_the_live_session_instead_of_refusing(audit):
    """Phase 6 finding: a second agent process could not reach the game.

    The first agent session left a session open. The second called
    mud_connect, was told "already_connected", and had no way to learn the
    identifier, so the game was unreachable until an operator restarted the
    service. Connecting now returns the session that exists.
    """
    t = FakeTransport()
    s = await open_session(t, audit)
    first = s.session_id
    await s.act(first, "look", intent="getting my bearings")

    resumed = await s.connect()

    assert resumed["resumed"] is True
    assert resumed["session_id"] == first
    assert resumed["commands_sent"] == 1
    # No second login, and nothing was written to the PTY by reconnecting.
    assert t.starts == 1 and t.reconnects == 0
    assert t.writes == ["look"]


@pytest.mark.asyncio
async def test_a_resumed_session_still_gates_commands_on_turn_state(audit):
    t = FakeTransport()
    s = await open_session(t, audit)
    t.set_prompt(PromptKind.NONE)
    await s.act(s.session_id, "look", intent="getting my bearings")

    resumed = await s.connect()

    # Resuming reports the true state and grants nothing: the turn state is
    # what decides, exactly as before.
    assert resumed["resumed"] is True
    assert resumed["turn_state"] != TurnState.READY.value
    result = await s.act(s.session_id, "north", intent="moving on")
    assert not result.accepted and result.reason == "not_ready"
    assert t.writes == ["look"]


@pytest.mark.asyncio
async def test_a_stale_identifier_is_still_refused_after_a_resume(audit):
    """MCP-10 is unaffected: resuming hands back the live id, and every other
    identifier is still refused."""
    t = FakeTransport()
    s = await open_session(t, audit)
    live = s.session_id
    await s.connect()

    for forged in ("", "not-the-session", live[:-1] + "x", None, 42):
        with pytest.raises(SessionError) as err:
            await s.act(forged, "look", intent="test intent")
        assert err.value.reason in ("invalid_session", "no_session")
    assert t.writes == []


@pytest.mark.asyncio
async def test_a_dead_link_is_reconnected_rather_than_resumed(audit):
    t = FakeTransport()
    s = await open_session(t, audit)
    first = s.session_id
    t.state = LinkState.FAULTED

    reopened = await s.connect()

    assert reopened["resumed"] is False
    assert reopened["session_id"] != first
    assert t.reconnects == 1
