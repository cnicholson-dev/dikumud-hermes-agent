"""E2E-05: every way a session can end names its own reason.

Phase 8 audited what already stopped a session and found quota, timeout and
disconnect covered, while repetition, a play-session budget and any record of
*why* a session ended were not. These tests cover the part that was missing,
and check it against the fake transport so a stop is proven to reach the PTY
never rather than merely to return an error.
"""
import pytest

from mud_control.audit import AuditLog
from mud_control.limits import SessionBudget, SessionLimits, StopReason
from mud_control.session import MudSession, TurnState
from mud_control.state import LinkState

from test_session import FakeTransport  # the same stand-in the protocol uses


@pytest.fixture
def audit(tmp_path):
    return AuditLog(tmp_path / "audit.jsonl")


async def session_with(limits, audit):
    session = MudSession(FakeTransport(), audit=audit, limits=limits)
    await session.connect()
    return session


async def play(session, command, intent="playing"):
    """One full turn: act, then observe back to READY.

    Observing between commands is the protocol, not test scaffolding. Without
    it the state stays COMMAND_SENT and the next call is refused as not_ready,
    which would mask whatever the budget did.
    """
    result = await session.act(session.session_id, command, intent=intent)
    await session.observe(session.session_id, timeout=0)
    return result


# -- the budget on its own ----------------------------------------------

def test_repeating_one_command_is_not_progress():
    budget = SessionBudget(limits=SessionLimits(max_repeats=3))
    assert budget.record_accepted("look") is None
    assert budget.record_accepted("look") is None
    assert budget.record_accepted("look") is StopReason.NO_PROGRESS


def test_a_varied_run_of_commands_is_progress():
    budget = SessionBudget(limits=SessionLimits(max_repeats=3))
    for command in ("look", "north", "look", "south", "look", "exits"):
        assert budget.record_accepted(command) is None
    assert budget.stopped is None


def test_repeats_must_be_consecutive():
    # "kill guard" twice, a heal, then twice more is a fight, not a loop.
    budget = SessionBudget(limits=SessionLimits(max_repeats=3))
    for command in ("kill guard", "kill guard", "drink potion",
                    "kill guard", "kill guard"):
        assert budget.record_accepted(command) is None


def test_the_first_reason_is_the_one_that_sticks():
    budget = SessionBudget(limits=SessionLimits(max_repeats=2, max_turns=2))
    budget.record_accepted("look")
    assert budget.record_accepted("look") is StopReason.NO_PROGRESS
    # The turn limit is also reached now; the session did not stop because of it.
    assert budget.check_before() is StopReason.NO_PROGRESS
    assert budget.stopped is StopReason.NO_PROGRESS


def test_refusals_do_not_spend_turns():
    # Otherwise hostile input could burn a session's budget without the game
    # ever seeing a command.
    budget = SessionBudget(limits=SessionLimits(max_consecutive_rejections=3))
    budget.record_rejected()
    budget.record_rejected()
    assert budget.turns == 0
    assert budget.record_rejected() is StopReason.REJECTION_LOOP


def test_an_accepted_command_clears_the_rejection_run():
    budget = SessionBudget(limits=SessionLimits(max_consecutive_rejections=3))
    budget.record_rejected()
    budget.record_rejected()
    budget.record_accepted("look")
    assert budget.record_rejected() is None


def test_limits_come_from_the_environment():
    limits = SessionLimits.from_env({"MUD_CONTROL_MAX_TURNS": "7",
                                     "MUD_CONTROL_MAX_REPEATS": "2"})
    assert limits.max_turns == 7
    assert limits.max_repeats == 2
    # Absent and empty both fall back to the default rather than raising.
    default = SessionLimits().max_turns
    assert SessionLimits.from_env({}).max_turns == default
    assert SessionLimits.from_env({"MUD_CONTROL_MAX_TURNS": ""}).max_turns == default


# -- enforced at the boundary -------------------------------------------

@pytest.mark.asyncio
async def test_a_loop_stops_the_session_and_the_pty_goes_quiet(audit):
    session = await session_with(SessionLimits(max_repeats=3), audit)
    transport = session._transport  # noqa: SLF001

    for _ in range(2):
        assert (await play(session, "look", "looking")).accepted

    # The third repeat is still sent: the world has already seen it, and the
    # session ends with that command as its last.
    third = await play(session, "look", "looking")
    assert third.accepted
    assert third.reason == StopReason.NO_PROGRESS.value
    assert transport.writes == ["look", "look", "look"]

    # Everything after is refused, and nothing more reaches the PTY.
    after = await play(session, "north", "moving on")
    assert not after.accepted
    assert after.reason == StopReason.NO_PROGRESS.value
    assert "loop rather than play" in after.detail
    assert transport.writes == ["look", "look", "look"]


