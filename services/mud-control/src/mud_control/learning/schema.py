# Copyright (C) 2026 Cody Nicholson
# SPDX-License-Identifier: LGPL-2.1-only
"""The fixed schema and size limits for persisted learning.

`SECURITY.md` section 9 requires "schema-valid bounded plain text". This module
is that schema: record shapes, limits, the tag vocabulary, the procedure name
grammar, and the on-disk layout. It performs no I/O and enforces no content
policy, so the validator and the store share one definition of what a record is
rather than each carrying its own idea of it.

Every limit here is a number a human chose for a reason, and the reason is in
the comment. A limit without a rationale gets raised the first time it is
inconvenient.
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from typing import Any

#: Written into every stored document. A future incompatible change increments
#: this, and `store.load()` refuses a document whose version it does not know
#: rather than guessing at the shape.
SCHEMA_VERSION = "learning/1"

# ---------------------------------------------------------------------------
# Facts
# ---------------------------------------------------------------------------

#: A fact is one sentence or two about something observed in play. 240
#: characters is roughly three lines of MUD text, which is enough for "the
#: cityguard in the temple square kills a level 1 character in four rounds" and
#: too short for a stored strategy essay. The design asks memory to hold "only
#: high-value durable facts"; a tight bound is what makes that true in practice
#: rather than in prompt text.
MAX_FACT_CHARS = 240

#: Below this a fact is a fragment, not an observation worth carrying between
#: sessions.
MIN_FACT_CHARS = 8

#: 64 facts at 240 characters is a ~15 KB recall payload, which is a reasonable
#: single tool result and a reasonable amount of remembered experience for a
#: supervised demonstration. When the store is full the agent must forget
#: something to learn something, which is the intended pressure.
MAX_FACTS = 64

#: Tags are optional, but a tag that is offered must come from this list. A free
#: text tag field is a second content channel with no policy behind it.
FACT_TAGS = frozenset({
    "place", "route", "npc", "mob", "item", "shop", "danger", "death",
    "combat", "command", "help", "lesson", "goal", "person",
})

MAX_FACT_TAGS = 3

# ---------------------------------------------------------------------------
# Procedures
# ---------------------------------------------------------------------------

#: Lowercase, hyphenated, no dots and no slashes, so a name can never be read as
#: a path fragment or a file extension. Length is bounded so the file name is
#: bounded.
PROCEDURE_NAME = re.compile(r"^[a-z][a-z0-9-]{2,31}$")

#: One screen of guidance. Long enough for "what I do after dying", short
#: enough that it cannot become a transcript or a data dump.
MAX_PROCEDURE_CHARS = 4000
MIN_PROCEDURE_CHARS = 40
MAX_PROCEDURE_LINES = 120

#: A line longer than this is not prose. It is a pasted blob, a URL, or an
#: encoded payload, and the character policy would usually catch it anyway.
MAX_PROCEDURE_LINE_CHARS = 200

MAX_PROCEDURE_TITLE_CHARS = 80
MIN_PROCEDURE_TITLE_CHARS = 4

#: Twelve procedures is more than a supervised demonstration will produce and
#: still small enough that the index fits in one observation.
MAX_PROCEDURES = 12

# ---------------------------------------------------------------------------
# On-disk layout
# ---------------------------------------------------------------------------

#: One document per kind, each rewritten by a single atomic replace.
#:
#: Procedures were first sketched as one Markdown file each with a JSON index
#: beside them, which reads better on the volume. It was dropped because a
#: procedure would then be two files, a save would be two writes, and a crash
#: between them would leave a store that is neither the old state nor the new
#: one. LEARN-06 asks for atomic mutation, and one file per kind is how that is
#: obtained without a journal. The stored body is still inert Markdown text;
#: only its container is JSON.
FACTS_FILE = "facts.json"
PROCEDURES_FILE = "procedures.json"

#: A read guard. Neither document can legitimately approach this size
#: (64 facts x 240 chars, 12 procedures x 4000 chars, plus metadata), so a file
#: larger than this is corrupt or hostile and is not parsed at all.
MAX_DOCUMENT_BYTES = 256 * 1024


def utc_now() -> str:
    """An ISO-8601 UTC timestamp, second resolution.

    `SECURITY.md` section 9 requires writes to be timestamped. Second
    resolution is deliberate: a finer timestamp would let write timing be read
    back out of the store, and nothing here needs it.
    """
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def digest(text: str) -> str:
    """Content digest for a stored record.

    This detects a record that was edited or corrupted on the volume between a
    write and a load, and it is checked on every load. It is **not** an
    authenticity control: the digest sits beside the content it covers, with no
    key, so anyone able to rewrite the file can rewrite the digest. The control
    that matters on load is revalidation against the content policy; this is the
    cheap consistency check in front of it.
    """
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class Fact:
    """One observed fact. Immutable once written; correct it by forgetting it."""

    id: str
    text: str
    tags: tuple[str, ...]
    created_at: str
    digest: str

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "tags": list(self.tags),
            "created_at": self.created_at,
            "digest": self.digest,
        }


@dataclass(frozen=True, slots=True)
class Procedure:
    """One inert Markdown procedure. The body is guidance, never a command."""

    name: str
    title: str
    body: str
    created_at: str
    updated_at: str
    digest: str

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "body": self.body,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "digest": self.digest,
        }

    def summary(self) -> dict[str, Any]:
        """Metadata only, for the recall index. The body is fetched by name."""
        return {
            "name": self.name,
            "title": self.title,
            "updated_at": self.updated_at,
            "chars": len(self.body),
            "lines": len(self.body.splitlines()),
        }


@dataclass(frozen=True, slots=True)
class SchemaError(Exception):
    """A record that does not fit the schema. Never contains stored content."""

    reason: str
    detail: str = ""

    def __str__(self) -> str:
        return f"{self.reason}: {self.detail}" if self.detail else self.reason


def fact_id(number: int) -> str:
    return f"fact-{number:04d}"


def empty_facts_document() -> dict[str, Any]:
    return {
        "schema": SCHEMA_VERSION,
        "kind": "facts",
        "next_id": 1,
        "facts": [],
    }


def empty_procedures_document() -> dict[str, Any]:
    return {
        "schema": SCHEMA_VERSION,
        "kind": "procedures",
        "procedures": [],
    }


def check_tags(tags: object) -> tuple[str, ...]:
    """Return the validated tag tuple, or raise `SchemaError`."""
    if tags is None:
        return ()
    if isinstance(tags, str) or not isinstance(tags, (list, tuple)):
        raise SchemaError("tags_not_a_list",
                          f"expected a list of tags, got {type(tags).__name__}")
    if len(tags) > MAX_FACT_TAGS:
        raise SchemaError("too_many_tags",
                          f"{len(tags)} tags exceeds the limit of {MAX_FACT_TAGS}")
    out: list[str] = []
    for tag in tags:
        if not isinstance(tag, str):
            raise SchemaError("tag_not_a_string",
                              f"expected a string tag, got {type(tag).__name__}")
        if tag not in FACT_TAGS:
            raise SchemaError(
                "unknown_tag",
                f"'{tag[:32]}' is not one of: " + ", ".join(sorted(FACT_TAGS)),
            )
        if tag not in out:
            out.append(tag)
    return tuple(out)


def check_procedure_name(name: object) -> str:
    if not isinstance(name, str):
        raise SchemaError("name_not_a_string",
                          f"expected a string name, got {type(name).__name__}")
    if not PROCEDURE_NAME.match(name):
        raise SchemaError(
            "bad_name",
            "a procedure name is 3 to 32 characters of lowercase letters, "
            "digits and hyphens, starting with a letter",
        )
    return name
