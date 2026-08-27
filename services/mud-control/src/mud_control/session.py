# Copyright (C) 2026 Cody Nicholson
# SPDX-License-Identifier: LGPL-2.1-only
"""The turn-state protocol and the single game session.

This is the layer that decides whether the model may act. The transport below
it moves bytes and knows nothing about turns; this file knows nothing about
pseudo-terminals. Keeping them apart is deliberate: gameplay policy does not
belong inside a byte transport.

The protocol, from the design section 4.4:

    READY         the latest observation is settled, nothing in flight
                  -> one mud_act, or observation/status
    COMMAND_SENT  one validated command has been written
                  -> observation/status only
    OBSERVING     output is arriving and accumulating
                  -> observation/status only

`mud_act` is accepted only in READY. Return to READY happens only on a
recognised prompt or the documented quiet window. Prompt loss, timeout,
disconnect and ambiguity are reported explicitly and never silently unlock
command submission.

Atomicity is the part that is easy to get subtly wrong. Checking the state and
then writing the command as two separate awaits leaves a window in which a
second concurrent mud_act passes the same check. The check, the transition and
the write therefore happen together under one lock, so the state has already
left READY before any other caller can test it. MCP-04 exists for this.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass, field
from enum import Enum

from .limits import SessionBudget, SessionLimits, StopReason
from .playlog import PlayLog
from .prompts import PromptKind
from .state import TERMINAL, LinkState, TransportFault
from .transport import Observation, TintinTransport
from .validate import ValidationError, validate


class TurnState(str, Enum):
    READY = "READY"
    COMMAND_SENT = "COMMAND_SENT"
    OBSERVING = "OBSERVING"
    #: Not part of the design's three-state protocol. It exists so that a
    #: link which has failed can never be represented as one of the three
    #: working states, which would make a dead session look actionable.
    UNAVAILABLE = "UNAVAILABLE"


class SessionError(Exception):
    """A refusal the caller is allowed to see. Never carries secrets."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


@dataclass(slots=True)
class TurnResult:
    """What happened to one mud_act."""

    accepted: bool
    turn_state: TurnState
    reason: str = ""
    detail: str = ""


