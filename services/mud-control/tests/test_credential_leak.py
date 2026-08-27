"""MCP-09: the game password never reaches an observation.

Phase 2 kept the password out of observations by suppressing TinTin++'s echo
of the line just sent, and out of the audit record by scrubbing the raw tap.
Phase 6 found the gap between those two: echo suppression matches the echoed
line exactly, and only as the first line of a chunk. On a reconnect the server
prompt and the echo arrived in one read, so the match failed and the password
reached the model through `mud_observe`:

    By what name do you wish to be known? Password: <the password>

These tests drive the same shapes through the reading path and require the
password to be absent from what an observation would carry.
"""
import os

import pytest

from mud_control import sanitize
from mud_control.playlog import PlayLog
from mud_control.transport import TintinTransport, TransportConfig

PASSWORD = "not-a-real-password"


#: Where the fixture's transport writes its feed. A name of its own rather
#: than "play.log", so a test that builds its own PlayLog cannot end up sharing
#: a file with the one the transport is holding open.
WIRED_LOG = "wired-play.log"


@pytest.fixture
def transport(tmp_path):
    cred = tmp_path / "cred"
    cred.write_text(f"Wren\n{PASSWORD}\n")
    cfg = TransportConfig(host="stub", port=4000, character="Wren",
                          credential_path=cred, tintin_path=tmp_path / "tt",
                          session_config=tmp_path / "session.tin",
                          runtime_dir=tmp_path / "run")
    # Wired up the way build_from_env wires it, so a test can drive the real
    # read path and look at the file the service would actually have written.
    t = TintinTransport(cfg, play_log=PlayLog(tmp_path / WIRED_LOG))
    # Registered at the moment of use by authenticate(); done here directly so
    # the reading path can be tested without a live login.
    t._redactions.append(PASSWORD.encode("latin-1"))  # noqa: SLF001
    return t


@pytest.mark.parametrize("chunk", [
    # The exact shape observed in Phase 6: prompt and echo in one read.
    f"By what name do you wish to be known? Password: {PASSWORD}\n",
    # The echo on its own line, which suppression already handled.
    f"Password: \n{PASSWORD}\n",
    # Mid-stream, with game text on either side.
    f"The temple is quiet.\nPassword: {PASSWORD}\nYou are hungry.\n",
    # Upper and mixed case prompts.
    f"PASSWORD: {PASSWORD}\n",
    f"PassWord:{PASSWORD}\n",
])
def test_the_password_never_survives_into_observation_text(transport, chunk):
    cleaned = transport._redact_text(chunk)  # noqa: SLF001
    assert PASSWORD not in cleaned
    assert "<redacted>" in cleaned


def test_a_password_split_across_two_reads_is_still_hidden(transport):
    # Exact-value replacement cannot match a value split across chunks, which
    # is why the prompt rule exists as well.
    first = transport._redact_text("By what name? Password: not-a-real")  # noqa: SLF001
    assert "not-a-real" not in first


def test_game_text_is_left_alone(transport):
    text = ("The Temple Of Midgaard\n"
            "You are in the southern end of the temple hall.\n"
            "The cityguard says 'Move along, citizen.'\n")
    assert transport._redact_text(text) == text  # noqa: SLF001


def test_the_raw_tap_is_redacted_too(transport):
    raw = f"Password: {PASSWORD}\r\n".encode("latin-1")
    assert PASSWORD.encode("latin-1") not in transport._redact(raw)  # noqa: SLF001


# -- the split that defeated per-chunk redaction (Phase 7) --------------

def test_a_password_split_after_the_prompt_is_caught_when_served(transport):
    """One racing login in four leaked the password before this existed.

    The reads split so that no single chunk contained anything matchable: the
    first ended at the prompt, and the rest carried only fragments of the
    value. Per-chunk redaction saw nothing to do; the buffer then reassembled
    the password intact. Redacting the assembled text is what catches it.
    """
    chunks = ["By what name do you wish to be known? Password: ",
              PASSWORD[:6], PASSWORD[6:] + "\nWelcome to DikuMUD\n"]

    # Per chunk, exactly as _on_readable would: nothing is redacted, because
    # nothing is matchable in isolation. This is the bug, asserted.
    per_chunk = "".join(transport._redact_text(c) for c in chunks)  # noqa: SLF001
    assert PASSWORD in per_chunk

    # Over the assembled text, as observe() now does.
    assert PASSWORD not in transport._redact_text("".join(chunks))  # noqa: SLF001


