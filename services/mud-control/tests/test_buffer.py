"""PTY-07: output is bounded with deterministic overflow."""
from conftest import load

from mud_control import sanitize
from mud_control.buffer import OutputBuffer


def test_ordinary_output_is_returned_in_order():
    buf = OutputBuffer()
    buf.append("first\n")
    buf.append("second\n")
    assert buf.take(1024) == "first\nsecond\n"
    assert buf.unread == 0
    assert not buf.overflowed


def test_take_returns_at_most_the_limit_and_keeps_the_rest():
    buf = OutputBuffer()
    buf.append("abcdefghij")
    assert buf.take(4) == "abcd"
    assert buf.unread == 6
    assert buf.take(1024) == "efghij"


def test_oversize_output_discards_oldest_and_keeps_newest():
    text = sanitize.clean(load("14-oversize-output-overflow.bin")).game_text
    buf = OutputBuffer(max_unread=64 * 1024)
    buf.append(text)

    assert buf.overflowed, "fixture is larger than the bound; overflow must trip"
    assert buf.unread <= 64 * 1024
    held = buf.peek()
    # Newest output is what the model must act on, so it is what we keep.
    assert "NEWEST-MARKER" in held
    assert "OLDEST-MARKER" not in held
    assert buf.discarded_chars > 0


def test_overflow_is_visible_rather_than_silent():
    buf = OutputBuffer(max_unread=16)
    buf.append("x" * 100)
    # A caller must be able to report a gap instead of inventing continuity.
    assert buf.overflowed
    assert buf.discarded_chars == 84
