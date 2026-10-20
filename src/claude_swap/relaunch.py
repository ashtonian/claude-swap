"""Close running Claude Code sessions around an account switch and reopen
them resumed.

A switch changes the login every Claude Code session on this machine shares,
but a running session keeps the old account until its credential cache
expires (about 30 seconds behind the macOS Keychain), and a Remote Control
session stays registered under it. A restarting switch closes the sessions,
swaps the account, and reopens every session — in its own terminal where it
can be — with ``claude --resume <id>``, the command line it was started
with, and the Claude Code settings in its environment.

What is reopened (``plan_restart``): every interactive CLI session whose
command line can be read (macOS ``KERN_PROCARGS2``, Linux ``/proc``), by
the first of these that works:

1. In place: the resume command typed into the shell the session was
   started from, once that shell has the terminal back — a tmux pane, a
   Muxy terminal, an iTerm2 session or a Terminal.app tab, named by the
   session's own environment (``TMUX_PANE``, ``MUXY_SESSION_ID``,
   ``ITERM_SESSION_ID``, its tty). A session that was its tmux pane's own
   process gets its pane respawned.
2. A new window in the same app: its tmux session, its Muxy project, a new
   iTerm2 or Terminal.app window.
3. A new window in ``restart.fallback``, for terminals cswap cannot type
   into (an editor's terminal, Ghostty, Warp) and whatever 1 and 2 could
   not take.

Background sessions (``claude --bg``) are not closed: Claude Code's own
``claude respawn`` restarts them once the switch lands. Sessions inside an
app (an editor's extension, Claude Desktop, the SDK) and sessions signing
in with their own credentials keep running and are reported as skipped:
they pick the new account up from the credential store.

Order (``SessionRestarter.__call__``):

1. Each session is closed (SIGTERM, then SIGKILL) as soon as it is idle.
   Working ones — in a turn, running a background shell, or waiting on a
   question — get ``restart.busyTimeoutSeconds``; then the rest are closed
   mid-turn (``restart.onBusyTimeout`` close), or the switch is called off
   (abort) and the ones already closed reopen unswitched.
2. The switch runs.
3. Every closed session is reopened, whether or not the switch landed, and
   counts as reopened once a session record comes back under its id.

Reopened sessions skip Claude Code's offer to resume a long conversation
from a summary (``restart.resume`` full) and re-run a turn the close or a
rate limit cut off (``restart.continueInterrupted``). The plan is kept in
``<backup>/relaunch/plan.json`` so ``cswap reopen`` can finish the job if
this process dies between the close and the reopen.
"""

from __future__ import annotations

import contextlib
import functools
import json
import logging
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

from claude_swap.exceptions import RestartBlockedError, SwitchError
from claude_swap.fsutil import atomic_write_text
from claude_swap.locking import FileLock
from claude_swap.process_detection import (
    ClaudeSession,
    is_pid_alive,
    pid_matches_record,
    ps_columns,
    scan_sessions,
)
from claude_swap.settings import RestartSettings, atomic_write_json, load_restart_settings

_logger = logging.getLogger("claude-swap")

PLAN_SCHEMA_VERSION = 1
STATE_DIRNAME = "relaunch"
PLAN_FILENAME = "plan.json"
WORKER_LOG_FILENAME = "worker.log"
# Set on a restart re-run detached from a session it is about to close. The
# value is the pid of the command that started it, which the worker outlives.
WORKER_ENV = "CSWAP_RESTART_WORKER"

# Claude Code offers to resume a conversation from a summary once its last
# message is CLAUDE_CODE_RESUME_THRESHOLD_MINUTES old (and it is large); a
# threshold no conversation reaches resumes it whole without asking.
_RESUME_FULL = {"CLAUDE_CODE_RESUME_THRESHOLD_MINUTES": "2147483647"}
# Re-run, on resume, the turn a close or a rate limit cut off.
_CONTINUE_INTERRUPTED = {"CLAUDE_CODE_RESUME_INTERRUPTED_TURN": "1"}

# The Claude Code settings a reopened session keeps from the environment it
# was started with. Everything else comes from the shell it is typed into,
# the shell that started it.
_CARRIED_PREFIXES = (
    "ANTHROPIC_",
    "CLAUDE_",
    "ENABLE_",
    "DISABLE_",
    "MCP_",
    "MAX_THINKING_",
    "MAX_MCP_",
    "BASH_DEFAULT_",
    "BASH_MAX_",
)
_CARRIED_NAMES = frozenset(
    {
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
        "NODE_EXTRA_CA_CERTS",
    }
)
# Per-process values a claude hands its children (a claude started from
# another's shell inherits them), and the switches the restart sets itself.
_NOT_CARRIED = frozenset(
    {
        "CLAUDE_PID",
        "CLAUDE_EFFORT",
        "CLAUDE_CODE_ENTRYPOINT",
        "CLAUDE_CODE_EXECPATH",
        "CLAUDE_CODE_SSE_PORT",
        "CLAUDE_CODE_SESSION_ID",
        "CLAUDE_CODE_CHILD_SESSION",
        "CLAUDE_CODE_BRIDGE_SESSION_ID",
        "CLAUDE_CODE_SESSION_ATTENDED",
        "CLAUDE_CODE_MESSAGING_SOCKET",
        "CLAUDE_CODE_MESSAGING_TOKEN",
        *_RESUME_FULL,
        *_CONTINUE_INTERRUPTED,
    }
)
# A session authenticating with one of these never used the shared login: a
# switch does not reach it, and closing it would only interrupt it.
_OWN_AUTH = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
)

# Options that pick or start a conversation, or run it somewhere else. The
# reopened session is this conversation, here, by --resume.
_DROPPED_OPTIONS = frozenset(
    {
        "--resume",
        "--continue",
        "--session-id",
        "--fork-session",
        "--from-pr",
        "--teleport",
        "--cloud",
        "--environment",
        "--worktree",
        "--tmux",
        "--print",
        "--bg",
        "--desktop",
    }
)

# Record statuses with nothing in flight; None is a Claude Code that does
# not report one.
_IDLE_STATUSES = frozenset({"idle", None})

_SHELLS = frozenset(
    {"sh", "bash", "zsh", "fish", "dash", "ksh", "mksh", "tcsh", "csh", "nu", "xonsh", "elvish"}
)
# How far up from a session to look for the shell that started it, when its
# terminal does not bound the walk (tmux does: the pane's own process).
_MAX_SHELL_DEPTH = 8

# Local tool calls (tmux, muxy, osascript, ps). `claude --help` gets longer:
# the first run of a just-updated native binary is slow.
_TOOL_TIMEOUT_S = 10.0
_HELP_TIMEOUT_S = 30.0
# How long the shell gets to take its terminal back after the session in it
# exits, and a tmux pane to report its process gone.
_TERMINAL_RETURN_TIMEOUT_S = 10.0
# How long a SIGKILLed session gets to disappear.
_KILL_WAIT_S = 5.0
# How long a restart waits for another one to let go of the state dir.
_LOCK_WAIT_S = 10.0
_POLL_S = 0.5


# ---------------------------------------------------------------------------
# Processes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProcessImage:
    """A process's command line and environment as it was exec'd."""

    argv: tuple[str, ...]
    env: Mapping[str, str]


def read_process(pid: int) -> ProcessImage | None:
    """``pid``'s argv and environment, or None when unreadable: gone, owned
    by another user, or a platform without a reader."""
    if sys.platform == "darwin":
        raw = _darwin_procargs(pid)
        return None if raw is None else parse_procargs2(raw)
    if sys.platform.startswith("linux"):
        return _linux_process(pid)
    return None


