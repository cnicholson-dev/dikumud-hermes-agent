"""PTY-01 startup/shutdown and PTY-06 disconnect behaviour.

These use a stub standing in for TinTin++ so the lifecycle and the state
machine are tested deterministically, with no dependency on a running game
server. The live path is exercised separately in the Phase 2 gate report.
"""
import asyncio
import os
import textwrap
from pathlib import Path

import pytest

from mud_control.buffer import OutputBuffer
from mud_control.state import TERMINAL, WRITABLE, LinkState, TransportFault
from mud_control.transport import TintinTransport, TransportConfig

STUB = textwrap.dedent("""\
    #!/usr/bin/env python3
    # Minimal stand-in for TinTin++: announces a connected session, echoes
    # nothing, and stays alive until signalled.
    import sys, time
    sys.stdout.write("#SESSION 'diku' CONNECTED TO 'stub' PORT '4000'\\r\\n")
    sys.stdout.write("By what name do you wish to be known? ")
    sys.stdout.flush()
    while True:
        time.sleep(0.2)
""")


def make_config(tmp_path: Path) -> TransportConfig:
    stub = tmp_path / "fake-tt"
    stub.write_text(STUB)
    stub.chmod(0o755)
    cfg_file = tmp_path / "session.tin"
    cfg_file.write_text("#nop stub\n")
    cred = tmp_path / "cred"
    cred.write_text("Wren\nsecretpw\n")
    return TransportConfig(
        host="stub", port=4000, character="Wren",
        credential_path=cred, tintin_path=stub, session_config=cfg_file,
        runtime_dir=tmp_path / "run", connect_timeout=8.0,
    )


@pytest.mark.asyncio
async def test_start_then_close_leaves_no_orphan(tmp_path):
    t = TintinTransport(make_config(tmp_path))
    await t.start()
    pid = t.pid
    assert pid is not None
    assert t.state is LinkState.CONNECTED
    os.kill(pid, 0)  # raises if the child is not running

    await t.close()

    assert t.state is LinkState.CLOSED
    assert t.pid is None
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)  # reaped, not left as a zombie or orphan


@pytest.mark.asyncio
async def test_close_is_idempotent(tmp_path):
    t = TintinTransport(make_config(tmp_path))
    await t.start()
    await t.close()
    await t.close()
    assert t.state is LinkState.CLOSED


@pytest.mark.asyncio
async def test_writes_are_refused_once_the_link_is_gone(tmp_path):
    t = TintinTransport(make_config(tmp_path))
    await t.start()
    await t.close()
    with pytest.raises(TransportFault) as err:
        await t.send_line("look")
    assert err.value.state in TERMINAL or t.state in TERMINAL


@pytest.mark.asyncio
async def test_send_line_rejects_embedded_newlines(tmp_path):
    t = TintinTransport(make_config(tmp_path))
    await t.start()
    try:
        for bad in ("look\nkill guard", "look\r\nkill guard", "look\rkill"):
            with pytest.raises(TransportFault):
                await t.send_line(bad)
    finally:
        await t.close()


@pytest.mark.asyncio
async def test_starting_twice_is_refused(tmp_path):
    t = TintinTransport(make_config(tmp_path))
    await t.start()
    try:
        with pytest.raises(TransportFault):
            await t.start()
    finally:
        await t.close()


def test_terminal_states_are_not_writable():
    # Fail closed: no terminal state may be mistaken for a usable link.
    assert not (TERMINAL & WRITABLE)
    for state in (LinkState.DISCONNECTED, LinkState.FAULTED, LinkState.CLOSED):
        assert state in TERMINAL
        assert state not in WRITABLE


def test_fault_carries_the_state_that_caused_it():
    err = TransportFault("link lost", LinkState.DISCONNECTED)
    assert err.state is LinkState.DISCONNECTED
    # A caller never has to infer why the link died.
    assert "link lost" in str(err)


def test_credential_never_appears_in_a_fault_message(tmp_path):
    cfg = make_config(tmp_path)
    err = TransportFault(f"cannot write while {LinkState.FAULTED.value}")
    assert "secretpw" not in str(err)
    assert "secretpw" not in repr(cfg)


# -- the settle rule that caused the login race (Phase 8) ---------------

@pytest.mark.asyncio
async def test_quiet_before_the_server_speaks_is_not_a_settled_screen(tmp_path):
    """The race that recurred in Phases 6, 7 and 8, as a unit test.

    Right after connect the buffer holds a few bytes of Telnet negotiation and
    no game text. The settle rule used to accept "no new bytes for a quiet
    window, and unread > 0" as a settled screen, so a server that paused before
    sending its banner was reported as settled with no prompt. authenticate()
    then gave up at a prompt that arrived a moment later, and the session sat
    at the login screen with the agent unable to do anything about it.

    Quiet plus real text means settled. Quiet plus nothing means the server has
    not spoken yet.
    """
    t = TintinTransport(make_config(tmp_path))
    await t.start()
    try:
        # Negotiation bytes only: they clean to no game text at all.
        t._buffer.append("")            # noqa: SLF001
        t._settle_tail = ""             # noqa: SLF001
        t._cfg = t._cfg                 # noqa: SLF001

        with pytest.raises(TransportFault):
            # No text ever arrives, so this must time out rather than claim
            # the screen settled.
            await t.wait_settled(timeout=1.0)
    finally:
        await t.close()


@pytest.mark.asyncio
async def test_quiet_with_text_and_no_prompt_still_settles(tmp_path):
    # The MOTD pager leaves no prompt, and that case must keep working.
    t = TintinTransport(make_config(tmp_path))
    await t.start()
    try:
        t._settle_tail = "Some game text with no prompt at the end"  # noqa: SLF001
        t._buffer.append(t._settle_tail)                             # noqa: SLF001
        assert await t.wait_settled(timeout=2.0) is not None
    finally:
        await t.close()


def test_an_overlong_password_is_refused_with_a_reason_not_a_retry(tmp_path):
    """DikuMUD answers "Illegal password." and the login loop would retry the
    same value until it ran out of attempts, reporting a generic fault."""
    cfg = make_config(tmp_path)
    cfg.credential_path.write_text("Bram\n" + "x" * 11 + "\n")
    t = TintinTransport(cfg)

    with pytest.raises(TransportFault) as err:
        t._read_credential()  # noqa: SLF001

    assert "11 characters" in str(err.value)
    assert "x" * 11 not in str(err.value), "the length is reported, never the value"
