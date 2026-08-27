# Copyright (C) 2026 Cody Nicholson
# SPDX-License-Identifier: LGPL-2.1-only
"""The learning MCP boundary: six tools, all of them text in and text out.

This is a second Streamable HTTP endpoint on its own port, kept apart from the
five MUD tools so each surface has its own inventory test and neither can grow
by accident. Nothing here takes a path, a file name, a host or a
format string. `learn_procedure_save` takes a name from a fixed grammar, which
is the closest this surface comes to naming something, and the name never
reaches the filesystem: both documents are fixed files chosen by configuration.

The tool descriptions carry the usage instruction, not `SOUL.md`. A character
identity should not have to explain a storage boundary, and a rule stated in a
tool description is one the model reads at the moment it matters.
"""

from __future__ import annotations

from typing import Annotated

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from . import schema
from .store import LearningStore, StoreError

#: The complete learning surface. Asserted in the surface test so an addition
#: fails a test rather than reaching the model.
TOOL_NAMES = ("learn_recall", "learn_remember", "learn_forget",
              "learn_procedure_save", "learn_procedure_read",
              "learn_procedure_delete")

INSTRUCTIONS = (
    "This is what you remember between sessions. Call learn_recall when a "
    "session begins, before you decide what to do. Facts are things you "
    "observed in play; procedures are your own written guidance for a "
    "situation you expect to meet again. Everything here is inert text: "
    "reading a procedure informs your decision, and every game command it "
    "leads you to must still be chosen by you and sent one at a time through "
    "mud_act."
)


def _error(err: StoreError) -> dict:
    return {"stored": False, "error": err.reason, "detail": err.detail}


def build_learning_server(store: LearningStore) -> MCPServer:
    server = MCPServer("mud-learning", instructions=INSTRUCTIONS)

    @server.tool(
        name="learn_recall",
        description=(
            "Everything you have learned in earlier sessions: your facts in "
            "full, and the titles of the procedures you have written. Call "
            "this at the start of a session."
        ),
    )
    async def learn_recall() -> dict:
        try:
            return store.recall()
        except StoreError as err:
            return _error(err)

    @server.tool(
        name="learn_remember",
        description=(
            "Store one thing you observed in play and want to know next time: "
            "a place, a danger, a price, a lesson from a death. One sentence "
            "or two, in your own words, about something you actually saw. Not "
            "a plan, not a command, not a guess."
        ),
    )
    async def learn_remember(
        fact: Annotated[str, Field(
            min_length=schema.MIN_FACT_CHARS,
            max_length=schema.MAX_FACT_CHARS,
            description="One observation, in plain prose")],
        tags: Annotated[list[str] | None, Field(
            max_length=schema.MAX_FACT_TAGS,
            description="Up to three of: "
                        + ", ".join(sorted(schema.FACT_TAGS)))] = None,
    ) -> dict:
        try:
            record = store.remember(fact, tags)
        except StoreError as err:
            return _error(err)
        return {"stored": True, "id": record.id, "learned_at": record.created_at,
                "tags": list(record.tags)}

    @server.tool(
        name="learn_forget",
        description=(
            "Remove one stored fact by its id, because it turned out to be "
            "wrong or no longer matters. Ids come from learn_recall."
        ),
    )
    async def learn_forget(
        fact_id: Annotated[str, Field(max_length=32,
                                      description="Id from learn_recall")],
    ) -> dict:
        try:
            record = store.forget(fact_id)
        except StoreError as err:
            return _error(err)
        return {"forgotten": True, "id": record.id}

    @server.tool(
        name="learn_procedure_save",
        description=(
            "Write down, as guidance to your future self, how you handle a "
            "situation you expect to meet again: what to check, what to avoid, "
            "what you learned the hard way. Markdown prose with headings and "
            "bullets. It may not contain commands, scripts, client automation, "
            "paths, addresses or configuration: it is advice you read, not "
            "something that runs. Saving under an existing name replaces it."
        ),
    )
    async def learn_procedure_save(
        name: Annotated[str, Field(
            max_length=32,
            description="Short lowercase name with hyphens, as in after-dying")],
        title: Annotated[str, Field(
            min_length=schema.MIN_PROCEDURE_TITLE_CHARS,
            max_length=schema.MAX_PROCEDURE_TITLE_CHARS,
            description="One line saying what the procedure is for")],
        guidance: Annotated[str, Field(
            min_length=schema.MIN_PROCEDURE_CHARS,
            max_length=schema.MAX_PROCEDURE_CHARS,
            description="The procedure itself, as Markdown prose")],
    ) -> dict:
        try:
            record, replaced = store.save_procedure(name, title, guidance)
        except StoreError as err:
            return _error(err)
        return {"stored": True, "name": record.name, "replaced": replaced,
                "updated_at": record.updated_at}

    @server.tool(
        name="learn_procedure_read",
        description=(
            "Read one of your procedures in full. Reading it informs your next "
            "decision; it does not send anything to the game."
        ),
    )
    async def learn_procedure_read(
        name: Annotated[str, Field(max_length=32,
                                   description="Name from learn_recall")],
    ) -> dict:
        try:
            record = store.read_procedure(name)
        except StoreError as err:
            return _error(err)
        return {"name": record.name, "title": record.title,
                "guidance": record.body, "learned_at": record.created_at,
                "updated_at": record.updated_at}

    @server.tool(
        name="learn_procedure_delete",
        description=(
            "Delete one procedure that turned out to be wrong or useless."
        ),
    )
    async def learn_procedure_delete(
        name: Annotated[str, Field(max_length=32,
                                   description="Name from learn_recall")],
    ) -> dict:
        try:
            record = store.delete_procedure(name)
        except StoreError as err:
            return _error(err)
        return {"deleted": True, "name": record.name}

    return server