@functools.cache
def _libc():
    import ctypes
    import ctypes.util

    return ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)


def _darwin_procargs(pid: int) -> bytes | None:
    import ctypes

    ctl_kern, kern_procargs2 = 1, 49
    mib = (ctypes.c_int * 3)(ctl_kern, kern_procargs2, pid)
    size = ctypes.c_size_t(0)
    libc = _libc()
    if libc.sysctl(mib, 3, None, ctypes.byref(size), None, 0) != 0:
        return None
    buf = ctypes.create_string_buffer(size.value)
    if libc.sysctl(mib, 3, buf, ctypes.byref(size), None, 0) != 0:
        return None
    return buf.raw[: size.value]


def parse_procargs2(raw: bytes) -> ProcessImage | None:
    """Decode a ``KERN_PROCARGS2`` buffer: ``argc`` (native int), the exec
    path, NUL padding, ``argc`` argument strings, then the environment up to
    the first empty string."""
    if len(raw) < 4:
        return None
    argc = int.from_bytes(raw[:4], sys.byteorder)
    rest = raw[4:]
    pos = rest.find(b"\0")
    if pos < 0:
        return None
    while pos < len(rest) and rest[pos] == 0:
        pos += 1
    strings = rest[pos:].split(b"\0")
    if argc < 1 or len(strings) < argc:
        return None
    argv = tuple(os.fsdecode(s) for s in strings[:argc])
    return ProcessImage(argv, _environment(strings[argc:]))


def _linux_process(pid: int) -> ProcessImage | None:
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
        environ = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return None
    if not cmdline:
        return None
    argv = tuple(os.fsdecode(s) for s in cmdline.rstrip(b"\0").split(b"\0"))
    return ProcessImage(argv, _environment(environ.split(b"\0")))


def _environment(entries: Sequence[bytes]) -> dict[str, str]:
    env: dict[str, str] = {}
    for entry in entries:
        if not entry:
            break
        key, sep, value = os.fsdecode(entry).partition("=")
        if sep and key:
            env.setdefault(key, value)
    return env


