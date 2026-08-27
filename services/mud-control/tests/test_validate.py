"""MCP-05, MCP-06, MCP-07: command injection is refused at the boundary."""
import pytest

from mud_control.validate import (MAX_COMMAND_LENGTH, ValidationError,
                                  is_valid, validate)

ORDINARY_PLAY = [
    "look", "north", "south", "east", "west", "up", "down",
    "kill cityguard", "get all corpse", "wear armor", "wield sword",
    "score", "inventory", "equipment", "help", "who", "time", "save", "quit",
    "say Hello, friend!", "tell wren I am on my way",
    "ask priest about the temple (again)",
    "look at the bulletin board", "open door", "unlock chest",
]


@pytest.mark.parametrize("command", ORDINARY_PLAY)
def test_ordinary_play_is_accepted(command):
    assert validate(command) == command


def test_the_validated_value_is_the_input_object():
    # SECURITY.md: "send the same validated value". If validate() returned a
    # transformed copy, the checked value and the sent value could differ.
    original = "say Hello, friend!"
    assert validate(original) is original


# -- MCP-05 multiline and control-byte injection ------------------------

@pytest.mark.parametrize("command", [
    "look\nkill guard",      # LF
    "look\rkill guard",      # CR
    "look\r\nkill guard",    # CRLF
    "look\x00kill",          # NUL
    "look\x1b[31m",          # ESC
    "look\x07",              # BEL
    "look\tkill",            # TAB
    "look\x0bkill",          # VT
    "look\x0ckill",          # FF
    "look\x7f",              # DEL
    "\nlook",
    "look\n",
])
def test_multiline_and_control_bytes_are_rejected(command):
    assert not is_valid(command)


@pytest.mark.parametrize("command", [
    "look kill",   # LINE SEPARATOR
    "look kill",   # PARAGRAPH SEPARATOR
    "look\x85kill",     # NEL
    "look＃system",  # fullwidth number sign
    "lo​ok",       # zero width space
    "löök",   # non-ASCII letters
])
def test_unicode_lookalikes_and_separators_are_rejected(command):
    # Nothing outside printable ASCII reaches a decision point, so a lookalike
    # can never be folded into a dangerous character by a later normalisation.
    assert not is_valid(command)


# -- MCP-06 TinTin++ client commands ------------------------------------

@pytest.mark.parametrize("command", [
    "#system ls",
    "#SYSTEM ls",
    "#SyStEm ls",
    " #system ls",
    "\t#system ls",
    "  #run x id",
    "#script {id}",
    "#read /etc/passwd",
    "#send look",
    "#alias x {look}",
    "#showme hi",
    "#kill",
    "#",
])
def test_tintin_client_commands_are_rejected(command):
    assert not is_valid(command)


def test_hash_anywhere_is_rejected_not_just_at_the_start():
    # A '#' mid-string could still introduce a client command after a
    # separator, so the character is refused outright, not only as a prefix.
    assert not is_valid("look #system ls")
    assert not is_valid("say a#b")


# -- MCP-07 separators and batches --------------------------------------

@pytest.mark.parametrize("command", [
    "look;kill guard",      # TinTin++ command separator
    "look ; kill guard",
    "look|sh",
    "look || id",
    "look&&id",
    "look & id",
    "look`id`",
    "look $(id)",
    "look ${HOME}",
    "look $USER",
    "look @fn",
    "look %s",
    "look\\nkill",          # literal backslash-n
    "look\\;kill",
    "look{a}{b}",
    "look>out",
    "look<in",
    "look*",
    "look~",
    "look^",
])
def test_separators_and_batches_are_rejected(command):
    assert not is_valid(command)


# -- bounds and shape ---------------------------------------------------

def test_empty_and_whitespace_only_are_rejected():
    for command in ("", " ", "   ", "\t", "\n"):
        assert not is_valid(command)


def test_length_bound_matches_the_games_own_limit():
    assert is_valid("x" * MAX_COMMAND_LENGTH)
    assert not is_valid("x" * (MAX_COMMAND_LENGTH + 1))


@pytest.mark.parametrize("value", [None, 42, 3.5, b"look", ["look"], {"c": "look"}])
def test_non_strings_are_rejected(value):
    assert not is_valid(value)


def test_rejection_reasons_are_specific():
    with pytest.raises(ValidationError) as err:
        validate("look\nkill")
    assert err.value.reason == "disallowed_character"
    assert "LF" in err.value.detail

    with pytest.raises(ValidationError) as err:
        validate("")
    assert err.value.reason == "empty"


def test_rejection_message_does_not_echo_the_whole_command():
    # Hostile input must not be reflected verbatim into an error string.
    hostile = "look;" + "A" * 60
    with pytest.raises(ValidationError) as err:
        validate(hostile)
    assert "A" * 60 not in str(err.value)
