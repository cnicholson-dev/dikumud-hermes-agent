# Copyright (C) 2026 Cody Nicholson
# SPDX-License-Identifier: LGPL-2.1-only
"""Validation for the one game command a model may send.

`SECURITY.md` section 6 states the rule this file exists to satisfy:

    Normalize once, validate once, and send the same validated value. Avoid
    validation/parsing differences between layers.

The way that is achieved here is to perform NO transformation at all. There is
no decoding, no unescaping, no Unicode normalisation, no case folding and no
whitespace rewriting beyond rejecting what is unacceptable. `validate()` returns
the caller's exact string, and that same object is what reaches the PTY. A
second decode cannot smuggle anything past the check, because there is no
second decode.

Unicode normalisation was considered and rejected for the same reason. NFKC
would fold lookalike characters into ASCII, which means the string that was
checked would differ from the string that was sent. Instead anything outside
printable ASCII is refused outright, so lookalikes never arrive at a decision
point.

The character set is an allowlist, not a denylist. A denylist has to enumerate
every dangerous character in TinTin++, in the shell, and in DikuMUD's parser,
and it silently fails open the moment one is missed. The allowlist below covers
ordinary DikuMUD play, including speech, and admits nothing else.
"""

from __future__ import annotations

from dataclasses import dataclass

#: DikuMUD's own input limit. structs.h sets MAX_INPUT_LENGTH to 80, and
#: comm.c truncates a longer line, so anything beyond this could not be sent
#: faithfully even if we allowed it.
MAX_COMMAND_LENGTH = 80

#: Characters a DikuMUD command may contain.
#:
#: Letters, digits and space cover movement, inspection, combat and inventory.
#: The punctuation is what ordinary speech needs: "say Hello, friend!" and
#: "tell wren I'm on my way" and "ask priest about the temple (again)".
#:
#: Deliberately absent, each for a reason:
#:   #   TinTin++ command prefix          -> #system, #run, #script
#:   ;   TinTin++ command separator       -> command batching
#:   {}  TinTin++ argument delimiters
#:   $   TinTin++ variable expansion
#:   @   TinTin++ function call
#:   %   TinTin++ regex / format
#:   \   escape introducer
#:   |&  shell pipes and background
#:   `   shell command substitution
#:   <>  shell redirection
#:   *~^ globbing and TinTin++ specials
_ALLOWED = frozenset(
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789"
    " "
    ".,!?'\"-:()/"
)

#: Reported back so a rejection explains itself without a second code path.
class Rejection(str):
    __slots__ = ()


@dataclass(frozen=True, slots=True)
class ValidationError(Exception):
    """Why a command was refused. Never contains credential material."""

    reason: str
    detail: str = ""

    def __str__(self) -> str:
        return f"{self.reason}: {self.detail}" if self.detail else self.reason


def _describe(char: str) -> str:
    """Name a rejected character without echoing raw bytes into a message."""
    code = ord(char)
    names = {
        0x00: "NUL", 0x07: "BEL", 0x08: "BS", 0x09: "TAB", 0x0A: "LF",
        0x0B: "VT", 0x0C: "FF", 0x0D: "CR", 0x1B: "ESC", 0x7F: "DEL",
    }
    if code in names:
        return names[code]
    if code < 0x20 or code == 0x7F:
        return f"control byte 0x{code:02x}"
    if code > 0x7E:
        return f"non-ASCII U+{code:04X}"
    return repr(char)


def validate(command: object) -> str:
    """Return the exact command string to send, or raise ValidationError.

    The returned value is the input object unchanged. Callers must send this
    return value, not the original argument, so that the checked value and the
    sent value cannot drift apart.
    """
    if not isinstance(command, str):
        raise ValidationError("not_a_string",
                              f"expected a string, got {type(command).__name__}")

    if command == "":
        raise ValidationError("empty", "command is empty")

    if len(command) > MAX_COMMAND_LENGTH:
        raise ValidationError(
            "too_long",
            f"{len(command)} characters exceeds the {MAX_COMMAND_LENGTH} "
            "character limit DikuMUD itself enforces",
        )

    if not command.strip():
        raise ValidationError("whitespace_only", "command is only whitespace")

    # Character check first: it subsumes control bytes, line breaks, NUL,
    # ESC, every separator, and every TinTin++ metacharacter in one pass.
    for char in command:
        if char not in _ALLOWED:
            raise ValidationError("disallowed_character", _describe(char))

    # The '#' prefix cannot reach here (it is not in the allowlist), but the
    # check is kept explicit because it is the rule SECURITY.md names, and a
    # future widening of the allowlist must not silently re-open it.
    if command.lstrip().startswith("#"):
        raise ValidationError("tintin_prefix",
                              "commands may not begin with '#'")

    return command


def is_valid(command: object) -> bool:
    try:
        validate(command)
    except ValidationError:
        return False
    return True