def process_table() -> dict[int, int]:
    """pid -> parent pid for every process, from one ``ps`` call; empty when
    ``ps`` fails."""
    try:
        proc = subprocess.run(
            ["ps", "-A", "-o", "pid=,ppid="],
            capture_output=True,
            text=True,
            timeout=_TOOL_TIMEOUT_S,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    table = {}
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            table[int(parts[0])] = int(parts[1])
    return table


def ancestors_of(pid: int, table: Mapping[int, int]) -> list[int]:
    """``pid``'s parent, grandparent, ... up to (not including) pid 1."""
    chain: list[int] = []
    seen = {pid}
    parent = table.get(pid)
    while parent is not None and parent > 1 and parent not in seen:
        chain.append(parent)
        seen.add(parent)
        parent = table.get(parent)
    return chain


def is_interactive_shell(image: ProcessImage) -> bool:
    """A shell reading commands from its terminal: a login shell (``-zsh``),
    or one started with neither a command (``-c``) nor a script."""
    if not image.argv:
        return False
    first = image.argv[0]
    if os.path.basename(first.lstrip("-")) not in _SHELLS:
        return False
    if first.startswith("-"):
        return True
    for arg in image.argv[1:]:
        if arg == "--command":  # fish
            return False
        if arg.startswith("--"):
            continue
        if arg.startswith(("-", "+")):
            if "c" in arg[1:]:
                return False
            continue
        return False  # a script: the shell runs it and exits
    return True


def owns_terminal(pid: int) -> bool | None:
    """Whether ``pid``'s process group is the foreground of its terminal;
    None when ``ps`` cannot tell."""
    text = ps_columns(pid, "pgid", "tpgid")
    if text is None:
        return None
    parts = text.split()
    if len(parts) != 2:
        return None
    return parts[0] == parts[1]


# ---------------------------------------------------------------------------
# The claude command line
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClaudeOptions:
    """One ``claude`` executable's option grammar, read from its ``--help``:
    how many arguments each spelling takes (``arity``), and the first long
    spelling each answers to (``canonical``)."""

    arity: Mapping[str, str]  # "flag" | "required" | "optional" | "variadic" | "optional-variadic"
    canonical: Mapping[str, str]

    _LINE = re.compile(r"^  (-[\w-]+(?:, -[\w-]+)*)(?: ([<\[][^>\]]*[>\]]))?(?:\s|$)")

    @classmethod
    def from_help(cls, text: str) -> ClaudeOptions:
        arity: dict[str, str] = {}
        canonical: dict[str, str] = {}
        for line in text.splitlines():
            match = cls._LINE.match(line)
            if match is None:
                continue
            names = match.group(1).split(", ")
            longs = [n for n in names if n.startswith("--")]
            primary = longs[0] if longs else names[0]
            kind = _arity(match.group(2))
            for name in names:
                arity[name] = kind
                canonical[name] = primary
        return cls(arity, canonical)


def _arity(spec: str | None) -> str:
    if spec is None:
        return "flag"
    variadic = spec.endswith("...>") or spec.endswith("...]")
    if spec.startswith("<"):
        return "variadic" if variadic else "required"
    return "optional-variadic" if variadic else "optional"


def read_claude_options(command: Sequence[str]) -> ClaudeOptions | None:
    """The grammar ``command --help`` prints, or None when it cannot be run
    or does not describe ``--resume``."""
    try:
        proc = subprocess.run(
            [*command, "--help"],
            capture_output=True,
            text=True,
            timeout=_HELP_TIMEOUT_S,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    options = ClaudeOptions.from_help(proc.stdout)
    return options if options.arity.get("--resume") else None


def _option_like(token: str) -> bool:
    # Commander's test for "this is an option, not a value".
    return len(token) > 1 and token[0] == "-"


def reopen_command(
    argv: Sequence[str], options: ClaudeOptions, prefix: int = 1
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """``argv`` rewritten to reopen the session, ready for ``--resume <id>``,
    and the positional arguments it left out.

    ``argv[:prefix]`` is the program (``claude``, or ``node cli.js``). Every
    option keeps its arguments, consumed the way Commander, claude's parser,
    consumes them; the options in ``_DROPPED_OPTIONS`` go, and so does every
    positional argument: the only one an interactive session takes is its
    first prompt, which must not be sent again. An option this claude's help
    does not list is kept as a bare flag, so its value, if it had one, is
    dropped rather than a prompt kept.
    """
    out = list(argv[:prefix])
    dropped: list[str] = []
    tokens = list(argv[prefix:])
    i = 0
    while i < len(tokens):
        token = tokens[i]
        i += 1
        if token == "--":
            dropped.extend(tokens[i:])
            break
        if not _option_like(token):
            dropped.append(token)
            continue
        group = [token]
        name, attached = token, None
        if token.startswith("--"):
            if "=" in token:
                name, attached = token.split("=", 1)
        elif len(token) > 2:
            # -rVALUE, or bundled flags -cd: Commander takes a short option's
            # value from the rest of the token, and splits bundled flags.
            name = token[:2]
            if options.arity.get(name) == "flag":
                tokens[i:i] = ["-" + token[2:]]
                group = [name]
            else:
                attached = token[2:]
        kind = options.arity.get(name, "flag")
        if attached is None and kind != "flag":
            if kind == "required" or kind == "variadic":
                if i < len(tokens):
                    group.append(tokens[i])
                    i += 1
            elif i < len(tokens) and not _option_like(tokens[i]):
                group.append(tokens[i])
                i += 1
            if kind in ("variadic", "optional-variadic"):
                while i < len(tokens) and not _option_like(tokens[i]) and tokens[i] != "--":
                    group.append(tokens[i])
                    i += 1
        if options.canonical.get(name, name) in _DROPPED_OPTIONS:
            continue
        out.extend(group)
    return tuple(out), tuple(dropped)


def carried_env(env: Mapping[str, str]) -> dict[str, str]:
    """The Claude Code settings in ``env`` a reopened session keeps."""
    return {
        key: value
        for key, value in env.items()
        if key not in _NOT_CARRIED
        and (key in _CARRIED_NAMES or key.startswith(_CARRIED_PREFIXES))
        and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key)
    }


def _own_auth(env: Mapping[str, str]) -> str | None:
    for key in _OWN_AUTH:
        value = env.get(key, "")
        if value and value.lower() not in ("0", "false", "no"):
            return key
    return None


def _program(image: ProcessImage) -> tuple[int, list[str]] | str:
    """How many argv entries name the program, and the command that runs
    that program here; or why it cannot be run. The native ``claude`` is
    one entry; an interpreter running a script (``node cli.js`` from npm)
    is two."""
    argv = image.argv
    head = argv[0]
    exe = head if os.sep in head else shutil.which(head, path=image.env.get("PATH"))
    if exe is None or not os.path.exists(exe):
        return f"`{head}` is no longer on its PATH"
    if _is_interpreter(head) and len(argv) > 1 and not _option_like(argv[1]):
        if not os.path.isfile(argv[1]):
            return f"{argv[1]} no longer exists"
        return 2, [exe, argv[1]]
    return 1, [exe]


def _is_interpreter(path: str) -> bool:
    # macOS framework builds re-exec as .../Python.app/Contents/MacOS/Python.
    name = os.path.basename(path).lower()
    return name in ("node", "bun") or name.startswith("python")


def launch_directory(pwd: str | None, cwd: str) -> str:
    """Where to start a reopened session: the directory it was started in,
    spelled the way its shell spelled it (``PWD``, through any symlink) when
    that is still the same directory, else its recorded one."""
    if pwd and os.path.isdir(pwd):
        try:
            if os.path.samefile(pwd, cwd):
                return pwd
        except OSError:
            pass
    return cwd


# ---------------------------------------------------------------------------
# Terminals
# ---------------------------------------------------------------------------


class TerminalError(Exception):
    """A terminal could not be inspected, typed into, or opened."""


class TerminalUnavailable(TerminalError):
    """The terminal app does not answer at all (a call timed out, or macOS
    has not allowed Automation of it): no session in it can be reached."""


@dataclass(frozen=True)
class Terminal:
    """A terminal cswap can drive, as a session's environment names it."""

    kind: str  # "tmux" | "muxy" | "iterm" | "terminal" (Terminal.app)
    target: str  # tmux pane id, Muxy session id, iTerm2 session unique id, Terminal.app tab tty
    socket: str | None = None  # tmux server socket
    tool: str | None = None  # tmux or muxy executable, found on the session's PATH
    profile: str | None = None  # MUXY_DIR
    # Where a new window for it opens: its tmux session, its Muxy project.
    parent: str | None = None

    def describe(self) -> str:
        noun = {
            "tmux": "tmux pane",
            "muxy": "Muxy terminal",
            "iterm": "iTerm2 session",
            "terminal": "Terminal.app tab",
        }
        return f"{noun[self.kind]} {self.target}"

    def to_json(self) -> dict:
        return {
            "kind": self.kind,
            "target": self.target,
            "socket": self.socket,
            "tool": self.tool,
            "profile": self.profile,
            "parent": self.parent,
        }

    @classmethod
    def from_json(cls, data: Mapping) -> Terminal:
        return cls(
            kind=data["kind"],
            target=data["target"],
            socket=data.get("socket"),
            tool=data.get("tool"),
            profile=data.get("profile"),
            parent=data.get("parent"),
        )


_TERM_PROGRAMS = {
    "Apple_Terminal": "Terminal.app",
    "iTerm.app": "iTerm2",
    "vscode": "an editor's terminal (VS Code, Cursor)",
    "WarpTerminal": "Warp",
    "ghostty": "Ghostty",
    "WezTerm": "WezTerm",
}


def terminal_name(env: Mapping[str, str]) -> str:
    """The terminal app a session runs in, for messages."""
    program = env.get("TERM_PROGRAM", "")
    return _TERM_PROGRAMS.get(program, program or "an unknown terminal")


def terminal_of(env: Mapping[str, str], tty: str | None = None) -> Terminal | None:
    """The terminal a session runs in, when cswap can type into it; None for
    any other, whose sessions reopen in ``restart.fallback``. tmux first: a
    pane inherits the variables of the terminal tmux runs in, and the pane
    is where the session is. ``tty`` is the session's terminal device, which
    is how a Terminal.app tab is found."""
    path = env.get("PATH")
    if env.get("TMUX") and env.get("TMUX_PANE"):
        tool = shutil.which("tmux", path=path) or shutil.which("tmux")
        if tool is not None:
            socket = env["TMUX"].split(",")[0]
            return Terminal("tmux", env["TMUX_PANE"], socket=socket, tool=tool)
    if env.get("MUXY_SESSION_ID"):
        tool = shutil.which("muxy", path=path) or shutil.which("muxy")
        if tool is not None:
            return Terminal(
                "muxy", env["MUXY_SESSION_ID"], tool=tool, profile=env.get("MUXY_DIR")
            )
    if sys.platform == "darwin":
        _, _, unique_id = env.get("ITERM_SESSION_ID", "").partition(":")
        if unique_id:
            return Terminal("iterm", unique_id)
        if env.get("TERM_PROGRAM") == "Apple_Terminal" and tty:
            return Terminal("terminal", tty)
    return None


def _session_tty(pid: int) -> str | None:
    tty = ps_columns(pid, "tty")
    if not tty or tty.startswith("?"):
        return None
    return tty if tty.startswith("/dev/") else f"/dev/{tty}"


def _run_tool(
    command: Sequence[str],
    *,
    env=None,
    stdin: str | None = None,
    timeout: float = _TOOL_TIMEOUT_S,
) -> str:
    try:
        proc = subprocess.run(
            list(command),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            input=stdin,
            stdin=None if stdin is not None else subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as e:
        # osascript waits on a pending Automation prompt until it times out.
        raise TerminalUnavailable(f"{os.path.basename(command[0])}: no answer in {timeout:.0f}s") from e
    except OSError as e:
        raise TerminalError(f"{os.path.basename(command[0])}: {e}") from e
    if proc.returncode != 0:
        detail = proc.stderr.strip() or f"exit {proc.returncode}"
        if "-1743" in detail:  # errAEEventNotPermitted: Automation denied
            raise TerminalUnavailable(f"macOS does not allow controlling it: {detail}")
        raise TerminalError(f"{os.path.basename(command[0])} {command[1]}: {detail}")
    return proc.stdout.strip()


def _app_running(executable: str) -> bool:
    """Whether a macOS app is running, by its executable's path suffix
    (``iTerm.app/Contents/MacOS/iTerm2``). Asked of the process table, not
    AppleScript: a ``tell`` to an app that is not running launches it, and
    ``application ... is running`` answers false from outside the login
    window session (a tmux server started over SSH) even while Apple Events
    still reach the app."""
    try:
        listing = _run_tool(["ps", "-A", "-o", "comm="])
    except TerminalError:
        return False
    return any(line.endswith("/" + executable) for line in listing.splitlines())


class _Tmux:
    """Types into the pane's shell, respawns a pane whose own process the
    session was, and opens windows in the pane's tmux session (or, as the
    fallback, in a tmux session named ``cswap``)."""

    FALLBACK_SESSION = "cswap"

    def _run(self, term: Terminal, *args: str) -> str:
        server = ["-S", term.socket] if term.socket else []
        return _run_tool([term.tool or "tmux", *server, *args])

    def _get(self, term: Terminal, fmt: str) -> str:
        return self._run(term, "display-message", "-p", "-t", term.target, fmt)

    def locate(
        self, term: Terminal, session_pid: int, lineage: Sequence[int]
    ) -> tuple[Terminal, int | None]:
        pane_pid, _, session_name = self._get(term, "#{pane_pid}\t#{session_name}").partition("\t")
        if not pane_pid.isdigit() or int(pane_pid) not in lineage:
            raise TerminalError(f"{term.describe()} does not run it")
        return replace(term, parent=session_name or None), int(pane_pid)

    def keep_pane(self, term: Terminal) -> None:
        self._run(term, "set-option", "-p", "-t", term.target, "remain-on-exit", "on")

    def pane_dead(self, term: Terminal) -> bool:
        return self._get(term, "#{pane_dead}") == "1"

    def respawn(self, term: Terminal, directory: str, script: Path) -> None:
        self._run(term, "respawn-pane", "-t", term.target, "-c", directory, _run_line(script))
        self._run(term, "set-option", "-p", "-u", "-t", term.target, "remain-on-exit")

    def type_line(self, term: Terminal, line: str) -> None:
        self._run(term, "send-keys", "-t", term.target, "-l", line)
        self._run(term, "send-keys", "-t", term.target, "Enter")

    def open_window(self, term: Terminal | None, directory: str, script: Path) -> str:
        if term is not None and term.parent:
            self._run(
                term, "new-window", "-d", "-t", f"={term.parent}:", "-c", directory,
                _run_line(script),
            )
            return f"a new window in tmux session {term.parent}"
        tool = shutil.which("tmux")
        if tool is None:
            raise TerminalError("tmux is not installed")
        server = Terminal("tmux", "", tool=tool)
        name = self.FALLBACK_SESSION
        try:
            self._run(server, "has-session", "-t", f"={name}")
        except TerminalError:
            self._run(server, "new-session", "-d", "-s", name, "-c", directory, _run_line(script))
        else:
            self._run(server, "new-window", "-d", "-t", f"={name}:", "-c", directory, _run_line(script))
        return f"a new window in tmux session {name} (tmux attach -t {name})"


class _Muxy:
    """Sends the line to the terminal's shell, and opens new terminals in
    the terminal's project."""

    def _run(self, term: Terminal, *args: str) -> str:
        env = {**os.environ, "MUXY_DIR": term.profile} if term.profile else None
        return _run_tool([term.tool or "muxy", *args], env=env)

    def locate(
        self, term: Terminal, session_pid: int, lineage: Sequence[int]
    ) -> tuple[Terminal, int | None]:
        try:
            listed = json.loads(self._run(term, "session", "list", "--json"))
        except json.JSONDecodeError as e:
            raise TerminalError(f"muxy session list: {e}") from e
        entry = next(
            (
                s
                for s in (listed if isinstance(listed, list) else [])
                if isinstance(s, dict) and str(s.get("id")) == term.target
            ),
            None,
        )
        if entry is None or entry.get("status") != "Live":
            raise TerminalError(f"{term.describe()} is not live")
        return replace(term, parent=entry.get("project_id") or None), None

    def type_line(self, term: Terminal, line: str) -> None:
        self._run(term, "session", "send", term.target, line)
        self._run(term, "session", "send-keys", term.target, "Enter")

    def open_window(self, term: Terminal | None, directory: str, script: Path) -> str:
        if term is None or not term.parent:
            raise TerminalError("a new Muxy terminal needs the project the session ran in")
        # Muxy types the command into the new terminal's shell and presses
        # Return.
        self._run(term, "session", "create", term.parent, "--directory", directory, "--", _run_line(script))
        return "a new Muxy terminal in its project (Existing Terminals)"


class _ITerm:
    """Writes into the session found by unique id, and opens windows."""

    BUNDLE = "com.googlecode.iterm2"
    EXECUTABLE = "iTerm.app/Contents/MacOS/iTerm2"
    # The session's tty; writes item 2 into it when given.
    _FIND = f"""
on run argv
  tell application id "{BUNDLE}"
    repeat with w in windows
      repeat with t in tabs of w
        repeat with s in sessions of t
          if unique id of s is (item 1 of argv) then
            if (count of argv) > 1 then tell s to write text (item 2 of argv)
            return tty of s
          end if
        end repeat
      end repeat
    end repeat
  end tell
  return ""
end run
"""
    _NEW_WINDOW = f"""
on run argv
  tell application id "{BUNDLE}"
    create window with default profile command (item 1 of argv)
  end tell
end run
"""

    def _find(self, *args: str) -> str:
        if not _app_running(self.EXECUTABLE):
            return ""
        return _run_tool(["osascript", "-", *args], stdin=self._FIND)

    def locate(
        self, term: Terminal, session_pid: int, lineage: Sequence[int]
    ) -> tuple[Terminal, int | None]:
        tty = self._find(term.target)
        if not tty:
            raise TerminalError(f"{term.describe()} is not open")
        if tty != _session_tty(session_pid):
            raise TerminalError(f"{term.describe()} does not run it")
        return term, None

    def type_line(self, term: Terminal, line: str) -> None:
        if not self._find(term.target, line):
            raise TerminalError(f"{term.describe()} is not open")

    def open_window(self, term: Terminal | None, directory: str, script: Path) -> str:
        _run_tool(["osascript", "-", _run_line(script)], stdin=self._NEW_WINDOW)
        return "a new iTerm2 window"


class _TerminalApp:
    """Types into the Terminal.app tab found by tty (``do script ... in``),
    and opens windows by handing it the launcher (``open -a Terminal``),
    which needs no Automation permission."""

    BUNDLE = "com.apple.Terminal"
    EXECUTABLE = "Terminal.app/Contents/MacOS/Terminal"
    _FIND = f"""
on run argv
  tell application id "{BUNDLE}"
    repeat with w in windows
      repeat with t in tabs of w
        if tty of t is (item 1 of argv) then
          if (count of argv) > 1 then do script (item 2 of argv) in t
          return tty of t
        end if
      end repeat
    end repeat
  end tell
  return ""
end run
"""

    def _find(self, *args: str) -> str:
        if not _app_running(self.EXECUTABLE):
            return ""
        return _run_tool(["osascript", "-", *args], stdin=self._FIND)

    def locate(
        self, term: Terminal, session_pid: int, lineage: Sequence[int]
    ) -> tuple[Terminal, int | None]:
        if self._find(term.target) != term.target:
            raise TerminalError(f"{term.describe()} is not open")
        return term, None

    def type_line(self, term: Terminal, line: str) -> None:
        if not self._find(term.target, line):
            raise TerminalError(f"{term.describe()} is not open")

    def open_window(self, term: Terminal | None, directory: str, script: Path) -> str:
        _run_tool(["open", "-b", self.BUNDLE, str(script)])
        return "a new Terminal.app window"


_BACKENDS = {
    "tmux": _Tmux(),
    "muxy": _Muxy(),
    "iterm": _ITerm(),
    "terminal": _TerminalApp(),
}
# restart.fallback values that name a backend; "auto" picks per platform.
_FALLBACK_PLATFORMS = {"terminal": "darwin", "iterm": "darwin", "tmux": None}


def fallback_kind(setting: str) -> str | None:
    """The backend ``restart.fallback`` opens windows in on this platform,
    or None (``none``, or an app this platform does not have)."""
    if setting == "auto":
        setting = "terminal" if sys.platform == "darwin" else "tmux"
    platform = _FALLBACK_PLATFORMS.get(setting, "")
    if platform == "" or (platform is not None and sys.platform != platform):
        return None
    return setting


def _run_line(script: Path) -> str:
    return f"sh {shlex.quote(str(script))}"


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlannedSession:
    """A running session the restart closes and reopens."""

    pid: int
    proc_start: str | None
    session_id: str
    name: str | None
    status: str | None
    # "interactive": closed before the switch and reopened in a terminal.
    # "background": restarted after it by ``claude respawn <job_id>``.
    kind: str
    # Where it runs, for messages: a Terminal's description, or the app.
    where: str
    # The terminal it is reopened in, when cswap can drive it; None reopens
    # it in restart.fallback.
    terminal: Terminal | None
    # The interactive shell it was started from, typed into to reopen it in
    # place. None: a new window, or for a tmux pane whose own process it
    # was, the pane respawned.
    shell_pid: int | None
    directory: str
    # Interactive: its command line rewritten by reopen_command, the program
    # resolved to a path. Background: the claude that runs `respawn`.
    command: tuple[str, ...]
    env: Mapping[str, str]  # carried Claude Code settings
    dropped: tuple[str, ...] = ()  # positional arguments left out (a first prompt)
    job_id: str | None = None

    @property
    def argv(self) -> tuple[str, ...]:
        """The command that reopens it: its own, resuming its conversation."""
        return (*self.command, "--resume", self.session_id)

    @property
    def key(self) -> tuple[int, str | None]:
        """The process it was planned from. Not the session id, which /clear
        moves on."""
        return (self.pid, self.proc_start)

    @property
    def label(self) -> str:
        return f"{self.name or 'session ' + self.session_id[:8]} (pid {self.pid})"

    def reopens_in(self, fallback: str | None) -> str:
        """Where it comes back, for ``cswap sessions``."""
        if self.kind == "background":
            return "claude respawn"
        if self.terminal is None:
            return f"new {fallback} window" if fallback else "nowhere"
        if self.shell_pid is None:
            return "its pane, respawned" if self.terminal.kind == "tmux" else "a new window"
        return "in place"

    def to_json(self) -> dict:
        return {
            "pid": self.pid,
            "procStart": self.proc_start,
            "sessionId": self.session_id,
            "name": self.name,
            "status": self.status,
            "kind": self.kind,
            "where": self.where,
            "terminal": self.terminal.to_json() if self.terminal else None,
            "shellPid": self.shell_pid,
            "directory": self.directory,
            "command": list(self.command),
            "env": dict(self.env),
            "dropped": list(self.dropped),
            "jobId": self.job_id,
        }

    @classmethod
    def from_json(cls, data: Mapping) -> PlannedSession:
        terminal = data.get("terminal")
        return cls(
            pid=int(data["pid"]),
            proc_start=data.get("procStart"),
            session_id=str(data["sessionId"]),
            name=data.get("name"),
            status=data.get("status"),
            kind=str(data.get("kind") or "interactive"),
            where=str(data.get("where") or ""),
            terminal=Terminal.from_json(terminal) if terminal else None,
            shell_pid=data.get("shellPid"),
            directory=str(data["directory"]),
            command=tuple(data["command"]),
            env=dict(data.get("env") or {}),
            dropped=tuple(data.get("dropped") or ()),
            job_id=data.get("jobId"),
        )


@dataclass(frozen=True)
class SkippedSession:
    """A running session the restart leaves alone, and why."""

    pid: int
    session_id: str
    name: str | None
    reason: str

    @property
    def label(self) -> str:
        return f"{self.name or 'session ' + self.session_id[:8]} (pid {self.pid})"

    def to_json(self) -> dict:
        return {
            "pid": self.pid,
            "sessionId": self.session_id,
            "name": self.name,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class RestartPlan:
    sessions: tuple[PlannedSession, ...]
    skipped: tuple[SkippedSession, ...]
    # Sessions this process runs inside: closing one ends this process, so
    # they are left running (a command run from one detaches first).
    hosting: tuple[PlannedSession, ...]
    # Session records that could not be read.
    unreadable: int

    def to_json(self) -> dict:
        return {
            "sessions": [s.to_json() for s in self.sessions],
            "hosting": [s.to_json() for s in self.hosting],
            "skipped": [s.to_json() for s in self.skipped],
            "unreadable": self.unreadable,
        }


def plan_restart(claude_dir: Path | None = None, *, fallback: str = "auto") -> RestartPlan:
    """What a restart would close and reopen among the running sessions of
    ``claude_dir`` (default: the active Claude config home). ``fallback``
    is ``restart.fallback``: with ``none``, sessions in a terminal cswap
    cannot type into are left running."""
    sessions, unreadable = scan_sessions(claude_dir)
    if sys.platform != "darwin" and not sys.platform.startswith("linux"):
        reason = "restarting sessions is supported on macOS and Linux only"
        return RestartPlan((), tuple(_skipped(s, reason) for s in sessions), (), unreadable)
    table = process_table()
    own_lineage = {os.getpid(), *ancestors_of(os.getpid(), table)}
    grammars: dict[tuple[str, ...], ClaudeOptions | None] = {}
    # Terminal kinds that stopped answering this plan; the rest of their
    # sessions go straight to the fallback instead of each timing out.
    unavailable: set[str] = set()
    can_fall_back = fallback_kind(fallback) is not None
    planned: list[PlannedSession] = []
    hosting: list[PlannedSession] = []
    skipped: list[SkippedSession] = []
    for session in sorted(sessions, key=lambda s: s.started_at):
        outcome = _plan_session(session, table, grammars, unavailable, can_fall_back)
        if isinstance(outcome, str):
            skipped.append(_skipped(session, outcome))
        elif session.pid in own_lineage:
            hosting.append(outcome)
        else:
            planned.append(outcome)
    return RestartPlan(tuple(planned), tuple(skipped), tuple(hosting), unreadable)


def _skipped(session: ClaudeSession, reason: str) -> SkippedSession:
    return SkippedSession(session.pid, session.session_id, session.name, reason)


def _plan_session(
    session: ClaudeSession,
    table: Mapping[int, int],
    grammars: dict[tuple[str, ...], ClaudeOptions | None],
    unavailable: set[str],
    can_fall_back: bool,
) -> PlannedSession | str:
    if session.entrypoint != "cli":
        return (
            f"it runs inside an app ({session.entrypoint or 'unknown entrypoint'}), "
            "which has to restart it"
        )
    if session.kind not in ("interactive", "bg"):
        return f"Claude Code's own {session.kind or 'unknown'} process"
    if not session.session_id:
        return "its record names no session id"
    image = read_process(session.pid)
    if image is None or not image.argv:
        return "its command line could not be read"
    own = _own_auth(image.env)
    if own is not None:
        return f"it signs in with {own}, not the shared login a switch changes"
    if session.kind == "bg":
        return _plan_background(session, image)
    program = _program(image)
    if isinstance(program, str):
        return program
    prefix, executable = program

    key = tuple(executable)
    if key not in grammars:
        grammars[key] = read_claude_options(executable)
    options = grammars[key]
    if options is None:
        return f"`{' '.join(executable)} --help` could not be read"
    command, dropped = reopen_command(image.argv, options, prefix)
    # The resolved program, so a new window's shell need not find it on
    # whatever PATH it starts with.
    command = (*executable, *command[prefix:])

    directory = launch_directory(image.env.get("PWD"), session.cwd)
    if not os.path.isdir(directory):
        return f"its directory {directory} no longer exists"

    terminal = terminal_of(image.env, _session_tty(session.pid))
    lineage = [session.pid, *ancestors_of(session.pid, table)]
    root = None
    if terminal is not None and terminal.kind in unavailable:
        terminal = None
    if terminal is not None:
        try:
            terminal, root = _BACKENDS[terminal.kind].locate(terminal, session.pid, lineage)
        except TerminalError as e:
            if isinstance(e, TerminalUnavailable):
                unavailable.add(terminal.kind)
            _logger.info("%s: %s; it reopens in restart.fallback", session.pid, e)
            terminal = None
    if terminal is None and not can_fall_back:
        return (
            f"it runs in {terminal_name(image.env)}, which cswap cannot type into, "
            "and restart.fallback is none"
        )
    return PlannedSession(
        pid=session.pid,
        proc_start=session.proc_start,
        session_id=session.session_id,
        name=session.name,
        status=session.status,
        kind="interactive",
        where=terminal.describe() if terminal else terminal_name(image.env),
        terminal=terminal,
        shell_pid=_launching_shell(lineage, root) if terminal else None,
        directory=directory,
        command=command,
        env=carried_env(image.env),
        dropped=dropped,
    )


def _plan_background(session: ClaudeSession, image: ProcessImage) -> PlannedSession | str:
    """A background session restarts by ``claude respawn <job id>``; its own
    command line names nothing to run (the worker retitles itself)."""
    if not session.job_id:
        return "a background session without a job id to respawn"
    claude = shutil.which("claude", path=image.env.get("PATH")) or shutil.which("claude")
    if claude is None:
        return "`claude` is not on its PATH to run `claude respawn`"
    return PlannedSession(
        pid=session.pid,
        proc_start=session.proc_start,
        session_id=session.session_id,
        name=session.name,
        status=session.status,
        kind="background",
        where=f"background job {session.job_id}",
        terminal=None,
        shell_pid=None,
        directory=session.cwd,
        command=(claude,),
        env={},
        job_id=session.job_id,
    )


def _launching_shell(lineage: Sequence[int], root: int | None) -> int | None:
    """The nearest interactive shell above the session (``lineage[0]``), not
    past the terminal's own process ``root`` when it has one."""
    ancestors = lineage[1:]
    if root is not None and root in ancestors:
        ancestors = ancestors[: ancestors.index(root) + 1]
    else:
        ancestors = ancestors[:_MAX_SHELL_DEPTH]
    for pid in ancestors:
        image = read_process(pid)
        if image is not None and is_interactive_shell(image):
            return pid
    return None


# ---------------------------------------------------------------------------
# Restart
# ---------------------------------------------------------------------------


@dataclass
class RestartReport:
    reopened: list[PlannedSession] = field(default_factory=list)
    failed: list[tuple[PlannedSession, str]] = field(default_factory=list)
    # Closed while working; their turn is re-run on resume when
    # continueInterrupted is on.
    interrupted: list[PlannedSession] = field(default_factory=list)
    # Exited on their own while the restart waited; not reopened.
    exited: list[PlannedSession] = field(default_factory=list)
    hosting: list[PlannedSession] = field(default_factory=list)
    skipped: list[SkippedSession] = field(default_factory=list)
    # Where each reopened session came back, by PlannedSession.key.
    reopened_in: dict[tuple[int, str | None], str] = field(default_factory=dict)

    def to_json(self) -> dict:
        """The shape ``restart_summary`` and the switch results carry."""

        def ref(s: PlannedSession) -> dict:
            return {"pid": s.pid, "sessionId": s.session_id, "name": s.name}

        return {
            "reopened": [{**ref(s), "in": self.reopened_in.get(s.key)} for s in self.reopened],
            "failed": [{**ref(s), "reason": reason} for s, reason in self.failed],
            "interrupted": [ref(s) for s in self.interrupted],
            "exited": [ref(s) for s in self.exited],
            "leftRunning": [
                {**ref(s), "reason": "runs this command"} for s in self.hosting
            ]
            + [s.to_json() for s in self.skipped],
        }

    def warnings(self) -> list[str]:
        """One line per session the restart could not bring back."""
        return [
            f"{s.label} was closed but not reopened ({reason}); reopen it with "
            f"`cswap reopen` or `claude --resume {s.session_id}`"
            for s, reason in self.failed
        ]


class SessionRestarter:
    """Wraps an account switch: closes the running sessions, runs the switch,
    reopens them. Installed on a switcher as ``around_switch``; see the
    module docstring for the order and the rules."""

    def __init__(
        self,
        state_dir: Path,
        settings: RestartSettings,
        *,
        claude_dir: Path | None = None,
        close_busy: bool = False,
        progress: Callable[[str], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        terminals: Mapping[str, object] | None = None,
        foreground: Callable[[int], bool | None] = owns_terminal,
        kill: Callable[[int, int], None] = os.kill,
    ):
        self.state_dir = state_dir
        self.settings = settings
        self.claude_dir = claude_dir
        # Close working sessions at once instead of waiting for them.
        self.close_busy = close_busy
        self.progress = progress or _logger.info
        self.clock = clock
        self.sleep = sleep
        # Terminal backends by Terminal.kind; whether a shell owns its
        # terminal; how a session is signalled.
        self.terminals = terminals if terminals is not None else _BACKENDS
        self.foreground = foreground
        self.kill = kill
        # The last restart's outcome, for callers that switch in human mode
        # (whose switch returns nothing) and still report it.
        self.last_report: RestartReport | None = None

    @property
    def plan_path(self) -> Path:
        return self.state_dir / PLAN_FILENAME

    def plan(self) -> RestartPlan:
        return plan_restart(self.claude_dir, fallback=self.settings.fallback)

    def __call__(self, switch: Callable[[], dict]) -> dict:
        with self._locked():
            return self._restart(switch)

    @contextlib.contextmanager
    def _locked(self):
        """One restart (or ``cswap reopen``) at a time."""
        lock = FileLock(self.state_dir / ".lock", timeout=_LOCK_WAIT_S)
        if not lock.acquire():
            raise SwitchError(
                "Another switch is restarting sessions; try again when it finishes"
            )
        try:
            yield
        finally:
            lock.release()

    def _restart(self, switch: Callable[[], dict]) -> dict:
        plan = self.plan()
        report = RestartReport(hosting=list(plan.hosting), skipped=list(plan.skipped))
        self.last_report = report
        for s in plan.skipped:
            self.progress(f"Leaving {s.label} running: {s.reason}")
        for s in plan.hosting:
            self.progress(f"Leaving {s.label} running: this command runs inside it")
        if plan.unreadable:
            self.progress(f"{plan.unreadable} session record(s) could not be read")

        interactive = [s for s in plan.sessions if s.kind == "interactive"]
        background = [s for s in plan.sessions if s.kind == "background"]
        self._save(plan.sessions)
        closed = self._close_all(interactive, report)
        switched = False
        try:
            result = switch()
            switched = True
        finally:
            # Background sessions only restart onto a switch that landed:
            # respawning them onto the same account would interrupt them
            # for nothing.
            self._reopen_all([*closed, *(background if switched else ())], report)
        result = dict(result)
        result["restart"] = report.to_json()
        result["warnings"] = [*result.get("warnings", []), *report.warnings()]
        return result

    # -- close ----------------------------------------------------------------

    def _close_all(
        self, sessions: Sequence[PlannedSession], report: RestartReport
    ) -> list[PlannedSession]:
        """Close each session once it is idle; when the wait runs out, close
        the rest or call the switch off. Returns the closed ones."""
        pending = {s.key: s for s in sessions}
        closed: list[PlannedSession] = []
        wait = 0.0 if self.close_busy else self.settings.busy_timeout_seconds
        deadline = self.clock() + wait
        announced = False
        while pending:
            live = {r.pid: r for r in scan_sessions(self.claude_dir)[0]}
            for key, session in list(pending.items()):
                record = live.get(session.pid)
                if record is None:
                    del pending[key]
                    report.exited.append(session)
                    self.progress(f"{session.label} exited on its own; not reopening it")
                    continue
                if record.session_id and record.session_id != session.session_id:
                    # /clear (or /resume) inside it moved it to another
                    # conversation: reopen that one.
                    session = pending[key] = replace(session, session_id=record.session_id)
                if record.status in _IDLE_STATUSES:
                    del pending[key]
                    self._close(session, closed, report)
            if not pending:
                break
            if self.clock() >= deadline:
                if self.settings.on_busy_timeout == "abort" and not self.close_busy:
                    still = list(pending.values())
                    self._reopen_all(closed, report)
                    raise RestartBlockedError(
                        f"{len(still)} session(s) still working after "
                        f"{wait:.0f}s, so the switch was called off: "
                        + ", ".join(s.label for s in still)
                        + ". Wait for them, or switch with --close-busy",
                        busy=still,
                    )
                for session in pending.values():
                    self.progress(f"Closing {session.label} mid-turn")
                    if self._close(session, closed, report):
                        report.interrupted.append(session)
                break
            if not announced:
                announced = True
                self.progress(
                    f"Waiting up to {wait:.0f}s for {len(pending)} working "
                    "session(s) to finish their turn"
                )
            self.sleep(1.0)
        return closed

    def _close(
        self, session: PlannedSession, closed: list[PlannedSession], report: RestartReport
    ) -> bool:
        """SIGTERM, then SIGKILL after ``close_timeout_seconds``. True once
        the session is gone; a session that cannot be closed is left
        running and reported."""
        if not pid_matches_record(session.pid, session.proc_start):
            report.exited.append(session)
            return False
        try:
            if session.terminal is not None and session.terminal.kind == "tmux" and session.shell_pid is None:
                # Its pane's own process: keep the pane when it exits, to
                # respawn it.
                self.terminals["tmux"].keep_pane(session.terminal)
            self.kill(session.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except (TerminalError, PermissionError) as e:
            self._leave_running(session, str(e), report)
            return False
        if not self._wait_gone(session.pid, self.settings.close_timeout_seconds):
            try:
                self.kill(session.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            if not self._wait_gone(session.pid, _KILL_WAIT_S):
                self._leave_running(session, "it did not exit after SIGKILL", report)
                return False
        closed.append(session)
        self._save_state(session, "closed")
        self.progress(f"Closed {session.label}")
        return True

    def _leave_running(self, session: PlannedSession, reason: str, report: RestartReport) -> None:
        report.skipped.append(
            SkippedSession(session.pid, session.session_id, session.name, reason)
        )
        self.progress(f"Leaving {session.label} running: {reason}")

    def _wait_gone(self, pid: int, timeout: float) -> bool:
        deadline = self.clock() + timeout
        while is_pid_alive(pid):
            if self.clock() >= deadline:
                return False
            self.sleep(_POLL_S)
        return True

    # -- reopen ---------------------------------------------------------------

    def _reopen_all(
        self, sessions: Sequence[PlannedSession], report: RestartReport
    ) -> None:
        launched: list[PlannedSession] = []
        for session in sessions:
            try:
                where = self._launch(session)
            except TerminalError as e:
                report.failed.append((session, str(e)))
                self._save_state(session, "failed")
                self.progress(f"Could not reopen {session.label}: {e}")
            else:
                report.reopened_in[session.key] = where
                launched.append(session)
        self._await_reopened(launched, report)

    def _launch(self, session: PlannedSession) -> str:
        """Start ``session`` again; returns where. In place first — typed
        into the shell it was started from, or its tmux pane respawned —
        then a new window in the same app, then one in restart.fallback.
        Each later step runs only once the session is known not to be back,
        so a terminal that restarted it on its own is never given a second
        copy."""
        if session.kind == "background":
            _run_tool([*session.command, "respawn", session.job_id or ""], timeout=_HELP_TIMEOUT_S)
            return "its background job (claude respawn)"
        try:
            script = self._write_launcher(session)
        except OSError as e:
            raise TerminalError(f"could not write its launcher: {e}") from e
        attempts: list[str] = []
        term = session.terminal
        if term is not None:
            backend = self.terminals[term.kind]
            try:
                return self._in_place(session, backend, script)
            except TerminalError as e:
                attempts.append(str(e))
            if self._running_again(session):
                return "its terminal, on its own"
            try:
                return backend.open_window(term, session.directory, script)
            except TerminalError as e:
                attempts.append(str(e))
        kind = fallback_kind(self.settings.fallback)
        if kind is not None:
            if self._running_again(session):
                return "its terminal, on its own"
            try:
                return self.terminals[kind].open_window(None, session.directory, script)
            except TerminalError as e:
                attempts.append(str(e))
        else:
            attempts.append("restart.fallback is none")
        script.unlink(missing_ok=True)
        raise TerminalError("; ".join(attempts))

    def _in_place(self, session: PlannedSession, backend, script: Path) -> str:
        term = session.terminal
        if session.shell_pid is None:
            if term.kind != "tmux":
                raise TerminalError(f"{term.describe()} had no shell to return to")
            if not self._wait_for(lambda: backend.pane_dead(term)):
                raise TerminalError(f"{term.describe()} kept running")
            backend.respawn(term, session.directory, script)
            return f"{term.describe()}, respawned"
        if not self._wait_for(lambda: self.foreground(session.shell_pid) is True):
            raise TerminalError(f"{term.describe()} did not return to its shell")
        # The leading space keeps the line out of shell history where the
        # shell ignores space-led lines (HIST_IGNORE_SPACE,
        # HISTCONTROL=ignorespace); the script removes itself.
        backend.type_line(term, " " + _run_line(script))
        return term.describe()

    def _running_again(self, session: PlannedSession) -> bool:
        return any(
            r.session_id == session.session_id and r.pid != session.pid
            for r in scan_sessions(self.claude_dir)[0]
        )

    def _wait_for(self, ready: Callable[[], bool]) -> bool:
        deadline = self.clock() + _TERMINAL_RETURN_TIMEOUT_S
        while True:
            try:
                if ready():
                    return True
            except TerminalError:
                pass
            if self.clock() >= deadline:
                return False
            self.sleep(_POLL_S)

    def _write_launcher(self, session: PlannedSession) -> Path:
        env = dict(session.env)
        if self.settings.resume == "full":
            env.update(_RESUME_FULL)
        if self.settings.continue_interrupted:
            env.update(_CONTINUE_INTERRUPTED)
        # .command and executable: Terminal.app runs it when handed it.
        path = self.state_dir / f"{session.session_id}.command"
        atomic_write_text(path, launcher_script(session, env), mode=0o700)
        return path

    def _await_reopened(
        self, sessions: Sequence[PlannedSession], report: RestartReport
    ) -> None:
        """A session counts as reopened once a live record carries its id
        under a new pid."""
        waiting = list(sessions)
        old_pids = {s.pid for s in sessions}
        deadline = self.clock() + self.settings.reopen_timeout_seconds
        while waiting:
            for record in scan_sessions(self.claude_dir)[0]:
                if record.pid in old_pids:
                    continue
                session = next((s for s in waiting if s.session_id == record.session_id), None)
                if session is not None:
                    waiting.remove(session)
                    old_pids.add(record.pid)
                    report.reopened.append(session)
                    self._save_state(session, "reopened")
                    where = report.reopened_in.get(session.key)
                    self.progress(f"Reopened {session.label} as pid {record.pid} in {where}")
            if not waiting or self.clock() >= deadline:
                break
            self.sleep(_POLL_S)
        for session in waiting:
            report.reopened_in.pop(session.key, None)
            reason = (
                f"no session came back within {self.settings.reopen_timeout_seconds:.0f}s; "
                f"see {session.where}"
            )
            report.failed.append((session, reason))
            self._save_state(session, "failed")
            self.progress(f"Could not confirm {session.label} reopened: {reason}")

    # -- plan file ------------------------------------------------------------

    def _save(self, sessions: Sequence[PlannedSession]) -> None:
        """Write this restart's plan before anything is closed. Sessions an
        earlier restart closed and never brought back stay listed for
        ``cswap reopen`` until they are running again."""
        live = {r.session_id for r in scan_sessions(self.claude_dir)[0]}
        planned = {s.session_id for s in sessions}
        unfinished = [
            entry
            for entry in read_plan_file(self.plan_path).get("sessions", [])
            if isinstance(entry, dict)
            and entry.get("state") in ("closed", "failed")
            and entry.get("sessionId") not in live | planned
        ]
        atomic_write_json(
            self.plan_path,
            {
                "schemaVersion": PLAN_SCHEMA_VERSION,
                "createdAt": datetime.now(timezone.utc).isoformat(),
                "sessions": [*unfinished, *({**s.to_json(), "state": "open"} for s in sessions)],
            },
        )

    def _save_state(self, session: PlannedSession, state: str) -> None:
        """Record how far ``session`` got. Best effort: the plan file is a
        recovery aid, and failing to update it must not stop the restart."""
        try:
            data = read_plan_file(self.plan_path)
            for i, entry in enumerate(data.get("sessions", [])):
                if (entry.get("pid"), entry.get("procStart")) == session.key:
                    data["sessions"][i] = {**session.to_json(), "state": state}
            atomic_write_json(self.plan_path, data)
        except OSError as e:
            _logger.warning("Could not record %s as %s in %s: %s", session.label, state, self.plan_path, e)

    # -- recovery -------------------------------------------------------------

    def reopen_unfinished(self) -> RestartReport:
        """Reopen the sessions restarts closed and never confirmed reopened
        (``cswap reopen``), unless they are running again."""
        with self._locked():
            return self._reopen_unfinished()

    def _reopen_unfinished(self) -> RestartReport:
        report = RestartReport()
        data = read_plan_file(self.plan_path)
        live = {r.session_id for r in scan_sessions(self.claude_dir)[0]}
        pending = []
        for entry in data.get("sessions", []):
            if entry.get("state") not in ("closed", "failed"):
                continue
            try:
                session = PlannedSession.from_json(entry)
            except (KeyError, TypeError, ValueError):
                continue
            # A background session's old process outlives a failed respawn,
            # so only an interactive one counts as back by being live.
            if session.kind == "interactive" and session.session_id in live:
                self._save_state(session, "reopened")
                continue
            pending.append(session)
        self._reopen_all(pending, report)
        return report


def restart_summary(restart: Mapping) -> str:
    """"reopened 3 of 4 session(s)" for a ``RestartReport.to_json()``."""
    reopened = len(restart.get("reopened", []))
    total = reopened + len(restart.get("failed", []))
    line = f"reopened {reopened} of {total} session(s)"
    if restart.get("failed"):
        line += "; `cswap reopen` retries the rest"
    return line


def restarter_for(
    backup_dir: Path,
    *,
    enabled: bool | None = None,
    close_busy: bool = False,
    progress: Callable[[str], None] | None = None,
) -> SessionRestarter | None:
    """The restarter the ``restart`` settings ask for, or None when switches
    leave sessions alone. ``enabled`` overrides ``restart.onSwitch``."""
    settings = load_restart_settings(backup_dir)
    if not (settings.on_switch if enabled is None else enabled):
        return None
    return SessionRestarter(
        backup_dir / STATE_DIRNAME,
        settings,
        close_busy=close_busy,
        progress=progress,
    )


def read_plan_file(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def launcher_script(session: PlannedSession, env: Mapping[str, str]) -> str:
    """The script a reopened session's terminal runs: it removes itself,
    enters the session's directory, and execs the resume command with the
    carried settings exported, so the claude replaces the ``sh`` and its
    parent is the shell the line was typed into."""
    lines = [
        "#!/bin/sh",
        f"# cswap: reopens Claude Code session {session.session_id} after an account switch.",
        'rm -f -- "$0"',
        f"cd -- {shlex.quote(session.directory)} || exit 1",
    ]
    lines += [f"{key}={shlex.quote(value)}; export {key}" for key, value in sorted(env.items())]
    lines.append("exec " + " ".join(shlex.quote(arg) for arg in session.argv))
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Detached worker
# ---------------------------------------------------------------------------


def spawn_worker(argv: Sequence[str], log_path: Path) -> int:
    """Re-run this command detached (own session, no terminal, output to
    ``log_path``), marked with ``WORKER_ENV``; returns its pid."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "claude_swap", *argv],
            stdin=subprocess.DEVNULL,
            stdout=fd,
            stderr=fd,
            start_new_session=True,
            env={**os.environ, WORKER_ENV: str(os.getpid()), "NO_COLOR": "1"},
        )
    finally:
        os.close(fd)
    return proc.pid


def wait_for_launcher(timeout: float) -> None:
    """In a worker: wait for the command that spawned it to exit, so the
    session it ran in is no longer this process's ancestor and can close."""
    value = os.environ.get(WORKER_ENV, "")
    if not value.isdigit():
        return
    deadline = time.monotonic() + timeout
    while is_pid_alive(int(value)) and time.monotonic() < deadline:
        time.sleep(_POLL_S)


def notify(title: str, message: str) -> None:
    """A macOS notification; nothing elsewhere or on failure."""
    if sys.platform != "darwin":
        return
    script = (
        "on run argv\n"
        "  display notification (item 2 of argv) with title (item 1 of argv)\n"
        "end run\n"
    )
    try:
        subprocess.run(
            ["osascript", "-", title, message],
            input=script,
            capture_output=True,
            text=True,
            timeout=_TOOL_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass
