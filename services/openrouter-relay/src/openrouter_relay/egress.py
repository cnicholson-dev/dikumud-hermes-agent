# Copyright (C) 2026 Cody Nicholson
# SPDX-License-Identifier: LGPL-2.1-only
"""Refuse to start when this container's egress is not restricted.

SEC-07 requires the relay to reach the OpenRouter path and nothing else.
Compose cannot express that, so the control is host firewall policy
(`scripts/restrict-relay-egress`). That leaves a gap Phase 7 named and did not
close: the rules are host state, they do not survive a reboot on their own, and
`docker compose up` does not apply them.

The gap that matters is not that the rules can be missing. It is that their
absence has **no symptom**: the relay's own policy pins the upstream URL, so a
relay with unrestricted egress serves the demonstration exactly as well as a
restricted one, and the deployment looks healthy while a control the security
report claims is in force is simply gone.

So the stack checks for itself. At startup the relay opens a TCP connection to
an address it must not be able to reach. If the connection succeeds, the
firewall is not in place, and the relay exits rather than run in a state its
own documentation says it is not in.

This is a genuine control rather than a warning, which is what `SECURITY.md`
section 12 asks for: "Do not replace a failed technical control with a prompt
instruction or warning in documentation."

The canary is an IP literal, never a hostname: DNS is denied by the same rules,
so a hostname would fail to resolve and be indistinguishable from a blocked
connection. 1.1.1.1:443 answers from anywhere with a route out, which is the
condition being tested.
"""

from __future__ import annotations

import os
import socket


class EgressNotRestricted(RuntimeError):
    """The container can reach the open Internet. Refuse to serve."""


#: Reachable from any host with general egress, and denied by the project's
#: rules. Overridable for a deployment whose egress policy differs.
DEFAULT_CANARY_HOST = "1.1.1.1"
DEFAULT_CANARY_PORT = 443

#: Short: a REJECT comes back immediately, and a DROP-based policy should not
#: hold startup for long either.
DEFAULT_TIMEOUT = 3.0


def canary_reachable(host: str, port: int, timeout: float) -> bool:
    """True when a TCP connection to the canary completes."""
    try:
        connection = socket.create_connection((host, port), timeout)
    except OSError:
        # Refused, rejected, unroutable, or timed out: all mean the path is
        # closed, which is the state we want.
        return False
    connection.close()
    return True


def enforce(environ: dict[str, str] | None = None) -> str:
    """Check egress and return a one-line description of the result.

    Raises `EgressNotRestricted` when the canary is reachable.
    """
    env = os.environ if environ is None else environ

    if env.get("RELAY_EGRESS_SELFTEST", "1") not in ("1", "true", "yes"):
        # An operator may switch this off for a deployment that restricts
        # egress by some other means. It is opt-out rather than opt-in so that
        # forgetting to configure anything fails closed.
        return "egress self-test disabled by RELAY_EGRESS_SELFTEST"

    host = env.get("RELAY_EGRESS_CANARY_HOST", DEFAULT_CANARY_HOST)
    port = int(env.get("RELAY_EGRESS_CANARY_PORT", DEFAULT_CANARY_PORT))
    timeout = float(env.get("RELAY_EGRESS_CANARY_TIMEOUT", DEFAULT_TIMEOUT))

    if canary_reachable(host, port, timeout):
        raise EgressNotRestricted(
            f"This container reached {host}:{port}, so its egress is not "
            "restricted and SEC-07 is not satisfied. The relay refuses to "
            "start in that state, because its own policy would keep the "
            "deployment looking healthy while the network control the "
            "security report claims is in force is absent.\n"
            "Apply it on the Docker host:\n"
            "    sudo scripts/restrict-relay-egress apply\n"
            "or set RELAY_EGRESS_SELFTEST=0 if egress is restricted by "
            "another mechanism."
        )
    return f"egress restricted: {host}:{port} unreachable"