def test_events_are_scrubbed_as_well(transport):
    # A client status line carried the tail of a login in one observed run.
    assert PASSWORD not in transport._redact_text(  # noqa: SLF001
        f"SESSION 'diku' RECONNECTED  Password: {PASSWORD}")


# -- the spectator feed, which is the one file holding content ----------

@pytest.mark.parametrize("chunk", [
    f"By what name do you wish to be known? Password: {PASSWORD}\n",
    f"Password: \n{PASSWORD}\n",
    f"The temple is quiet.\nPassword: {PASSWORD}\nYou are hungry.\n",
])
def test_the_play_log_never_receives_the_password(transport, tmp_path, chunk):
    """The feed is written from the cleaned stream, so it inherits redaction.

    Asserted rather than assumed, because this is the only file in the service
    that holds game content: everything else records reasons and lengths, and a
    regression here would put a credential on disk rather than merely make a
    record less useful.
    """
    log = PlayLog(tmp_path / "play.log")
    cleaned = sanitize.clean(chunk.encode("latin-1"))
    # The exact composition _on_readable applies before it writes.
    text = transport._redact_text(  # noqa: SLF001
        transport._suppress_echo(cleaned.game_text))  # noqa: SLF001
    log.write(text)
    log.close()

    written = (tmp_path / "play.log").read_text()
    assert PASSWORD not in written


@pytest.mark.parametrize("chunk", [
    f"By what name do you wish to be known? Password: {PASSWORD}\n",
    f"Password: \n{PASSWORD}\n",
    f"The temple is quiet.\nPassword: {PASSWORD}\nYou are hungry.\n",
])
def test_on_readable_writes_a_redacted_play_log(transport, tmp_path, chunk):
    """The write path itself, not a reconstruction of it.

    The test above composes clean, suppress and redact the way `_on_readable`
    does and asserts the result is safe. This one gives `_on_readable` a real
    file descriptor and a real PlayLog and asserts the file on disk is safe,
    because those are the same claim only for as long as nobody reorders the
    function.

    That is not hypothetical. On 2026-08-22 the live play log was found to
    contain this exact shape, in cleartext, written by an earlier build, while
    the reconstruction test passed: it cannot see the order of the real
    function, so a play-log write moved above the redaction would leave it
    green. This test fails in that case, and was checked against a deliberately
    reordered `_on_readable` before being trusted.

    A pipe rather than a PTY because `_on_readable` only reads a descriptor,
    and on the success path it touches nothing that needs a running event loop.
    """
    read_fd, write_fd = os.pipe()
    transport._fd = read_fd  # noqa: SLF001
    try:
        os.write(write_fd, chunk.encode("latin-1"))
        transport._on_readable()  # noqa: SLF001
    finally:
        os.close(write_fd)
        os.close(read_fd)
        transport._play_log.close()  # noqa: SLF001

    # The file the spectator reads, and the one file in this service that
    # holds game content.
    assert PASSWORD not in (tmp_path / WIRED_LOG).read_text()
    # What mud_observe would hand the model.
    assert PASSWORD not in transport._buffer.take(1_000_000)  # noqa: SLF001
    # What prompt detection matches against, which is also kept in memory.
    assert PASSWORD not in transport._settle_tail  # noqa: SLF001


def test_a_command_marker_carries_only_validated_input(tmp_path):
    """Markers come from the session layer for exactly this reason.

    `send_line` is also what types the character's password during login, so a
    marker written there would put the credential in the feed on the one path
    that must never contain it. The session writes markers instead, and it only
    ever holds a command that has passed the validator.
    """
    log = PlayLog(tmp_path / "play.log")
    log.command("north")
    log.close()

    assert (tmp_path / "play.log").read_text() == "north\n"