class MudSession:
    """One game session, its turn state, and the only path to the PTY."""

    def __init__(self, transport: TintinTransport, audit=None,
                 limits: SessionLimits | None = None,
                 play_log: PlayLog | None = None) -> None:
        self._transport = transport
        self._audit = audit
        #: The spectator feed, for command markers only. The game's own output
        #: is written by the transport, which is the layer that cleans it.
        self._play_log = play_log
        self._lock = asyncio.Lock()
        self._turn = TurnState.UNAVAILABLE
        self._session_id: str | None = None
        self._commands_sent = 0
        self._started_at: float | None = None
        self._last_prompt = PromptKind.NONE
        self._limits = limits or SessionLimits()
        self._budget = SessionBudget(limits=self._limits)
        self._stop_recorded = False

    # -- identity -------------------------------------------------------

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def turn_state(self) -> TurnState:
        return self._turn

    @property
    def commands_sent(self) -> int:
        return self._commands_sent

    def _require_session(self, session_id: object) -> None:
        """Reject stale, absent or forged session identifiers (MCP-10).

        Compared with `secrets.compare_digest` so a caller cannot learn the
        identifier a character at a time from timing.
        """
        if self._session_id is None:
            raise SessionError("no_session", "no session is open")
        if not isinstance(session_id, str):
            raise SessionError("invalid_session", "session id must be a string")
        if not secrets.compare_digest(session_id, self._session_id):
            raise SessionError("invalid_session",
                               "session id does not match the open session")

    # -- lifecycle ------------------------------------------------------

    async def connect(self) -> dict:
        async with self._lock:
            if self._session_id is not None and self._turn is not TurnState.UNAVAILABLE:
                if self._transport.state in TERMINAL:
                    # The identifier outlived the link. Drop it and open a new
                    # session below rather than handing back a session whose
                    # PTY is gone.
                    self._session_id = None
                    self._turn = TurnState.UNAVAILABLE
                else:
                    # The live session is returned rather than refused.
                    #
                    # Phase 6 found this by running two agent sessions in a row:
                    # the second process called mud_connect, got
                    # "already_connected", and had no way to learn the
                    # identifier of the session that was still open. The game
                    # was unreachable until an operator restarted the service,
                    # which is a poor answer for a character meant to persist
                    # across sessions.
                    #
                    # This costs nothing that MCP-10 protects: that test is
                    # about a *stale or forged* identifier being refused, and
                    # `_require_session` still refuses one. Any caller that can
                    # call mud_connect could already open a session; letting it
                    # rejoin the one that exists grants no new authority, and
                    # the turn state comes back unchanged, so the state machine
                    # still decides whether a command is allowed.
                    self._emit("session_resumed",
                               turn_state=self._turn.value,
                               commands_sent=self._commands_sent)
                    return {**self.status(), "resumed": True}
            # A transport that has been closed cannot be start()ed again, so a
            # second mud_connect after a mud_disconnect must go through
            # reconnect(), which resets the link to IDLE first. Without this
            # the service supported exactly one session per process lifetime
            # and every later connect returned a transport fault.
            if self._transport.state is LinkState.IDLE:
                await self._transport.start()
            else:
                await self._transport.reconnect()
            prompt = await self._transport.authenticate()
            self._session_id = secrets.token_urlsafe(16)
            self._started_at = time.time()
            self._commands_sent = 0
            # A new session is a new outing with a fresh budget, so an operator
            # can end one and start another without restarting the service.
            self._budget = SessionBudget(limits=self._limits)
            self._stop_recorded = False
            self._last_prompt = prompt
            self._turn = self._state_for(prompt)
            self._emit("session_opened", turn_state=self._turn.value,
                       prompt=prompt.value)
            return {**self.status(), "resumed": False}

    async def disconnect(self, session_id: str) -> dict:
        async with self._lock:
            self._require_session(session_id)
            await self._transport.disconnect()
            self._turn = TurnState.UNAVAILABLE
            self._emit("session_closed")
            result = self.status()
            self._session_id = None
            return result

    # -- the turn protocol ----------------------------------------------

    async def act(self, session_id: str, command: object,
                  intent: str = "") -> TurnResult:
        """Send exactly one validated command, only while READY."""
        async with self._lock:
            self._require_session(session_id)

            # A stopped session stays stopped. Checked before anything else,
            # including validation, because once a session has ended the only
            # honest answer to any command is why it ended.
            stopped = self._budget.check_before()
            if stopped is not None:
                return self._stop_result(stopped)

            # Order matters. Validation runs before the state check so a
            # malformed command is refused for what it is rather than being
            # masked by a state refusal, and neither path touches the PTY.
            try:
                checked = validate(command)
            except ValidationError as err:
                # Reason and length only. The rejected string is not recorded:
                # it is hostile input by definition, and copying it verbatim
                # into a file a human later reads adds risk without adding
                # information the reason does not already carry.
                self._emit("command_rejected", reason=err.reason,
                           detail=err.detail,
                           length=len(command) if isinstance(command, str) else -1)
                loop = self._budget.record_rejected()
                if loop is not None:
                    return self._stop_result(loop)
                return TurnResult(False, self._turn, err.reason, err.detail)

            # A command without a stated intent is not observable play. The
            # design requires one concise visible intent per command, and the
            # Phase 8 spectator surface has nothing to show without it. Checked
            # after command validation so hostile input is still diagnosed for
            # what it is, and before the state gate so the reason is specific.
            if not isinstance(intent, str) or not intent.strip():
                self._emit("command_rejected", reason="intent_required")
                return TurnResult(False, self._turn, "intent_required",
                                  "state your intent in one short line")

            # The link is checked before the turn state, because a lost link is
            # the stronger fact. The other order reported a dead game as
            # "not_ready: state is COMMAND_SENT", which is true and useless: it
            # tells the agent to wait for a turn that can never come, and hides
            # a disconnect behind a word that normally means "be patient".
            # Phase 8 found this by stopping the game under a live session.
            if self._transport.state in TERMINAL:
                self._turn = TurnState.UNAVAILABLE
                self._emit("command_rejected", reason="link_unavailable",
                           link_state=self._transport.state.value)
                # The session stops, and is recorded as stopping, but the
                # reason the caller sees stays `link_unavailable`: it is the
                # more specific fact, it is the contract MCP callers have had
                # since Phase 3, and `transport_fault` is the category it
                # belongs to rather than a better description of it.
                self._budget.stop(StopReason.TRANSPORT)
                self._stop_result(StopReason.TRANSPORT)
                return TurnResult(False, self._turn, "link_unavailable",
                                  self._transport.state.value)

            if self._turn is not TurnState.READY:
                self._emit("command_rejected", reason="not_ready",
                           turn_state=self._turn.value)
                return TurnResult(False, self._turn, "not_ready",
                                  f"state is {self._turn.value}, not READY")

            # Leave READY before the write, and before releasing the lock, so
            # no concurrent caller can observe READY for the same turn.
            self._turn = TurnState.COMMAND_SENT
            try:
                # The validated value is what is sent. Not the argument.
                await self._transport.send_line(checked)
            except TransportFault as err:
                self._turn = TurnState.UNAVAILABLE
                self._emit("command_failed", reason="transport_fault",
                           link_state=err.state.value)
                return TurnResult(False, self._turn, "transport_fault",
                                  err.state.value)

            self._commands_sent += 1
            self._emit("command_sent", command=checked, intent=intent,
                       turn_state=self._turn.value)
            # The marker for the spectator feed is written here rather than in
            # the transport's send_line, which is also what types the character
            # password during login. `checked` has passed the validator and can
            # only be a command the model chose, so the credential path cannot
            # reach the feed by construction.
            if self._play_log is not None:
                self._play_log.command(checked)

            # Counted after the write, because the command did reach the game
            # and the turn is spent whatever the budget now says. A stop here
            # ends the session with this command as its last, rather than
            # discarding a command the world has already seen.
            loop = self._budget.record_accepted(checked)
            if loop is not None:
                # Through the same guard as every other stop. Emitting here
                # directly announced no_progress twice: once for the command
                # that tripped it and again for the next refusal, which the
                # audit showed and the turn-limit test did not, because that
                # path never passes through here.
                self._stop_result(loop)
                return TurnResult(True, self._turn, loop.value,
                                  self._budget.detail(loop))
            return TurnResult(True, self._turn)

    def _stop_result(self, reason: StopReason) -> TurnResult:
        """Refuse a command because the session is over, and say why once."""
        if not self._stop_recorded:
            self._stop_recorded = True
            self._emit("session_stopped", reason=reason.value,
                       turns=self._budget.turns)
        return TurnResult(False, self._turn, reason.value,
                          self._budget.detail(reason))

    def stop(self, reason: StopReason = StopReason.OPERATOR) -> dict:
        """End the session from outside the turn loop.

        The checklist requires that an operator can stop a session
        immediately. Killing the agent process does that too, but this ends it
        at the boundary, so the reason is recorded and the game session is not
        left waiting for a client that has gone.
        """
        self._budget.stop(reason)
        self._stop_result(reason)
        return self.status()

    async def observe(self, session_id: str, timeout: float | None = None,
                      limit: int = 8192) -> Observation:
        """Read buffered output and settle the turn state.

        Permitted in every state, because observing never authorises an
        action. Unsolicited output, including automatic combat rounds, is
        returned here without unlocking command submission: only a settled
        prompt or the quiet window does that.
        """
        self._require_session(session_id)
        try:
            obs = await self._transport.observe(timeout=timeout, limit=limit)
            obs = await self._advance_plumbing(obs, timeout, limit)
        except TransportFault as err:
            async with self._lock:
                self._turn = TurnState.UNAVAILABLE
            self._emit("transport_fault", reason=err.state.value)
            raise SessionError("transport_fault", err.state.value) from None

        async with self._lock:
            self._settle_locked(obs)
        return obs

    #: What the trusted side answers, and with what. Neither is a gameplay
    #: decision: one is the MOTD asking for a keypress, the other is the
    #: account menu whose only continuing option is to enter the game.
    _PLUMBING = {
        PromptKind.PRESS_RETURN: "",
        PromptKind.MENU: "1",
    }

    async def _advance_plumbing(self, obs: Observation, timeout, limit):
        """Answer the prompts the agent cannot and should not answer.

        After the agent chooses sex and class, DikuMUD shows the MOTD and waits
        for a keypress, then offers the account menu. `mud_act` cannot answer
        either: an empty command is refused by the validator, and neither is a
        move in the game. Left alone the session would sit at the MOTD forever,
        one keypress short of a playable character.

        `authenticate()` already answers these during a normal login. This is
        the same handling for the case where control has passed to the agent
        mid-creation and then comes back.

        Bounded at two steps, because that is the length of the real sequence
        and a loop here would type into the game indefinitely.
        """
        for _ in range(2):
            answer = self._PLUMBING.get(obs.prompt)
            if answer is None:
                return obs
            self._emit("plumbing_answered", prompt=obs.prompt.value)
            await self._transport.send_line(answer)
            obs = await self._transport.observe(timeout=timeout, limit=limit)
        return obs

    def _settle_locked(self, obs: Observation) -> None:
        """Decide whether this observation returns the turn to READY."""
        self._last_prompt = obs.prompt

        if self._transport.state in TERMINAL:
            self._turn = TurnState.UNAVAILABLE
            # Recorded here as well, so a session that dies while the agent is
            # observing reports why without waiting for the next command. The
            # spectator reads the stop reason from status.
            self._budget.stop(StopReason.TRANSPORT)
            return

        if obs.prompt is PromptKind.NONE:
            # Nothing settled. If a command is outstanding we are watching its
            # output; either way the model may not act.
            if self._turn is TurnState.COMMAND_SENT:
                self._turn = TurnState.OBSERVING
            elif self._turn is TurnState.READY:
                self._turn = TurnState.OBSERVING
            return

        # A recognised prompt settles the turn. Identity and plumbing prompts
        # are not a licence to play, so they stay OBSERVING: the trusted side
        # answers those, and the model never sees a turn at one.
        #
        # Character creation is the exception, and it is the design's, not a
        # convenience. Section 9 has the agent making "all non-secret
        # character-creation choices - such as race, class, sex, and other
        # ordinary game prompts - through the same one-command MCP boundary",
        # and E2E-01 tests exactly that. At a sex or class prompt the server is
        # waiting for one input from the player, which is what READY means; the
        # bootstrap has already finished the part that carries the secret.
        #
        # Without this the agent cannot answer, mud_act refuses every attempt
        # as not_ready, and a new character can never be finished.
        self._turn = self._state_for(obs.prompt)

    #: Prompts at which the server is waiting for one input from the player.
    #: The in-game prompt, and the two creation choices the design gives the
    #: agent. Identity and plumbing prompts are deliberately absent.
    _AGENT_TURN_PROMPTS = (PromptKind.GAME, PromptKind.SEX, PromptKind.CLASS)

    def _state_for(self, prompt: PromptKind) -> TurnState:
        """One rule, used by connect and by settling alike.

        These were two rules until Phase 8, and they disagreed: a first login
        that stopped at the sex prompt reported OBSERVING from connect and
        READY from the next observe. The agent is told it may not act, and
        then may. One rule, one answer.
        """
        return (TurnState.READY if prompt in self._AGENT_TURN_PROMPTS
                else TurnState.OBSERVING)

    async def force_unavailable(self, reason: str) -> None:
        async with self._lock:
            self._turn = TurnState.UNAVAILABLE
            self._emit("session_unavailable", reason=reason)

    # -- status ---------------------------------------------------------

    def status(self) -> dict:
        """Non-secret status. Deliberately excludes host, port and identity."""
        return {
            "session_id": self._session_id,
            "turn_state": self._turn.value,
            "link_state": self._transport.state.value,
            "prompt": self._last_prompt.value,
            "unread_chars": self._transport.unread,
            "commands_sent": self._commands_sent,
            "uptime_seconds": (round(time.time() - self._started_at, 1)
                               if self._started_at else 0.0),
            # So the agent can see how much of its session is left, and the
            # spectator surface has a stop reason to show when it ends.
            **self._budget.summary(),
        }

    # -- audit ----------------------------------------------------------

    def _emit(self, event: str, **fields) -> None:
        if self._audit is None:
            return
        self._audit.record(event, session_id=self._session_id, **fields)
