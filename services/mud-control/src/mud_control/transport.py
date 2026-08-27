# Copyright (C) 2026 Cody Nicholson
# SPDX-License-Identifier: LGPL-2.1-only
"""Deterministic PTY harness owning one headless TinTin++ session.

Design constraints this implements:

* Standard library only: `pty`, `os`, `asyncio`. No pexpect. This transport must
  accumulate unsolicited output (combat rounds arrive with no command
  outstanding) and expose one explicit state machine. An expect-style library
  would add a second, blocking notion of "what we are waiting for" and two
  sources of truth about prompt state.
* The target is fixed trusted configuration. Nothing about the host, port,
  character or credential can be supplied by a caller.
* Credentials are read from a file and written to the PTY. They are never
  logged, never returned, and never placed in an exception message.
* Ambiguity fails closed. Timeouts, malformed transport and disconnects each
  produce an explicit terminal state rather than a guess.
"""

from __future__ import annotations

import asyncio
import fcntl
import os
import pty
import re
import signal
import struct
import termios
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import prompts, sanitize
from .buffer import OutputBuffer
from .playlog import PlayLog
from .prompts import PromptKind
from .state import TERMINAL, WRITABLE, LinkState, TransportFault

#: Whatever follows a password prompt on the same line. The second line of
#: defence behind exact-value redaction, for the case where the password is
#: split across two reads and so cannot be matched as a whole.
_PASSWORD_ECHO = re.compile(r"(?i)(password\s*:)[^\S\n]*\S+")

#: A fresh PTY reports a 0x0 window. TinTin++ draws into that geometry and
#: segfaults immediately, which cost real time to find during Phase 2 and is
#: invisible in its documentation. The size itself is arbitrary; it only has
#: to be non-degenerate. 500 columns keeps the client from wrapping game text,
#: so what we parse is what DikuMUD actually sent.
PTY_ROWS = 24
PTY_COLS = 500


@dataclass(frozen=True, slots=True)
class TransportConfig:
    """Trusted configuration. Never populated from model input."""

    host: str
    port: int
    character: str
    credential_path: Path
    tintin_path: Path = Path("/opt/mud-control/bin/tt++")
    session_config: Path = Path("/opt/mud-control/config/session.tin")
    session_name: str = "diku"
    runtime_dir: Path = Path("/tmp/mudctl")

    #: No new bytes for this long means the server has stopped talking. Used
    #: only when no prompt was recognised; a recognised prompt settles at once.
    #:
    #: Measured, not guessed. DikuMUD's game loop runs at OPT_USEC = 250000,
    #: i.e. four passes per second, so output legitimately arrives in bursts
    #: up to 250ms apart. A window at or below that settles mid-sentence: at
    #: 0.35s the harness repeatedly gave up in the middle of the MOTD. 0.75s
    #: is three server ticks, which cleared it.
    quiet_window: float = 0.75
    #: Hard ceiling on waiting for output to settle. Exceeding it is a fault,
    #: not a silent return, because an unsettled link must never look ready.
    settle_timeout: float = 10.0
    #: How long to wait for TinTin++ to report the session connected.
    connect_timeout: float = 20.0

    @classmethod
    def from_env(cls) -> "TransportConfig":
        return cls(
            host=os.environ["MUD_CONTROL_HOST"],
            port=int(os.environ.get("MUD_CONTROL_PORT", "4000")),
            character=os.environ["MUD_CONTROL_CHARACTER"],
            credential_path=Path(os.environ["MUD_CONTROL_CREDENTIAL_FILE"]),
            tintin_path=Path(os.environ.get("MUD_CONTROL_TINTIN",
                                            "/opt/mud-control/bin/tt++")),
            session_config=Path(os.environ.get("MUD_CONTROL_SESSION_CONFIG",
                                               "/opt/mud-control/config/session.tin")),
            runtime_dir=Path(os.environ.get("HOME", "/tmp/mudctl")),
        )


@dataclass(slots=True)
class Observation:
    """What a caller gets back from observing the link."""

    text: str
    prompt: PromptKind
    state: LinkState
    events: list[str] = field(default_factory=list)
    truncated: bool = False

    @property
    def settled(self) -> bool:
        return self.prompt is not PromptKind.NONE


