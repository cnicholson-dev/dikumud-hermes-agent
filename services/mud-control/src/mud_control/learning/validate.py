# Copyright (C) 2026 Cody Nicholson
# SPDX-License-Identifier: LGPL-2.1-only
"""The content policy for everything the agent may persist.

`SECURITY.md` section 9 lists what stored learning may not
contain: scripts, binaries, code blocks intended for execution, imports,
executable resources, TinTin++ automation, shell commands, arbitrary file
paths, URLs, MCP definitions, model configuration, tool definitions, and
capability-expansion instructions.

The approach is the one `validate.py` already uses for game commands, for the
same reasons:

* **No transformation.** The validated string is returned unchanged and is the
  string the store writes, so the checked value and the stored value cannot
  drift apart. There is no decode, no normalisation, no case folding.
* **An allowlist, not a denylist**, for characters. A denylist has to enumerate
  every metacharacter in TinTin++, in the shell, in Markdown and in YAML, and it
  fails open the first time one is missed. The allowlist admits ordinary English
  prose about a MUD and nothing else, which is all a learned fact needs to be.

Three checks here are denylists rather than allowlists: automation markers,
network/file references, and capability-expansion phrasing. They are defence in
depth, not the primary control, and they are honest about it. The primary
controls are the character allowlist, the size bounds, and the fact that the
tool surface is fixed at startup and cannot be changed by anything stored here.
A procedure the model reads is text in a tool result; the only way to affect the
game is still one `mud_act` call at a time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import schema

#: Characters a stored fact may contain: the game-command allowlist from
#: `validate.py`, plus '%'. If a character is not safe to send to the game there
#: is rarely a reason to carry it in a note about the game.
#:
#: '%' is the one addition. TinTin++ writes its variables as %1 and %2, which is
#: why the command validator refuses the character outright, but "the guard took
#: me to 30% health" is exactly the kind of durable observation this store
#: exists for. `_check_percent` admits the percentage and still refuses the
#: variable by requiring a digit *before* the sign.
_FACT_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789"
    " "
    ".,!?'\"-:()/%"
)

#: A procedure is Markdown, so it needs line breaks, headings, bullets and the
#: occasional percentage. Everything else is the fact set.
#:
#: Still absent, each for a reason:
#:   `   code fence and shell substitution
#:   ~   code fence
#:   \   escape introducer
#:   |&  pipes and background
#:   <>  redirection
#:   {}  TinTin++ argument delimiters
#:   $@  TinTin++ variable and function expansion
#:   ;   command separator
#:   []  link syntax
#:   _   identifier syntax (api_key, mcp_servers, base_url)
#:   ^   TinTin++ special
_PROCEDURE_CHARS = _FACT_CHARS | frozenset("\n#*+")

#: Markers of executable or automated content. Case-insensitive, word-bounded
#: where a bare substring would be too eager.
_AUTOMATION_MARKERS = (
    r"#!",
    r"\bsudo\b", r"\bcurl\b", r"\bwget\b", r"\bchmod\b", r"\bchown\b",
    r"\bapt-get\b", r"\bsystemctl\b", r"\bsubprocess\b", r"\bos\.system\b",
    r"\bpip\s+install\b", r"\bnpm\b", r"\bdocker\b", r"\bkubectl\b",
    r"\beval\s*\(", r"\bexec\s*\(", r"\bimport\b", r"\brequire\s*\(",
    # TinTin++ local commands. The '#' rule below already refuses these, and
    # they are named again because SECURITY.md section 6 names them and a future
    # widening of the character set must not silently re-open the door.
    r"#system", r"#alias", r"#action", r"#send", r"#var", r"#script",
    r"#run", r"#read", r"#write", r"#class", r"#event", r"#macro",
    r"-----begin",
)

#: Markers of configuration, tool definitions and credentials. Several of these
#: are already impossible because '_' is not in either character set; they are
#: listed so the rule is visible rather than incidental.
_CONFIG_MARKERS = (
    r"\bmcpservers\b", r"\bmcp\s+server\b", r"\btools?\s*:", r"\bmodel\s*:",
    r"\bprovider\s*:", r"\btoolset\b", r"\bbearer\b", r"\bauthorization\b",
    r"\bapikey\b", r"\bbase\s*url\b", r"\bsystem\s+prompt\b",
)

#: Instructions whose effect would be to widen what the agent may do. This is
#: the fuzziest check in the file and the one with the least security weight:
#: the tool surface is immutable at runtime, so a stored sentence cannot enable
#: anything. It is here because LEARN-04 asks for it and because a stored
#: instruction aimed at a future reader is worth refusing on its own terms.
_EXPANSION_MARKERS = (
    r"ignore\s+(all\s+)?(previous|prior|earlier|the\s+above)",
    r"disregard\s+(all\s+)?(previous|prior|your|the\s+above)",
    r"your\s+new\s+instructions",
    r"you\s+(may\s+)?now\s+(use|have\s+access|are\s+allowed)",
    r"enable\s+(the\s+)?\w+\s+tool",
    r"install\s+(the\s+|a\s+|an\s+)?\w+",
    r"grant\s+yourself", r"give\s+yourself",
    r"add\s+(a|an|another)\s+\w*\s*(tool|server|toolset)",
    r"override\s+your", r"change\s+your\s+(configuration|model|tools)",
)

#: A scheme, a domain, or a filename. Network locations and file references are
#: both named in SECURITY.md section 9.
_NETWORK_MARKERS = (
    r"\b(https?|ftp|file|data|ssh|telnet|ws|wss|mcp)\s*:",
    r"\bwww\.",
    r"\b[a-z0-9-]+\.(com|net|org|io|ai|dev|co|uk|edu|gov|xyz|sh)\b",
)

#: A file name. The first group is the usual code and configuration set; the
#: second is DikuMUD's own world data, which `SECURITY.md` section 2 lists as an
#: asset the agent must not reach. Naming one in a note is not reaching it, but
#: a stored note that points a later session at the world files is the first
#: half of the move this boundary exists to prevent.
_FILE_MARKER = re.compile(
    r"\b[a-z0-9-]+\.(md|py|sh|json|ya?ml|toml|ini|conf|cfg|exe|dll|so|bin|"
    r"js|ts|txt|log|db|sql|env|pem|key|"
    r"wld|mob|obj|zon|shp|messages)\b",
    re.IGNORECASE,
)

#: No English word, and no DikuMUD keyword, is this long. A token that is
#: catches base64, hex blobs, long paths, and anything else opaque.
#:
#: Set to 48 originally, and lowered after a live adversarial run stored a
#: 48-character base64 blob: the limit was exactly the length of the test
#: payload, which is the kind of coincidence a boundary should not depend on.
#: The longest word in ordinary use is around 28 characters, so 32 leaves room
#: for prose and cuts what an encoded payload can carry in one token.
MAX_TOKEN_CHARS = 32

_HEADING = re.compile(r"^#{1,6} \S")


@dataclass(frozen=True, slots=True)
class ContentError(Exception):
    """Why content was refused. Never quotes the refused content back."""

    reason: str
    detail: str = ""

    def __str__(self) -> str:
        return f"{self.reason}: {self.detail}" if self.detail else self.reason


def _describe(char: str) -> str:
    """Name a rejected character without echoing raw bytes into a message."""
    code = ord(char)
    names = {0x00: "NUL", 0x09: "TAB", 0x0A: "LF", 0x0D: "CR", 0x1B: "ESC",
             0x7F: "DEL"}
    if code in names:
        return names[code]
    if code < 0x20 or code == 0x7F:
        return f"control byte 0x{code:02x}"
    if code > 0x7E:
        return f"non-ASCII U+{code:04X}"
    return repr(char)


def _check_markers(text: str) -> None:
    """Run the four denylists. Order is chosen so the reason is the most
    specific one available, because a rejection the model cannot act on is a
    rejection it will repeat."""
    lowered = text.lower()

    for pattern in _AUTOMATION_MARKERS:
        if re.search(pattern, lowered):
            raise ContentError(
                "executable_content",
                "stored learning is inert prose; it may not contain commands, "
                "scripts, client automation or code",
            )

    for pattern in _NETWORK_MARKERS:
        if re.search(pattern, lowered):
            raise ContentError(
                "network_reference",
                "stored learning may not contain a URL, a host name or any "
                "network location",
            )

    if _FILE_MARKER.search(text):
        raise ContentError(
            "file_reference",
            "stored learning may not name a file or a path; describe what you "
            "observed in the game instead",
        )

    for pattern in _CONFIG_MARKERS:
        if re.search(pattern, lowered):
            raise ContentError(
                "configuration_content",
                "stored learning may not contain tool, server, model or "
                "credential configuration",
            )

    for pattern in _EXPANSION_MARKERS:
        if re.search(pattern, lowered):
            raise ContentError(
                "capability_instruction",
                "stored learning may not contain instructions about what you "
                "are permitted to do; it records what you observed in play",
            )


def _check_tokens(text: str) -> None:
    for token in text.split():
        if len(token) > MAX_TOKEN_CHARS:
            raise ContentError(
                "opaque_token",
                f"a run of {len(token)} characters with no space is not prose; "
                f"the limit is {MAX_TOKEN_CHARS}",
            )


def _check_path_syntax(text: str) -> None:
    """A '/' joins two alphanumerics, once per word, and does nothing else.

    That admits "north/south" and "hit points/mana" and refuses "/etc/passwd",
    "../lib", "http://host" and "lib/players/wren" without a path parser and
    without a list of interesting directories.

    The limit of the rule, stated plainly: a single "word/word" token is
    indistinguishable from prose, so a one-segment relative path is admitted.
    That is deliberate. Nothing stored here can be executed or opened, the agent
    has no filesystem tool to act on such a token, and a rule strict enough to
    catch it would cost the agent ordinary English. Multi-segment paths, leading
    slashes, and every file extension worth naming are refused above.
    """
    for token in text.split():
        if token.count("/") > 1:
            raise ContentError(
                "path_syntax",
                "a word with more than one '/' is a path, not prose",
            )

    for index, char in enumerate(text):
        if char != "/":
            continue
        before = text[index - 1] if index > 0 else ""
        after = text[index + 1] if index + 1 < len(text) else ""
        if not (before.isalnum() and after.isalnum()):
            raise ContentError(
                "path_syntax",
                "'/' may only join two words, as in north/south; it may not "
                "begin a path or repeat",
            )


def _check_percent(text: str) -> None:
    """'%' is allowed only after a digit, as in "50% health".

    TinTin++ writes variables as %1, %2; requiring a digit *before* the sign
    admits the percentage and refuses the variable.
    """
    for index, char in enumerate(text):
        if char != "%":
            continue
        before = text[index - 1] if index > 0 else ""
        if not before.isdigit():
            raise ContentError(
                "percent_syntax",
                "'%' is only allowed directly after a number, as in 50%",
            )


def validate_fact_text(text: object) -> str:
    """Return the exact fact text to store, or raise `ContentError`."""
    if not isinstance(text, str):
        raise ContentError("not_a_string",
                           f"expected a string, got {type(text).__name__}")
    if not text.strip():
        raise ContentError("empty", "a fact needs content")
    if len(text) < schema.MIN_FACT_CHARS:
        raise ContentError(
            "too_short",
            f"{len(text)} characters is a fragment; a fact is at least "
            f"{schema.MIN_FACT_CHARS} characters",
        )
    if len(text) > schema.MAX_FACT_CHARS:
        raise ContentError(
            "too_long",
            f"{len(text)} characters exceeds the {schema.MAX_FACT_CHARS} "
            "character limit for one fact; keep it to the observation itself",
        )

    for char in text:
        if char not in _FACT_CHARS:
            raise ContentError("disallowed_character", _describe(char))

    _check_path_syntax(text)
    _check_percent(text)
    _check_tokens(text)
    _check_markers(text)
    return text


def validate_procedure_title(title: object) -> str:
    if not isinstance(title, str):
        raise ContentError("not_a_string",
                           f"expected a string, got {type(title).__name__}")
    if len(title) < schema.MIN_PROCEDURE_TITLE_CHARS:
        raise ContentError("title_too_short",
                           "give the procedure a short descriptive title")
    if len(title) > schema.MAX_PROCEDURE_TITLE_CHARS:
        raise ContentError(
            "title_too_long",
            f"{len(title)} characters exceeds the "
            f"{schema.MAX_PROCEDURE_TITLE_CHARS} character title limit",
        )
    for char in title:
        if char not in _FACT_CHARS:
            raise ContentError("disallowed_character", _describe(char))
    _check_tokens(title)
    _check_markers(title)
    return title


def validate_procedure_body(body: object) -> str:
    """Return the exact procedure body to store, or raise `ContentError`.

    A procedure is Markdown prose: headings, bullets, sentences. Anything that
    reads as a code block, a command, a path, a URL or a configuration fragment
    is refused, because the difference between guidance and automation is the
    whole point of the learning boundary.
    """
    if not isinstance(body, str):
        raise ContentError("not_a_string",
                           f"expected a string, got {type(body).__name__}")
    if not body.strip():
        raise ContentError("empty", "a procedure needs content")
    if len(body) < schema.MIN_PROCEDURE_CHARS:
        raise ContentError(
            "too_short",
            f"{len(body)} characters is not a procedure; write at least "
            f"{schema.MIN_PROCEDURE_CHARS} characters of guidance",
        )
    if len(body) > schema.MAX_PROCEDURE_CHARS:
        raise ContentError(
            "too_long",
            f"{len(body)} characters exceeds the {schema.MAX_PROCEDURE_CHARS} "
            "character limit for one procedure",
        )

    for char in body:
        if char not in _PROCEDURE_CHARS:
            raise ContentError("disallowed_character", _describe(char))

    lines = body.splitlines()
    if len(lines) > schema.MAX_PROCEDURE_LINES:
        raise ContentError(
            "too_many_lines",
            f"{len(lines)} lines exceeds the {schema.MAX_PROCEDURE_LINES} "
            "line limit",
        )

    for number, line in enumerate(lines, start=1):
        if len(line) > schema.MAX_PROCEDURE_LINE_CHARS:
            raise ContentError(
                "line_too_long",
                f"line {number} is {len(line)} characters; the limit is "
                f"{schema.MAX_PROCEDURE_LINE_CHARS}",
            )
        # An indented block is Markdown's other way of writing code, and it
        # needs no fence characters to do it.
        if line.strip() and line.startswith("    "):
            raise ContentError(
                "indented_block",
                f"line {number} is indented four spaces, which is a Markdown "
                "code block; procedures hold prose, not code",
            )
        if "#" in line and not _HEADING.match(line):
            # '#' is the TinTin++ command prefix. It is admitted only as an ATX
            # heading, which requires the space that '#system' does not have.
            raise ContentError(
                "tintin_prefix",
                f"line {number}: '#' is only allowed as a Markdown heading "
                "('## Title'), because it is the client command prefix",
            )
        if "*" in line:
            # A single leading '*' is a list bullet. Anywhere else it is
            # emphasis, a glob, or a TinTin++ special, none of which a
            # procedure needs.
            body_text = line.lstrip(" ")
            bullet = body_text.startswith("* ") and "*" not in body_text[1:]
            if not bullet:
                raise ContentError(
                    "star_syntax",
                    f"line {number}: '*' is only allowed once, as a list "
                    "bullet at the start of the line",
                )

    _check_path_syntax(body)
    _check_percent(body)
    _check_tokens(body)
    _check_markers(body)
    return body


def fact_is_valid(text: object) -> bool:
    try:
        validate_fact_text(text)
    except ContentError:
        return False
    return True


def procedure_body_is_valid(body: object) -> bool:
    try:
        validate_procedure_body(body)
    except ContentError:
        return False
    return True
