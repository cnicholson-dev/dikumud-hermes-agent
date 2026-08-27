# Copyright (C) 2026 Cody Nicholson
# SPDX-License-Identifier: LGPL-2.1-only
"""Bounded factual memory and inert declarative procedures.

The store this package owns is the agent's only persistence path.
`public-documentation/DESIGN.md` section 8 records why it lives here rather than
in Hermes' own memory and skills facilities, and fixes the rules it enforces.

Nothing in this package imports the transport, the session state machine or the
credential path. It is given a directory and an audit log, and it holds no PTY
handle, no game session and no secret.
"""

from .schema import SCHEMA_VERSION

__all__ = ["SCHEMA_VERSION"]
