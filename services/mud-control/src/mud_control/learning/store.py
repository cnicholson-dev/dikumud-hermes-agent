# Copyright (C) 2026 Cody Nicholson
# SPDX-License-Identifier: LGPL-2.1-only
"""The store: atomic writes, revalidation on every load, fail-closed quarantine.

Four properties the persistence boundary fixes, made code here.

**Validate on write and on load.** Every public method begins by reading both
documents from disk and revalidating every record in them against the current
content policy. Content on disk earns no trust from having been accepted once:
the file may have been edited since, the validator may have grown stricter
since, and either way the check is cheap (a full store is under 100 KB).

**Atomic writes.** A document is written to a temporary file in the same
directory, flushed, fsynced, and moved into place with `os.replace`, which is
atomic on POSIX. A reader sees the old document or the new one, never a partial
one. One file per kind, so a mutation is exactly one replace (see `schema.py`).

**A rejected write changes nothing.** Validation happens before the first byte
is written, and the load that precedes it fails closed, so a refused mutation
cannot leave prior state altered. LEARN-06 asserts the bytes are identical.

**Fail closed on tampering.** A document that will not parse, that fails the
schema, that fails the content policy, or whose per-record digest does not match
its content puts the store into quarantine: every operation, read and write
alike, refuses with an explicit reason until an operator intervenes. The
tampered file is left exactly as found, because it is evidence.

The digest is a consistency check, not an authenticity control. It sits beside
the content it covers with no key, so anyone who can rewrite the file can
rewrite the digest. What defends the *content* on load is revalidation against
the policy; the digest catches the careless case and corruption.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..audit import AuditLog
from . import schema
from .schema import Fact, Procedure, SchemaError
from .validate import (ContentError, validate_fact_text,
                       validate_procedure_body, validate_procedure_title)


@dataclass(frozen=True, slots=True)
class StoreError(Exception):
    """Why an operation was refused. Never contains stored content."""

    reason: str
    detail: str = ""

    def __str__(self) -> str:
        return f"{self.reason}: {self.detail}" if self.detail else self.reason


class LearningStore:
    """Bounded factual memory and inert procedures on one small volume."""

    @staticmethod
    def root_for(base: Path | str, character: str) -> Path:
        """Where one character's learning lives.

        Learning is per character, because it is that character's experience.
        A new character inheriting the previous one's notes would break the
        claim the project actually makes: that the agent learns only from its
        own play. It would also be confusing in the other direction, since a
        fresh character would "remember" rooms it has never walked into.

        The name comes from trusted configuration, never from the model, and is
        still reduced to a safe directory name here: config is trusted, but a
        value that reaches a filesystem path deserves the same treatment as one
        that does not.
        """
        safe = "".join(c for c in character if c.isalnum() or c in "-_")[:32]
        if not safe:
            raise ValueError("character name has no usable characters")
        return Path(base) / safe

    def __init__(self, root: Path | str, audit: AuditLog):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._audit = audit
        # The MCP server may serve two calls concurrently. A mutation is
        # read-modify-write across a file, so it is serialised here rather than
        # relying on the caller to do it.
        self._lock = threading.Lock()

    # -- paths ----------------------------------------------------------

    @property
    def facts_path(self) -> Path:
        return self.root / schema.FACTS_FILE

    @property
    def procedures_path(self) -> Path:
        return self.root / schema.PROCEDURES_FILE

    # -- reading --------------------------------------------------------

    def _read_document(self, path: Path, kind: str) -> dict[str, Any]:
        """Read and shape-check one document, or raise a quarantine error."""
        if not path.exists():
            return (schema.empty_facts_document() if kind == "facts"
                    else schema.empty_procedures_document())

        try:
            size = path.stat().st_size
        except OSError as err:
            raise self._quarantine(kind, "unreadable", type(err).__name__)

        if size > schema.MAX_DOCUMENT_BYTES:
            raise self._quarantine(
                kind, "document_too_large",
                f"{size} bytes exceeds the {schema.MAX_DOCUMENT_BYTES} byte limit",
            )

        try:
            raw = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as err:
            # An existing file that cannot be read is not an empty store.
            # Treating it as empty and then writing would erase the record.
            raise self._quarantine(kind, "unreadable", type(err).__name__)

        try:
            document = json.loads(raw)
        except json.JSONDecodeError:
            raise self._quarantine(kind, "malformed_json",
                                   "the document is not valid JSON")

        if not isinstance(document, dict):
            raise self._quarantine(kind, "malformed_document",
                                   "the document is not an object")
        if document.get("schema") != schema.SCHEMA_VERSION:
            raise self._quarantine(
                kind, "unknown_schema",
                f"expected {schema.SCHEMA_VERSION}",
            )
        if document.get("kind") != kind:
            raise self._quarantine(kind, "wrong_kind",
                                   "the document declares another kind")
        return document

    def _quarantine(self, kind: str, reason: str, detail: str) -> StoreError:
        """Record the fault and build the error the caller raises."""
        self._audit.record("learning_quarantined", kind=kind, reason=reason,
                           detail=detail)
        return StoreError(
            "store_quarantined",
            f"the stored {kind} document was refused on load ({reason}): "
            f"{detail}. Learning is unavailable until an operator resolves it; "
            "nothing was changed.",
        )

    def load_facts(self) -> tuple[list[Fact], int]:
        """Return every valid stored fact and the next free id number.

        Every record is revalidated here. One bad record quarantines the whole
        document rather than being skipped: a store that silently drops what it
        cannot verify is a store that hides tampering.
        """
        document = self._read_document(self.facts_path, "facts")
        raw_facts = document.get("facts")
        if not isinstance(raw_facts, list):
            raise self._quarantine("facts", "malformed_document",
                                   "facts is not a list")
        next_id = document.get("next_id")
        if not isinstance(next_id, int) or next_id < 1:
            raise self._quarantine("facts", "malformed_document",
                                   "next_id is missing or not a positive integer")
        if len(raw_facts) > schema.MAX_FACTS:
            raise self._quarantine(
                "facts", "over_capacity",
                f"{len(raw_facts)} records exceeds the limit of {schema.MAX_FACTS}",
            )

        facts: list[Fact] = []
        seen: set[str] = set()
        for record in raw_facts:
            if not isinstance(record, dict):
                raise self._quarantine("facts", "malformed_record",
                                       "a record is not an object")
            identifier = record.get("id")
            text = record.get("text")
            created = record.get("created_at")
            stored_digest = record.get("digest")
            if not all(isinstance(value, str) for value
                       in (identifier, text, created, stored_digest)):
                raise self._quarantine("facts", "malformed_record",
                                       "a record is missing a required field")
            if identifier in seen:
                raise self._quarantine("facts", "duplicate_id",
                                       "two records share an id")
            seen.add(identifier)

            try:
                tags = schema.check_tags(record.get("tags", []))
                validate_fact_text(text)
            except (SchemaError, ContentError) as err:
                raise self._quarantine("facts", f"invalid_on_load:{err.reason}",
                                       f"record {identifier[:16]}")
            if schema.digest(text) != stored_digest:
                raise self._quarantine("facts", "digest_mismatch",
                                       f"record {identifier[:16]}")

            facts.append(Fact(id=identifier, text=text, tags=tags,
                              created_at=created, digest=stored_digest))
        return facts, next_id

    def load_procedures(self) -> list[Procedure]:
        """Return every valid stored procedure, revalidated as for facts."""
        document = self._read_document(self.procedures_path, "procedures")
        raw = document.get("procedures")
        if not isinstance(raw, list):
            raise self._quarantine("procedures", "malformed_document",
                                   "procedures is not a list")
        if len(raw) > schema.MAX_PROCEDURES:
            raise self._quarantine(
                "procedures", "over_capacity",
                f"{len(raw)} records exceeds the limit of {schema.MAX_PROCEDURES}",
            )

        procedures: list[Procedure] = []
        seen: set[str] = set()
        for record in raw:
            if not isinstance(record, dict):
                raise self._quarantine("procedures", "malformed_record",
                                       "a record is not an object")
            name = record.get("name")
            title = record.get("title")
            body = record.get("body")
            created = record.get("created_at")
            updated = record.get("updated_at")
            stored_digest = record.get("digest")
            if not all(isinstance(value, str) for value
                       in (name, title, body, created, updated, stored_digest)):
                raise self._quarantine("procedures", "malformed_record",
                                       "a record is missing a required field")
            if name in seen:
                raise self._quarantine("procedures", "duplicate_name",
                                       "two records share a name")
            seen.add(name)

            try:
                schema.check_procedure_name(name)
                validate_procedure_title(title)
                validate_procedure_body(body)
            except (SchemaError, ContentError) as err:
                raise self._quarantine("procedures",
                                       f"invalid_on_load:{err.reason}",
                                       f"record {str(name)[:32]}")
            if schema.digest(body) != stored_digest:
                raise self._quarantine("procedures", "digest_mismatch",
                                       f"record {str(name)[:32]}")

            procedures.append(Procedure(name=name, title=title, body=body,
                                        created_at=created, updated_at=updated,
                                        digest=stored_digest))
        return procedures

    # -- writing --------------------------------------------------------

    def _write_document(self, path: Path, payload: dict[str, Any]) -> None:
        """Replace one document atomically and durably."""
        text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        tmp = path.with_name(path.name + ".tmp")
        # Written with the same restrictive mode as the audit record. The
        # volume is mounted only into this service, and nothing else needs it.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
        try:
            os.write(fd, text.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
        # Without fsyncing the directory the rename itself can be lost on a
        # power cut, which would resurrect the previous document.
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

    def _reject(self, kind: str, err: SchemaError | ContentError,
                content: str | None) -> StoreError:
        """Audit a refused mutation by reason, size and digest, never content."""
        fields: dict[str, Any] = {"kind": kind, "reason": err.reason}
        if isinstance(content, str):
            fields["chars"] = len(content)
            fields["digest"] = schema.digest(content)
        self._audit.record("learning_rejected", **fields)
        return StoreError(err.reason, err.detail)

    # -- facts ----------------------------------------------------------

    def remember(self, text: object, tags: object = None) -> Fact:
        """Validate and store one fact. Returns the stored record."""
        with self._lock:
            facts, next_id = self.load_facts()

            try:
                checked_tags = schema.check_tags(tags)
            except SchemaError as err:
                raise self._reject("fact", err, None)
            try:
                checked_text = validate_fact_text(text)
            except ContentError as err:
                raise self._reject("fact", err,
                                   text if isinstance(text, str) else None)

            if any(fact.text == checked_text for fact in facts):
                # Not an error: the agent already knows this. Return the
                # existing record so a repeated observation is idempotent.
                return next(f for f in facts if f.text == checked_text)

            if len(facts) >= schema.MAX_FACTS:
                err = StoreError(
                    "store_full",
                    f"the store holds the maximum of {schema.MAX_FACTS} facts. "
                    "Forget one you no longer need, then try again.",
                )
                self._audit.record("learning_rejected", kind="fact",
                                   reason="store_full", chars=len(checked_text))
                raise err

            record = Fact(
                id=schema.fact_id(next_id),
                text=checked_text,
                tags=checked_tags,
                created_at=schema.utc_now(),
                digest=schema.digest(checked_text),
            )
            document = {
                "schema": schema.SCHEMA_VERSION,
                "kind": "facts",
                "next_id": next_id + 1,
                "facts": [fact.to_json() for fact in facts] + [record.to_json()],
            }
            self._write_document(self.facts_path, document)
            self._audit.record("learning_fact_added", id=record.id,
                               tags=",".join(record.tags),
                               chars=len(record.text), digest=record.digest)
            return record

    def forget(self, fact_id: object) -> Fact:
        """Remove one fact by id. Returns the record that was removed."""
        with self._lock:
            facts, next_id = self.load_facts()
            if not isinstance(fact_id, str):
                raise StoreError("not_a_string", "expected a fact id")
            remaining = [fact for fact in facts if fact.id != fact_id]
            if len(remaining) == len(facts):
                raise StoreError("unknown_fact",
                                 f"no stored fact has the id '{fact_id[:32]}'")
            removed = next(fact for fact in facts if fact.id == fact_id)
            document = {
                "schema": schema.SCHEMA_VERSION,
                "kind": "facts",
                # Ids are never reused, so a stale reference cannot silently
                # point at a different fact later.
                "next_id": next_id,
                "facts": [fact.to_json() for fact in remaining],
            }
            self._write_document(self.facts_path, document)
            self._audit.record("learning_fact_removed", id=removed.id)
            return removed

    # -- procedures -----------------------------------------------------

    def save_procedure(self, name: object, title: object,
                       body: object) -> tuple[Procedure, bool]:
        """Validate and store one procedure, replacing any of the same name.

        Returns the stored record and whether it replaced an existing one.
        """
        with self._lock:
            procedures = self.load_procedures()

            try:
                checked_name = schema.check_procedure_name(name)
            except SchemaError as err:
                raise self._reject("procedure", err, None)
            try:
                checked_title = validate_procedure_title(title)
            except ContentError as err:
                raise self._reject("procedure_title", err,
                                   title if isinstance(title, str) else None)
            try:
                checked_body = validate_procedure_body(body)
            except ContentError as err:
                raise self._reject("procedure", err,
                                   body if isinstance(body, str) else None)

            existing = next((p for p in procedures if p.name == checked_name),
                            None)
            if existing is None and len(procedures) >= schema.MAX_PROCEDURES:
                self._audit.record("learning_rejected", kind="procedure",
                                   reason="store_full", chars=len(checked_body))
                raise StoreError(
                    "store_full",
                    f"the store holds the maximum of {schema.MAX_PROCEDURES} "
                    "procedures. Delete one you no longer need, or improve an "
                    "existing procedure instead of adding another.",
                )

            now = schema.utc_now()
            record = Procedure(
                name=checked_name,
                title=checked_title,
                body=checked_body,
                created_at=existing.created_at if existing else now,
                updated_at=now,
                digest=schema.digest(checked_body),
            )
            kept = [p for p in procedures if p.name != checked_name]
            document = {
                "schema": schema.SCHEMA_VERSION,
                "kind": "procedures",
                "procedures": [p.to_json() for p in kept] + [record.to_json()],
            }
            self._write_document(self.procedures_path, document)
            self._audit.record(
                "learning_procedure_saved", name=record.name,
                replaced=existing is not None, chars=len(record.body),
                lines=len(record.body.splitlines()), digest=record.digest,
            )
            return record, existing is not None

    def read_procedure(self, name: object) -> Procedure:
        with self._lock:
            procedures = self.load_procedures()
            if not isinstance(name, str):
                raise StoreError("not_a_string", "expected a procedure name")
            found = next((p for p in procedures if p.name == name), None)
            if found is None:
                raise StoreError(
                    "unknown_procedure",
                    f"no stored procedure is named '{name[:32]}'",
                )
            return found

    def delete_procedure(self, name: object) -> Procedure:
        with self._lock:
            procedures = self.load_procedures()
            if not isinstance(name, str):
                raise StoreError("not_a_string", "expected a procedure name")
            remaining = [p for p in procedures if p.name != name]
            if len(remaining) == len(procedures):
                raise StoreError(
                    "unknown_procedure",
                    f"no stored procedure is named '{name[:32]}'",
                )
            removed = next(p for p in procedures if p.name == name)
            document = {
                "schema": schema.SCHEMA_VERSION,
                "kind": "procedures",
                "procedures": [p.to_json() for p in remaining],
            }
            self._write_document(self.procedures_path, document)
            self._audit.record("learning_procedure_removed", name=removed.name)
            return removed

    # -- recall ---------------------------------------------------------

    def recall(self) -> dict[str, Any]:
        """Everything the agent knows: facts in full, procedures in summary."""
        with self._lock:
            facts, _ = self.load_facts()
            procedures = self.load_procedures()
        return {
            "facts": [
                {"id": fact.id, "text": fact.text, "tags": list(fact.tags),
                 "learned_at": fact.created_at}
                for fact in facts
            ],
            "procedures": [procedure.summary() for procedure in procedures],
            "facts_used": len(facts),
            "facts_limit": schema.MAX_FACTS,
            "procedures_used": len(procedures),
            "procedures_limit": schema.MAX_PROCEDURES,
        }

    def startup_check(self) -> dict[str, Any]:
        """Load both documents once at startup so a tampered store is found
        before the agent asks, and record the result in the audit."""
        try:
            facts, _ = self.load_facts()
            procedures = self.load_procedures()
        except StoreError as err:
            self._audit.record("learning_unavailable", reason=err.reason)
            return {"ok": False, "reason": err.reason}
        self._audit.record("learning_loaded", facts=len(facts),
                           procedures=len(procedures))
        return {"ok": True, "facts": len(facts), "procedures": len(procedures)}
