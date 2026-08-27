"""The spectator's reasoning feed: bounded, rotating, and never fatal.

Two properties matter here and both are about what happens when things go
wrong. The feed sits on a tmpfs inside a 256 MB container, so it must not grow
without limit; and it is written from inside the relay's request path, so a
failure to write must cost the spectator its view and never cost the agent its
turn.
"""
from __future__ import annotations

import os

from openrouter_relay.reasoninglog import DEFAULT_MAX_BYTES, ReasoningLog


def test_it_appends_what_it_is_given(tmp_path):
    log = ReasoningLog(tmp_path / "reasoning.log")
    log.write("the temple is north")
    log.write(", so I should go north")
    log.close()
    assert (tmp_path / "reasoning.log").read_text() == (
        "the temple is north, so I should go north")


def test_empty_writes_are_ignored(tmp_path):
    """delta.content carries the key on nearly every chunk with nothing in it.

    Measured at 105 chunks against 10 with content. Reasoning deltas behave the
    same way at the end of a call, and a feed that recorded every empty string
    would still be correct but would spend its byte cap on nothing.
    """
    log = ReasoningLog(tmp_path / "reasoning.log")
    log.write("")
    log.close()
    assert (tmp_path / "reasoning.log").read_text() == ""


def test_a_call_marker_names_the_model_and_the_sequence(tmp_path):
    """The line a reader uses to tell one call's thinking from the next.

    The model id is in it because a different model may answer when the one
    ahead of it is unavailable, and which model thought something should not
    become a guess.
    """
    log = ReasoningLog(tmp_path / "reasoning.log")
    log.open_call(7, "vendor/some-model:free")
    log.write("thinking")
    log.close_call("stop")
    log.close()

    written = (tmp_path / "reasoning.log").read_text()
    assert "--- call 7  vendor/some-model:free" in written
    assert "--- end stop ---" in written
    assert "thinking" in written


def test_it_rotates_at_the_cap_rather_than_truncating(tmp_path):
    """Rotation, because the reader tails this file.

    Truncating in place would have a spectator reading into a hole. A rename
    leaves the open descriptor writing to a file that is simply no longer the
    one being read, and the previous generation stays readable.
    """
    path = tmp_path / "reasoning.log"
    log = ReasoningLog(path, max_bytes=100)
    log.write("a" * 60)
    assert not (tmp_path / "reasoning.log.1").exists()
    log.write("b" * 60)
    log.close()

    assert (tmp_path / "reasoning.log.1").exists()
    assert (tmp_path / "reasoning.log.1").read_text() == "a" * 60 + "b" * 60
    # The live file is a fresh one, not a truncated old one.
    assert path.read_text() == ""


def test_the_ceiling_is_two_generations(tmp_path):
    """A long session cannot fill the tmpfs.

    Reasoning is the bulk of a streamed response rather than a side channel,
    so this is the property that keeps the feed from competing with the relay
    for the container's memory.
    """
    path = tmp_path / "reasoning.log"
    log = ReasoningLog(path, max_bytes=1000)
    for _ in range(200):
        log.write("x" * 100)
    log.close()

    total = sum(p.stat().st_size for p in tmp_path.iterdir())
    assert total <= 2 * 1000 + 100, "more than two generations survived"


def test_a_write_failure_disables_the_log_rather_than_raising(tmp_path):
    """The property that protects the agent's turn.

    This is written from inside the request path. A full tmpfs must degrade to
    "the spectator stops updating" and never to "the model call failed".
    """
    log = ReasoningLog(tmp_path / "reasoning.log")
    os.close(log._fd)  # noqa: SLF001  simulate the descriptor going bad

    log.write("this must not raise")
    log.open_call(1, "vendor/model")
    log.close_call("stop")

    assert log._disabled is True  # noqa: SLF001
    log.close()


def test_an_unwritable_path_does_not_raise_at_construction(tmp_path):
    """Same rule, one step earlier.

    A deployment whose feed path cannot be created should serve model calls
    exactly as it always did, without a feed.
    """
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("")
    log = ReasoningLog(blocker / "reasoning.log")

    log.write("still must not raise")
    assert log._disabled is True  # noqa: SLF001


def test_the_default_cap_is_smaller_than_the_play_log(tmp_path):
    """Deliberate: this one is RAM inside the relay, not a disk volume."""
    assert DEFAULT_MAX_BYTES == 256_000