@pytest.mark.asyncio
async def test_the_turn_limit_stops_the_session(audit):
    session = await session_with(SessionLimits(max_turns=2), audit)
    results = [await play(session, c) for c in ("look", "north", "south")]

    assert [r.accepted for r in results] == [True, True, False]
    assert results[-1].reason == StopReason.TURN_LIMIT.value
    assert session._transport.writes == ["look", "north"]  # noqa: SLF001


@pytest.mark.asyncio
async def test_a_rejection_loop_stops_the_session(audit):
    session = await session_with(SessionLimits(max_consecutive_rejections=3),
                                 audit)
    for _ in range(2):
        result = await session.act(session.session_id, "#system id",
                                   intent="probing")
        # Refused by the character allowlist before the '#' prefix rule is
        # reached, which is the order Phase 6 established and pinned.
        assert result.reason == "disallowed_character"

    stopped = await session.act(session.session_id, "#system id",
                                intent="probing")
    assert stopped.reason == StopReason.REJECTION_LOOP.value
    assert session._transport.writes == []  # noqa: SLF001


@pytest.mark.asyncio
async def test_a_lost_link_stops_the_session_with_a_transport_reason(audit):
    session = await session_with(SessionLimits(), audit)
    session._transport.state = LinkState.DISCONNECTED  # noqa: SLF001

    result = await session.act(session.session_id, "look", intent="looking")

    assert not result.accepted
    assert session._budget.stopped is StopReason.TRANSPORT  # noqa: SLF001


@pytest.mark.asyncio
async def test_an_operator_can_stop_a_session_immediately(audit):
    session = await session_with(SessionLimits(), audit)
    session.stop()

    result = await session.act(session.session_id, "look", intent="looking")
    assert not result.accepted
    assert result.reason == StopReason.OPERATOR.value
    assert session._transport.writes == []  # noqa: SLF001


@pytest.mark.asyncio
@pytest.mark.parametrize("limits,commands", [
    # The turn limit trips on a refusal, and no_progress trips on a command
    # that was accepted. They reach the stop through different paths, and the
    # audit showed no_progress announcing itself twice because only one of
    # those paths went through the guard.
    (SessionLimits(max_turns=1), ["look", "north", "north", "north"]),
    (SessionLimits(max_repeats=2), ["look", "look", "look", "look"]),
])
async def test_a_session_announces_its_ending_once(audit, limits, commands):
    session = MudSession(FakeTransport(), audit=audit, limits=limits)
    await session.connect()
    for command in commands:
        await session.act(session.session_id, command, intent="playing")
        await session.observe(session.session_id, timeout=0)

    events = [line for line in audit.path.read_text().splitlines()
              if "session_stopped" in line]
    assert len(events) == 1, f"announced {len(events)} times"
    assert session.status()["stop_reason"] is not None


@pytest.mark.asyncio
async def test_the_stop_reason_is_visible_in_status(audit):
    session = await session_with(SessionLimits(max_turns=1), audit)
    await session.act(session.session_id, "look", intent="looking")
    for _ in range(3):
        await session.act(session.session_id, "north", intent="moving")

    assert session.status()["stop_reason"] == StopReason.TURN_LIMIT.value


@pytest.mark.asyncio
async def test_reconnecting_starts_a_fresh_budget(audit):
    session = await session_with(SessionLimits(max_turns=1), audit)
    await session.act(session.session_id, "look", intent="looking")
    assert (await session.act(session.session_id, "north",
                              intent="moving")).accepted is False

    await session.disconnect(session.session_id)
    await session.connect()

    result = await session.act(session.session_id, "north", intent="moving")
    assert result.accepted
    assert session.status()["stop_reason"] is None


@pytest.mark.asyncio
async def test_a_dead_link_is_reported_as_a_dead_link_not_as_a_turn(audit):
    """Phase 8 stopped the game under a live session and got back
    "not_ready: state is COMMAND_SENT" with no stop reason. True, and useless:
    it tells the agent to wait for a turn that can never come and hides a
    disconnect behind a word that normally means be patient."""
    session = await session_with(SessionLimits(), audit)
    # Mid-command, exactly as a real disconnect arrives.
    await session.act(session.session_id, "look", intent="looking")
    assert session.turn_state is TurnState.COMMAND_SENT
    session._transport.state = LinkState.DISCONNECTED  # noqa: SLF001

    result = await session.act(session.session_id, "north", intent="moving")

    # The caller sees the specific fact; the session records the category.
    assert result.reason == "link_unavailable"
    assert session.status()["stop_reason"] == StopReason.TRANSPORT.value


@pytest.mark.asyncio
async def test_a_link_that_dies_while_observing_records_the_stop(audit):
    session = await session_with(SessionLimits(), audit)
    session._transport.state = LinkState.DISCONNECTED  # noqa: SLF001

    await session.observe(session.session_id, timeout=0)

    # No further command is needed for the spectator to show why play ended.
    assert session.status()["stop_reason"] == StopReason.TRANSPORT.value
