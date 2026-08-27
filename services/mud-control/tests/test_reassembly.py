"""PTY-02 and PTY-05: fragmented reads reassemble; combat buffers correctly."""
from conftest import load

from mud_control import sanitize
from mud_control.buffer import OutputBuffer
from mud_control.prompts import PromptKind, classify


def feed(raw: bytes, chunk_size: int) -> str:
    """Push raw bytes through the cleaner in fixed-size chunks."""
    buf = OutputBuffer()
    for i in range(0, len(raw), chunk_size):
        buf.append(sanitize.clean(raw[i:i + chunk_size]).game_text)
    return buf.peek()


def test_byte_at_a_time_matches_whole_read():
    raw = load("05-fragmented-across-reads.bin")
    whole = sanitize.clean(raw).game_text
    in_pieces = feed(raw, 1)
    # Ordered reconstruction: same characters, same order, nothing invented.
    assert in_pieces.replace("\n", "") == whole.replace("\n", "")


def test_various_fragment_sizes_agree():
    raw = load("04-normal-command-and-prompt.bin")
    baseline = sanitize.clean(raw).game_text.replace("\n", "")
    for size in (1, 2, 3, 7, 13, 64, 512):
        assert feed(raw, size).replace("\n", "") == baseline, f"chunk={size}"


def test_multiple_messages_in_one_read_are_all_kept():
    raw = load("06-multiple-messages-one-read.bin")
    text = sanitize.clean(raw).game_text
    assert "You miss the Beastly Fido" in text
    assert "barely hits you" in text
    assert "You tickle the Beastly Fido" in text
    # Three messages plus the prompt arrived together and none were dropped.
    assert classify(text) is PromptKind.GAME


def test_combat_output_is_buffered_and_settles_on_a_prompt():
    text = sanitize.clean(load("08-automatic-combat-rounds.bin")).game_text
    assert "Cityguard" in text
    # Rounds arrive unsolicited; the transport keeps them and still settles.
    assert classify(text) is PromptKind.GAME


def test_nothing_is_invented_when_input_is_empty():
    assert sanitize.clean(b"").game_text == ""
    assert not sanitize.clean(b"")
