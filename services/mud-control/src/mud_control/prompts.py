# Copyright (C) 2026 Cody Nicholson
# SPDX-License-Identifier: LGPL-2.1-only
"""Recognise DikuMUD prompts in accumulated output.

Every pattern here was taken from output observed against the pinned server,
never from assumption. A recogniser written from what the server is expected to
send is a recogniser that fails on the one case nobody expected.

The rule is positional, not textual. A prompt is only a prompt
when it sits at the very end of what we have received with no newline after
it, because DikuMUD writes a prompt and then waits. Ordinary prose that merely
looks like a prompt is always followed by more output, so it never qualifies.
That is what PTY-04 tests: "real prompts settle; prompt-like prose does not".
"""

from __future__ import annotations

import re
from enum import Enum


class PromptKind(str, Enum):
    """What the server is currently asking for."""

    NONE = "none"
    GAME = "game"                  # in play, ready for a command
    NAME = "name"                  # "By what name do you wish to be known?"
    NAME_CONFIRM = "name_confirm"  # "Did I get that right, X (Y/N)?"
    PASSWORD = "password"          # existing character
    PASSWORD_NEW = "password_new"  # first-time creation
    PASSWORD_CONFIRM = "password_confirm"
    SEX = "sex"
    CLASS = "class"
    PRESS_RETURN = "press_return"
    MENU = "menu"


# Ordered: the first match wins, so the more specific patterns come first.
_PATTERNS: tuple[tuple[PromptKind, re.Pattern[str]], ...] = (
    (PromptKind.NAME, re.compile(r"By what name do you wish to be known\?\s*$", re.I)),
    (PromptKind.NAME_CONFIRM, re.compile(r"Did I get that right,\s+\S+\s*\(Y/N\)\?\s*$", re.I)),
    (PromptKind.PASSWORD_NEW, re.compile(r"Give me a password for\s+\S+\s*:\s*$", re.I)),
    (PromptKind.PASSWORD_CONFIRM, re.compile(r"(?:Please\s+)?[Rr]etype password:\s*$", re.I)),
    (PromptKind.SEX, re.compile(r"What is your sex\s*\(M/F\)\s*\?\s*$", re.I)),
    (PromptKind.CLASS, re.compile(r"^\s*Class\s*:\s*$", re.I | re.M)),
    (PromptKind.PRESS_RETURN, re.compile(r"\*\*\*\s*PRESS RETURN:\s*$", re.I)),
    (PromptKind.MENU, re.compile(r"Make your choice:\s*$", re.I)),
    # "Password:" and the bare "Name:" reprompt come after the more specific
    # creation prompts so they cannot shadow them.
    (PromptKind.PASSWORD, re.compile(r"Password:\s*$", re.I)),
    # The reprompt after a rejected name arrives as
    # "Illegal name, please try another.Name: " on ONE line, so this cannot be
    # anchored to the start of a line.
    (PromptKind.NAME, re.compile(r"Name:\s*$", re.I)),
    # The in-game prompt. Stock DikuMUD Alfa writes a bare "> ".
    (PromptKind.GAME, re.compile(r"(?:^|\n)>\s*$")),
)

# How much of the tail to examine. Prompts are short; scanning the whole buffer
# would let an early match in old output masquerade as the current state.
_TAIL_CHARS = 200


def classify(buffer: str) -> PromptKind:
    """Return the prompt the buffer currently ends on, if any.

    `buffer` is cleaned game text. A trailing newline means the server is still
    talking, so nothing is settled.
    """
    if not buffer:
        return PromptKind.NONE

    # Trailing spaces are part of a prompt; a trailing newline is not.
    if buffer.endswith("\n"):
        return PromptKind.NONE

    tail = buffer[-_TAIL_CHARS:]
    for kind, pattern in _PATTERNS:
        if pattern.search(tail):
            return kind
    return PromptKind.NONE


def is_settled(buffer: str) -> bool:
    """True when the server has finished talking and is waiting on us."""
    return classify(buffer) is not PromptKind.NONE


def expects_secret(kind: PromptKind) -> bool:
    """True for prompts whose answer is credential material.

    The harness uses this to decide what must never be echoed into a log, a
    fixture, or an audit event.
    """
    return kind in (
        PromptKind.PASSWORD,
        PromptKind.PASSWORD_NEW,
        PromptKind.PASSWORD_CONFIRM,
    )
