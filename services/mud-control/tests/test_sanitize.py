"""PTY-03: control sequences are stripped without corrupting game text."""
from conftest import load

from mud_control import sanitize


def test_ansi_sequences_are_removed_but_text_survives():
    raw = load("10-ansi-and-control-sequences.bin")
    assert b"\x1b" in raw, "fixture should contain escape sequences to strip"

    cleaned = sanitize.clean(raw)

    assert "\x1b" not in cleaned.game_text
    assert "[0m" not in cleaned.game_text
    # The help screen's actual content must survive intact.
    assert "Movement:" in cleaned.game_text
    assert "Communication:" in cleaned.game_text


def test_tintin_status_lines_are_events_not_game_text():
    raw = load("01-connect-and-telnet-negotiation.bin")
    cleaned = sanitize.clean(raw)

    joined = " ".join(cleaned.events).upper()
    assert "CONNECTED" in joined, "session status should be an event"
    # Client chatter must not reach the model as though the world said it.
    assert "SESSION 'diku' CONNECTED" not in cleaned.game_text
    assert "TRYING TO CONNECT" not in cleaned.game_text


def test_game_text_is_preserved_verbatim():
    cleaned = sanitize.clean(b"You are standing on the temple square.\r\n")
    assert cleaned.game_text.rstrip("\n") == "You are standing on the temple square."


def test_malformed_telnet_does_not_corrupt_surrounding_text():
    cleaned = sanitize.clean(load("15-malformed-telnet.bin"))
    assert "Before the negotiation." in cleaned.game_text
    assert "After the negotiation." in cleaned.game_text
    assert "\xff" not in cleaned.game_text


def test_tabs_and_newlines_are_kept():
    # DikuMUD lays out help tables with tabs; stripping them would corrupt them.
    cleaned = sanitize.clean(b"a\tb\r\nc\r\n")
    assert cleaned.game_text == "a\tb\nc\n" or cleaned.game_text == "a\tb\nc"