class TintinTransport:
    """Owns the TinTin++ process, its PTY, and the link state."""

    def __init__(self, config: TransportConfig,
                 raw_sink: "Callable[[bytes], None] | None" = None,
                 play_log: "PlayLog | None" = None) -> None:
        #: Optional tap on the raw PTY byte stream, before any cleaning.
        #: Used to record byte-accurate fixtures and, from Phase 3, to feed
        #: the append-only audit record. It never sees anything the transport
        #: does not, and it is never populated from model input.
        self._raw_sink = raw_sink
        #: Optional spectator feed, written from the cleaned stream rather than
        #: the raw one. Absent in tests that do not ask for it, and absent in a
        #: deployment that sets no path for it.
        self._play_log = play_log
        self._cfg = config
        self._pid: int | None = None
        self._fd: int | None = None
        self._state = LinkState.IDLE
        self._buffer = OutputBuffer()
        self._events: list[str] = []
        self._settle_tail = ""
        self._reader_attached = False
        self._pending_echo: str | None = None
        #: Byte strings scrubbed from the raw stream before anything
        #: downstream sees it. TinTin++ echoes typed input, so the character
        #: password appears verbatim in the PTY output, and SECURITY.md
        #: section 8 requires that no audit event, log or fixture ever contain
        #: credential material.
        #:
        #: Phase 6 found that echo suppression alone was not enough for the
        #: *observation* path. It matches the echoed line exactly and only as
        #: the first line of a chunk, and on a reconnect the server's prompt
        #: and the echo arrived in one read:
        #:
        #:     By what name do you wish to be known? Password: <the password>
        #:
        #: so the echo was not at the start, the exact match failed, and the
        #: password reached the model through mud_observe. Redaction is now
        #: applied to the cleaned text as well, unconditionally and without
        #: reference to position, which is what MCP-09 actually requires.
        self._redactions: list[bytes] = []
        self._data = asyncio.Event()

    # -- properties -----------------------------------------------------

    @property
    def state(self) -> LinkState:
        return self._state

    @property
    def unread(self) -> int:
        return self._buffer.unread

    @property
    def pid(self) -> int | None:
        return self._pid

    # -- lifecycle ------------------------------------------------------

    def _session_file(self) -> Path:
        """Write the startup file: connect, then apply the fixed config.

        Order matters. TinTin++ CONFIG options are per-session, so applying
        them before #session would set them on the global pseudo-session and
        leave the real link unconfigured, including the VERBATIM and CHILD
        LOCK settings that stop #system from being interpreted.
        """
        self._cfg.runtime_dir.mkdir(parents=True, exist_ok=True)
        static = self._cfg.session_config.read_text(encoding="utf-8")
        path = self._cfg.runtime_dir / "session.generated.tin"
        path.write_text(
            f"#session {self._cfg.session_name} {self._cfg.host} {self._cfg.port}\n"
            f"{static}",
            encoding="utf-8",
        )
        path.chmod(0o600)
        return path

    async def start(self) -> None:
        """Fork TinTin++ onto a PTY and wait for the session to connect."""
        if self._state is not LinkState.IDLE:
            raise TransportFault(f"cannot start from {self._state.value}")

        session_file = self._session_file()
        self._state = LinkState.STARTING

        pid, fd = pty.fork()
        if pid == 0:  # child
            try:
                os.environ["HOME"] = str(self._cfg.runtime_dir)
                os.environ["TERM"] = "xterm"
                os.execv(str(self._cfg.tintin_path),
                         ["tt++", "-G", "-T", "-s", "-r", str(session_file)])
            except BaseException:
                os._exit(127)

        self._pid = pid
        self._fd = fd
        self._set_winsize()
        self._disable_echo()
        os.set_blocking(fd, False)
        asyncio.get_running_loop().add_reader(fd, self._on_readable)
        self._reader_attached = True

        try:
            await self._await_connected()
        except TransportFault:
            await self.close()
            raise
        self._state = LinkState.CONNECTED

    def _set_winsize(self) -> None:
        assert self._fd is not None
        fcntl.ioctl(self._fd, termios.TIOCSWINSZ,
                    struct.pack("HHHH", PTY_ROWS, PTY_COLS, 0, 0))

    def _disable_echo(self) -> None:
        """Stop the PTY echoing our own writes back at us.

        A PTY echoes by default, so every line written for the game came back
        on the read side and was indistinguishable from server output. That
        would put our own input into observations, and worse, would echo the
        password into the buffer during authentication.
        """
        assert self._fd is not None
        attrs = termios.tcgetattr(self._fd)
        attrs[3] &= ~(termios.ECHO | termios.ECHONL)  # lflag
        termios.tcsetattr(self._fd, termios.TCSANOW, attrs)

    async def _await_connected(self) -> None:
        """Wait for TinTin++ to announce the session, or fail closed."""
        deadline = asyncio.get_running_loop().time() + self._cfg.connect_timeout
        while asyncio.get_running_loop().time() < deadline:
            await self._pump(0.25)
            joined = " ".join(self._events)
            if "CONNECTED" in joined.upper():
                return
            if "DIED" in joined.upper() or "REFUSED" in joined.upper():
                raise TransportFault("session failed to connect",
                                     LinkState.DISCONNECTED)
            if not self._child_alive():
                raise TransportFault("TinTin++ exited during startup",
                                     LinkState.FAULTED)
        raise TransportFault("timed out waiting for session connect",
                             LinkState.FAULTED)

    def _child_alive(self) -> bool:
        if self._pid is None:
            return False
        try:
            done, _ = os.waitpid(self._pid, os.WNOHANG)
        except ChildProcessError:
            return False
        return done == 0

    # -- reading --------------------------------------------------------

    def _on_readable(self) -> None:
        """Drain the PTY without blocking. Runs in the event loop."""
        assert self._fd is not None
        try:
            raw = os.read(self._fd, 65536)
        except BlockingIOError:
            return
        except OSError:
            # The slave side closed: TinTin++ is gone.
            self._detach_reader()
            if self._state not in TERMINAL:
                self._state = LinkState.DISCONNECTED
            self._data.set()
            return

        if not raw:
            self._detach_reader()
            if self._state not in TERMINAL:
                self._state = LinkState.DISCONNECTED
            self._data.set()
            return

        if self._raw_sink is not None:
            self._raw_sink(self._redact(raw))

        cleaned = sanitize.clean(raw)
        text = self._redact_text(self._suppress_echo(cleaned.game_text))
        if text:
            self._buffer.append(text)
            self._settle_tail = (self._settle_tail + text)[-512:]
            # The spectator's copy, written here rather than anywhere later
            # because this is the point where the stream has been cleaned, the
            # echo suppressed and the credential redacted. Anything downstream
            # would have to repeat all three. See playlog.py.
            if self._play_log is not None:
                self._play_log.write(text)
        for event in cleaned.events:
            self._events.append(event)
            self._note_event(event)
        self._data.set()

    def _redact(self, raw: bytes) -> bytes:
        """Scrub known secrets from raw bytes bound for the sink."""
        for secret in self._redactions:
            if secret and secret in raw:
                raw = raw.replace(secret, b"<redacted>")
        return raw

    def _redact_text(self, text: str) -> str:
        """Scrub credential material from text bound for an observation.

        Two rules, because they fail differently:

        1. The known password, replaced wherever it appears. Exact and
           complete, but blind to a password split across two reads.
        2. Anything following a "Password:" prompt on the same line. Covers
           the split case, at the cost of hiding whatever word follows that
           prompt, which is never game text worth keeping.
        """
        for secret in self._redactions:
            if not secret:
                continue
            text = text.replace(secret.decode("latin-1", errors="replace"),
                                "<redacted>")
        return _PASSWORD_ECHO.sub(r"\1 <redacted>", text)

    def _suppress_echo(self, text: str) -> str:
        """Drop TinTin++'s echo of the line we just sent.

        TinTin++ echoes typed input locally. Disabling the PTY's own ECHO is
        not enough, because the client re-enables raw terminal handling and
        prints the line itself. Left in place the echo would appear in
        observations as though the world had said it, and during
        authentication it would put the password into the output buffer.

        Only the first line is considered, and only an exact match, so genuine
        game text that happens to repeat the command is preserved.
        """
        if not text or self._pending_echo is None:
            return text
        echo = self._pending_echo
        stripped = text.lstrip("\n")
        leading = text[: len(text) - len(stripped)]
        if stripped.startswith(echo + "\n"):
            self._pending_echo = None
            return leading + stripped[len(echo) + 1 :]
        if stripped.rstrip("\n") == echo:
            self._pending_echo = None
            return leading
        # The echo did not arrive first; stop waiting for it rather than
        # risk stripping something later that merely looks the same.
        self._pending_echo = None
        return text

    def _note_event(self, event: str) -> None:
        """Turn TinTin++ status chatter into explicit link state."""
        upper = event.upper()
        if "DISCONNECT" in upper or "SESSION 'DIKU' DIED" in upper:
            if self._state not in TERMINAL:
                self._state = LinkState.DISCONNECTED

    def _detach_reader(self) -> None:
        if self._reader_attached and self._fd is not None:
            try:
                asyncio.get_running_loop().remove_reader(self._fd)
            except (RuntimeError, ValueError):
                pass
            self._reader_attached = False

    async def _pump(self, timeout: float) -> bool:
        """Wait up to `timeout` for any new data. True if some arrived."""
        self._data.clear()
        try:
            await asyncio.wait_for(self._data.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    # -- observation ----------------------------------------------------

    async def wait_settled(self, timeout: float | None = None) -> PromptKind:
        """Wait until the server stops talking.

        Settles on a recognised prompt immediately. Failing that, a quiet
        window with no new bytes also counts as settled, because DikuMUD does
        not always leave a prompt (the MOTD pager is one case). Exhausting the
        timeout is a fault: an unsettled link must never be reported as ready.
        """
        limit = self._cfg.settle_timeout if timeout is None else timeout
        loop = asyncio.get_running_loop()
        deadline = loop.time() + limit

        while True:
            kind = prompts.classify(self._settle_tail)
            if kind is not PromptKind.NONE:
                return kind
            if self._state in TERMINAL:
                raise TransportFault(f"link {self._state.value} while waiting",
                                     self._state)
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TransportFault("no prompt and no quiet window within timeout",
                                     LinkState.FAULTED)
            got = await self._pump(min(self._cfg.quiet_window, remaining))
            if not got and self._settle_tail.strip():
                # Nothing new for a full quiet window and we have real text.
                #
                # The condition used to be `self._buffer.unread`, which counts
                # bytes rather than screens. Immediately after connect the
                # buffer holds a few bytes of Telnet negotiation and no game
                # text at all, so a server that paused before sending its
                # banner looked settled, wait_settled returned NONE, and
                # authenticate() gave up at a prompt that arrived a moment
                # later. That is the intermittent "lands at the name prompt"
                # failure seen in Phases 6, 7 and 8, and it got worse the
                # slower the game server was to start.
                #
                # `_settle_tail` holds cleaned game text, so quiet plus text
                # means a screen the server has finished drawing, while quiet
                # plus nothing means it has not spoken yet and the right
                # response is to keep waiting until the timeout.
                return PromptKind.NONE

    async def observe(self, timeout: float | None = None,
                      limit: int = 8192) -> Observation:
        """Return buffered output, waiting for the link to settle first."""
        prompt = PromptKind.NONE
        try:
            prompt = await self.wait_settled(timeout)
        except TransportFault:
            if self._state not in TERMINAL:
                raise
        # Redacted again here, over the assembled text, and this is the check
        # that actually holds.
        #
        # Per-chunk redaction in _on_readable cannot see a secret that spans a
        # read boundary. Phase 7 measured it: with the game server just
        # restarted, one racing login in four put the password into an
        # observation, because the reads split as
        #
        #     chunk A  "...By what name...? Password: "     nothing follows
        #     chunk B  "Ph1Ver"                             no prompt, partial
        #     chunk C  "ify7\n..."                          no prompt, partial
        #
        # so neither the exact-value rule nor the prompt rule matched any
        # single chunk, while the buffer reassembled them into the intact
        # password. Redacting what is about to be served sees the whole string
        # and catches it, whatever the read boundaries were.
        #
        # Events go through the same scrub: they are client status lines, and
        # a reconnect notice has been seen carrying the tail of a login.
        text = self._redact_text(self._buffer.take(limit))
        events, self._events = [self._redact_text(e) for e in self._events], []
        obs = Observation(
            text=text,
            prompt=prompt,
            state=self._state,
            events=events,
            truncated=self._buffer.unread > 0,
        )
        return obs

    # -- writing --------------------------------------------------------

    async def send_line(self, line: str) -> None:
        """Write one line to the game.

        This is a byte transport. It rejects embedded newlines because one
        call must produce one line, but it does NOT implement the command
        validation the model is subject to: rejecting '#' prefixes, separators
        and batches is the MCP boundary's job in Phase 3. Doing it here would
        also block the bootstrap from sending a password that happens to start
        with '#'.
        """
        if self._state not in WRITABLE:
            raise TransportFault(f"cannot write while {self._state.value}",
                                 self._state)
        if "\n" in line or "\r" in line:
            raise TransportFault("send_line accepts exactly one line")
        assert self._fd is not None
        data = (line + "\n").encode("latin-1", errors="replace")
        self._pending_echo = line
        os.write(self._fd, data)
        # Answering a prompt voids it. Without this the settle tail still ends
        # on the prompt we just replied to, so the next wait_settled matches it
        # again immediately and the caller answers the same question forever.
        self._settle_tail = ""
        self._data.clear()

    async def authenticate(self) -> PromptKind:
        """Inject the character credential in response to server prompts.

        The credential is read at the moment of use and dropped immediately.
        It is never logged, never returned, and never included in a raised
        message, which is what MCP-09 will test in Phase 3.
        """
        if self._state not in WRITABLE:
            raise TransportFault(f"cannot authenticate while {self._state.value}",
                                 self._state)
        name, password = self._read_credential()
        # Register before the first write so the echo can never outrun it.
        encoded = password.encode("latin-1", errors="replace")
        if encoded not in self._redactions:
            self._redactions.append(encoded)
        self._state = LinkState.AUTHENTICATING
        try:
            for _ in range(8):
                kind = await self.wait_settled()
                if kind is PromptKind.NAME:
                    await self.send_line(name)
                elif kind is PromptKind.NAME_CONFIRM:
                    # "Did I get that right, Wren (Y/N)?" is part of the
                    # identity exchange, not a gameplay choice, so the trusted
                    # side answers it. Leaving it to the agent would ask the
                    # model to confirm a name it was never told, on the one
                    # code path where the character does not exist yet.
                    await self.send_line("y")
                elif kind in (PromptKind.PASSWORD, PromptKind.PASSWORD_NEW,
                              PromptKind.PASSWORD_CONFIRM):
                    await self.send_line(password)
                elif kind is PromptKind.PRESS_RETURN:
                    await self.send_line("")
                elif kind is PromptKind.MENU:
                    await self.send_line("1")
                elif kind is PromptKind.GAME:
                    self._state = LinkState.CONNECTED
                    return kind
                else:
                    # A creation prompt (sex, class, name confirmation) is the
                    # agent's decision, not ours. The design is explicit that
                    # the bootstrap "owns secrets only".
                    self._state = LinkState.CONNECTED
                    return kind
            raise TransportFault("authentication did not reach the game")
        finally:
            del password
            if self._state is LinkState.AUTHENTICATING:
                self._state = LinkState.CONNECTED

    #: DikuMUD's own limit, from interpreter.c. A longer password is answered
    #: with "Illegal password." and the login never completes.
    MAX_PASSWORD_LENGTH = 10

    def _read_credential(self) -> tuple[str, str]:
        raw = self._cfg.credential_path.read_text(encoding="utf-8")
        lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
        if len(lines) < 2:
            raise TransportFault("credential file must hold a name and a password")
        if len(lines[1]) > self.MAX_PASSWORD_LENGTH:
            # Checked here as well as in bootstrap-character, because a
            # credential file written by hand skips the bootstrap entirely.
            # Without this the game answers "Illegal password.", the login
            # loop retries the same value until it runs out of attempts, and
            # the operator gets a generic authentication fault describing
            # none of that. The length is reported; the value never is.
            raise TransportFault(
                f"the password is {len(lines[1])} characters and DikuMUD "
                f"rejects anything longer than {self.MAX_PASSWORD_LENGTH}")
        return lines[0], lines[1]

    # -- teardown -------------------------------------------------------

    async def disconnect(self) -> None:
        """Ask the game to end the session, then close the link."""
        if self._state in WRITABLE:
            try:
                await self.send_line("quit")
                await asyncio.sleep(0.5)
            except (TransportFault, OSError):
                pass
        await self.close()
        self._state = LinkState.CLOSED

    async def close(self) -> None:
        """Terminate TinTin++ and reap it. Must leave no orphan (PTY-01)."""
        self._detach_reader()
        if self._pid is not None:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.kill(self._pid, sig)
                except ProcessLookupError:
                    break
                for _ in range(20):
                    await asyncio.sleep(0.05)
                    try:
                        done, _ = os.waitpid(self._pid, os.WNOHANG)
                    except ChildProcessError:
                        done = self._pid
                    if done != 0:
                        break
                else:
                    continue
                break
            try:
                os.waitpid(self._pid, os.WNOHANG)
            except ChildProcessError:
                pass
            self._pid = None
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
        if self._state not in (LinkState.CLOSED, LinkState.FAULTED):
            self._state = LinkState.CLOSED

    async def reconnect(self) -> None:
        """Tear down and start again. State is never carried across."""
        await self.close()
        self._state = LinkState.IDLE
        self._buffer = OutputBuffer()
        self._events = []
        self._settle_tail = ""
        self._pending_echo = None
        self._data = asyncio.Event()
        await self.start()
