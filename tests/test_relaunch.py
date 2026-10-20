"""Session restart around a switch (relaunch.py).

The pure parts — the procargs decoder, the option grammar, the resume
command line, the carried environment, shell and terminal detection — are
table tests. The restart itself runs end to end against real processes: a
stand-in ``claude`` (a Python script answering ``--help`` like the real one,
writing a session record like the real one, and logging the argv, env and
directory it got) started from an interactive ``sh``, closed by the
restarter, and reopened through a terminal backend that runs the typed line
in ``sh``. Only the terminal is faked: tmux, Muxy and iTerm2 are the
external dependency.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from claude_swap import relaunch
from claude_swap.exceptions import RestartBlockedError, SwitchError
from claude_swap.relaunch import (
    ClaudeOptions,
    PlannedSession,
    ProcessImage,
    SessionRestarter,
    Terminal,
    carried_env,
    is_interactive_shell,
    launch_directory,
    launcher_script,
    parse_procargs2,
    plan_restart,
    restart_summary,
    reopen_command,
    terminal_of,
)
from claude_swap.settings import RestartSettings

pytestmark = pytest.mark.session_restart

POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32", reason="session restart reads processes on macOS and Linux"
)

# The option lines of `claude --help` (2.1.304) the grammar has to handle:
# short and long spellings, aliases, required/optional/variadic values, and a
# description wrapped onto its own line.
HELP = """Usage: claude [options] [command] [prompt]

Arguments:
  prompt                                Your prompt

Options:
  --add-dir <directories...>            Additional directories to allow tool
                                        access to
  --allowedTools, --allowed-tools <tools...>
      Comma or space-separated list of tool names to allow (e.g. "Bash(git *)
      Edit")
  -c, --continue                        Continue the most recent conversation in
                                        the current directory
  --dangerously-skip-permissions        Bypass all permission checks.
  -d, --debug [filter]                  Enable debug mode with optional category
  --disallowedTools, --disallowed-tools <tools...>
      Comma or space-separated list of tool names to deny
  --effort <level>                      Effort level for the current session
  --fork-session                        When resuming, create a new session ID
  --model <model>                       Model for the current session.
  -n, --name <name>                     Set a display name for this session
  -p, --print                           Print response and exit
  --remote-control [name]               Start an interactive session with Remote
  -r, --resume [value]                  Resume a conversation by session ID or
  --session-id <uuid>                   Use a specific session ID for the
  -w, --worktree [name]                 Create a new git worktree for this

Commands:
  agents [options]                      Manage background agents
"""

FAKE_CLAUDE = """
import glob, json, os, signal, subprocess, sys, time

HELP = {help!r}
# `claude respawn` runs with the environment of whoever calls it; the
# directories it needs sit beside the script.
with open(os.path.join(os.path.dirname(sys.argv[0]), "fake.json")) as f:
    for key, value in json.load(f).items():
        os.environ.setdefault(key, value)

if "--help" in sys.argv[1:]:
    print(HELP)
    sys.exit(0)
args = sys.argv[1:]
if args[:1] == ["respawn"]:
    # Like the real one: stop the job's process, start it again under the
    # same session id.
    for path in glob.glob(os.path.join(os.environ["FAKE_CLAUDE_DIR"], "sessions", "*.json")):
        with open(path) as f:
            old = json.load(f)
        if old.get("jobId") == args[1]:
            try:
                os.kill(old["pid"], signal.SIGTERM)
            except ProcessLookupError:
                pass
            env = {{**os.environ, "FAKE_SESSION_ID": old["sessionId"], "FAKE_KIND": "bg", "FAKE_JOB_ID": args[1]}}
            subprocess.Popen([sys.executable, sys.argv[0]], env=env, start_new_session=True,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            sys.exit(0)
    sys.exit("no such job")
session_id = args[args.index("--resume") + 1] if "--resume" in args else os.environ["FAKE_SESSION_ID"]
sessions = os.path.join(os.environ["FAKE_CLAUDE_DIR"], "sessions")
os.makedirs(sessions, exist_ok=True)
with open(os.path.join(os.environ["FAKE_LOG_DIR"], f"{{os.getpid()}}.json"), "w") as f:
    json.dump({{"argv": sys.argv, "env": dict(os.environ), "cwd": os.getcwd()}}, f)
record = {{
    "pid": os.getpid(),
    "sessionId": session_id,
    "cwd": os.path.realpath(os.getcwd()),
    "startedAt": int(time.time() * 1000),
    "kind": os.environ.get("FAKE_KIND", "interactive"),
    "entrypoint": "cli",
    "status": os.environ.get("FAKE_STATUS", "idle"),
    "name": os.environ.get("FAKE_NAME"),
    "jobId": os.environ.get("FAKE_JOB_ID"),
}}
path = os.path.join(sessions, f"{{os.getpid()}}.json")
with open(path + ".tmp", "w") as f:
    json.dump(record, f)
os.replace(path + ".tmp", path)
while True:
    time.sleep(1)
"""


# ---------------------------------------------------------------------------
# Pure parts
# ---------------------------------------------------------------------------


class TestParseProcargs2:
    def _buffer(self, argv, env, exec_path=b"/usr/local/bin/claude"):
        body = exec_path + b"\0\0\0\0"
        body += b"".join(a + b"\0" for a in argv)
        body += b"".join(e + b"\0" for e in env)
        body += b"\0leftover-after-the-environment\0"
        return len(argv).to_bytes(4, sys.byteorder) + body

    def test_splits_argv_from_environment(self):
        raw = self._buffer(
            [b"claude", b"--model", b"opus[1m]"], [b"PATH=/bin", b"A=b=c", b"PWD=/w"]
        )
        image = parse_procargs2(raw)
        assert image.argv == ("claude", "--model", "opus[1m]")
        assert dict(image.env) == {"PATH": "/bin", "A": "b=c", "PWD": "/w"}

    def test_truncated_buffer_is_unreadable(self):
        assert parse_procargs2(b"\x01") is None
        assert parse_procargs2((5).to_bytes(4, sys.byteorder) + b"/x\0a\0") is None

    @POSIX_ONLY
    def test_reads_this_process(self):
        image = relaunch.read_process(os.getpid())
        assert image is not None
        assert "python" in os.path.basename(image.argv[0]).lower() or "pytest" in " ".join(image.argv)
        assert image.env.get("PATH")


class TestClaudeOptions:
    def test_grammar_from_help(self):
        options = ClaudeOptions.from_help(HELP)
        assert options.arity["--model"] == "required"
        assert options.arity["-r"] == options.arity["--resume"] == "optional"
        assert options.canonical["-r"] == "--resume"
        assert options.arity["--add-dir"] == "variadic"
        assert options.arity["--allowed-tools"] == "variadic"
        assert options.canonical["--allowed-tools"] == "--allowedTools"
        assert options.arity["--dangerously-skip-permissions"] == "flag"
        assert options.canonical["-c"] == "--continue"
        assert "prompt" not in options.arity
        assert "agents" not in options.arity


OPTIONS = ClaudeOptions.from_help(HELP)
SID = "11111111-2222-3333-4444-555555555555"


class TestReopenCommand:
    @pytest.mark.parametrize(
        ("argv", "expected", "dropped"),
        [
            pytest.param(
                ["claude", "--model", "opus[1m]", "--name", "a@b", "--remote-control", "a@b"],
                ["claude", "--model", "opus[1m]", "--name", "a@b", "--remote-control", "a@b"],
                [],
                id="options-kept",
            ),
            pytest.param(
                ["claude", "--model", "sonnet", "fix the build"],
                ["claude", "--model", "sonnet"],
                ["fix the build"],
                id="first-prompt-dropped",
            ),
            pytest.param(
                ["claude", "--remote-control", "fleet", "go on"],
                ["claude", "--remote-control", "fleet"],
                ["go on"],
                id="optional-value-taken-before-prompt",
            ),
            pytest.param(
                ["claude", "--resume", "--model", "x"],
                ["claude", "--model", "x"],
                [],
                id="bare-resume-picker-dropped",
            ),
            pytest.param(
                ["claude", "-r", "old-id", "-c", "--fork-session"],
                ["claude"],
                [],
                id="conversation-pickers-dropped",
            ),
            pytest.param(
                ["claude", "--resume=old-id", "--session-id", "u", "--effort=max"],
                ["claude", "--effort=max"],
                [],
                id="equals-forms",
            ),
            pytest.param(
                ["claude", "--add-dir", "/a", "/b", "--model", "m"],
                ["claude", "--add-dir", "/a", "/b", "--model", "m"],
                [],
                id="variadic-consumes-until-next-option",
            ),
            pytest.param(
                ["claude", "--disallowedTools", "Agent", "Bash"],
                ["claude", "--disallowedTools", "Agent", "Bash"],
                [],
                id="variadic-swallows-trailing-words-like-commander",
            ),
            pytest.param(
                ["claude", "--model", "-weird-model"],
                ["claude", "--model", "-weird-model"],
                [],
                id="required-value-may-look-like-an-option",
            ),
            pytest.param(
                ["claude", "-cd", "hooks"],
                ["claude", "-d", "hooks"],
                [],
                id="bundled-short-flags-split",
            ),
            pytest.param(
                ["claude", "-ropen-id", "--verbose-unknown", "value"],
                ["claude", "--verbose-unknown"],
                ["value"],
                id="unknown-option-kept-as-a-flag-never-keeping-a-prompt",
            ),
            pytest.param(
                ["claude", "-w", "feature", "--model", "m"],
                ["claude", "--model", "m"],
                [],
                id="worktree-dropped",
            ),
            pytest.param(
                ["claude", "--model", "m", "--", "--literal prompt"],
                ["claude", "--model", "m"],
                ["--literal prompt"],
                id="after-double-dash-is-positional",
            ),
        ],
    )
    def test_rewrite(self, argv, expected, dropped):
        out, gone = reopen_command(argv, OPTIONS)
        assert list(out) == expected
        assert list(gone) == dropped

    def test_interpreter_prefix_is_the_program(self):
        out, _ = reopen_command(["node", "/x/cli.js", "--model", "m"], OPTIONS, prefix=2)
        assert list(out) == ["node", "/x/cli.js", "--model", "m"]

    def test_the_session_resumes_its_conversation(self):
        session = PlannedSession(
            pid=1, proc_start=None, session_id=SID, name=None, status=None,
            kind="interactive", where="tmux pane %1",
            terminal=Terminal("tmux", "%1"), shell_pid=None, directory="/",
            command=("claude", "--model", "m"), env={},
        )
        assert session.argv == ("claude", "--model", "m", "--resume", SID)


class TestCarriedEnv:
    def test_keeps_claude_settings_only(self):
        env = {
            "PATH": "/bin",
            "HOME": "/h",
            "TMUX_PANE": "%1",
            "ENABLE_CLAUDEAI_MCP_SERVERS": "false",
            "ANTHROPIC_BASE_URL": "https://proxy",
            "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "64000",
            "CLAUDE_CONFIG_DIR": "/c",
            "HTTPS_PROXY": "http://p:3128",
            "MAX_THINKING_TOKENS": "1000",
            # A claude started from another claude's shell inherits these;
            # they describe the parent, never this session.
            "CLAUDECODE": "1",
            "CLAUDE_CODE_SESSION_ID": "parent",
            "CLAUDE_CODE_ENTRYPOINT": "cli",
            "CLAUDE_PID": "123",
            # Set by the restart itself, never carried from the last one.
            "CLAUDE_CODE_RESUME_THRESHOLD_MINUTES": "2147483647",
        }
        assert carried_env(env) == {
            "ENABLE_CLAUDEAI_MCP_SERVERS": "false",
            "ANTHROPIC_BASE_URL": "https://proxy",
            "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "64000",
            "CLAUDE_CONFIG_DIR": "/c",
            "HTTPS_PROXY": "http://p:3128",
            "MAX_THINKING_TOKENS": "1000",
        }


class TestInteractiveShell:
    @pytest.mark.parametrize(
        ("argv", "interactive"),
        [
            (["-zsh"], True),
            (["/bin/zsh", "-l"], True),
            (["/bin/zsh"], True),
            (["bash", "-il"], True),
            (["fish"], True),
            (["zsh", "-ic", "claudemore"], False),
            (["/bin/sh", "-c", "claude"], False),
            (["bash", "/path/run-agent.sh"], False),
            (["fish", "--command", "claude"], False),
            (["claude"], False),
            (["node", "cli.js"], False),
        ],
    )
    def test_table(self, argv, interactive):
        assert is_interactive_shell(ProcessImage(tuple(argv), {})) is interactive


class TestLaunchDirectory:
    def test_keeps_the_symlinked_spelling_of_the_same_directory(self, tmp_path):
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real)
        assert launch_directory(str(link), str(real)) == str(link)

    def test_falls_back_to_the_recorded_directory(self, tmp_path):
        other = tmp_path / "other"
        other.mkdir()
        assert launch_directory(str(other), str(tmp_path)) == str(tmp_path)
        assert launch_directory(None, str(tmp_path)) == str(tmp_path)
        assert launch_directory(str(tmp_path / "gone"), str(tmp_path)) == str(tmp_path)


class TestTerminalOf:
    def _bin(self, tmp_path, *names):
        for name in names:
            tool = tmp_path / name
            tool.write_text("#!/bin/sh\n")
            tool.chmod(0o755)
        return str(tmp_path)

    def test_tmux_wins_over_the_terminal_it_runs_in(self, tmp_path):
        path = self._bin(tmp_path, "tmux", "muxy")
        term = terminal_of({
            "PATH": path,
            "TMUX": "/tmp/tmux-501/default,123,0",
            "TMUX_PANE": "%7",
            "MUXY_SESSION_ID": "42",
            "ITERM_SESSION_ID": "w0t0p0:ABC",
        })
        assert term == Terminal(
            "tmux", "%7", socket="/tmp/tmux-501/default", tool=str(tmp_path / "tmux")
        )

    def test_muxy(self, tmp_path):
        path = self._bin(tmp_path, "muxy")
        term = terminal_of({"PATH": path, "MUXY_SESSION_ID": "42", "MUXY_DIR": "/p"})
        assert term == Terminal("muxy", "42", tool=str(tmp_path / "muxy"), profile="/p")

    @pytest.mark.skipif(sys.platform != "darwin", reason="iTerm2 is macOS-only")
    def test_iterm(self):
        assert terminal_of({"ITERM_SESSION_ID": "w0t1p0:UUID-1"}) == Terminal("iterm", "UUID-1")

    @pytest.mark.skipif(sys.platform != "darwin", reason="Terminal.app is macOS-only")
    def test_terminal_app_by_tty(self):
        env = {"TERM_PROGRAM": "Apple_Terminal", "TERM_SESSION_ID": "X"}
        assert terminal_of(env, "/dev/ttys004") == Terminal("terminal", "/dev/ttys004")
        assert terminal_of(env, None) is None

    @pytest.mark.parametrize("program", ["WarpTerminal", "vscode", "ghostty", ""])
    def test_a_terminal_cswap_cannot_drive_is_none(self, program):
        assert terminal_of({"TERM_PROGRAM": program}, "/dev/ttys004") is None

    def test_tmux_without_tmux_installed_is_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(relaunch.shutil, "which", lambda *a, **k: None)
        assert terminal_of({"TMUX": "/s,1,0", "TMUX_PANE": "%1"}) is None


class TestFallbackKind:
    @pytest.mark.parametrize(
        ("setting", "darwin", "other"),
        [
            ("auto", "terminal", "tmux"),
            ("terminal", "terminal", None),
            ("iterm", "iterm", None),
            ("tmux", "tmux", "tmux"),
            ("none", None, None),
        ],
    )
    def test_per_platform(self, monkeypatch, setting, darwin, other):
        monkeypatch.setattr(relaunch.sys, "platform", "darwin")
        assert relaunch.fallback_kind(setting) == darwin
        monkeypatch.setattr(relaunch.sys, "platform", "linux")
        assert relaunch.fallback_kind(setting) == other


class TestTmuxBackend:
    def _record(self, monkeypatch, answers):
        calls = []

        def run_tool(command, **kwargs):
            calls.append(list(command))
            return answers.get(command[-1], "")

        monkeypatch.setattr(relaunch, "_run_tool", run_tool)
        return calls

    def test_typing_leaves_copy_mode_first(self, monkeypatch):
        calls = self._record(monkeypatch, {"#{pane_in_mode}": "1"})
        relaunch._Tmux().type_line(Terminal("tmux", "%3", tool="tmux"), " sh x")
        sent = [c[c.index("send-keys"):] for c in calls if "send-keys" in c]
        assert sent == [
            ["send-keys", "-t", "%3", "-X", "cancel"],
            ["send-keys", "-t", "%3", "-l", " sh x"],
            ["send-keys", "-t", "%3", "Enter"],
        ]

    def test_typing_outside_copy_mode_types_only(self, monkeypatch):
        calls = self._record(monkeypatch, {"#{pane_in_mode}": "0"})
        relaunch._Tmux().type_line(Terminal("tmux", "%3", tool="tmux"), " sh x")
        assert not any("cancel" in c for c in calls)

    def test_a_pane_that_could_not_be_closed_gets_its_option_back(self, tmp_path):
        released = []

        class Tmux:
            def keep_pane(self, term):
                pass

            def release_pane(self, term):
                released.append(term.target)

        session = PlannedSession(
            pid=2**22 + 7, proc_start=None, session_id=SID, name="a", status="idle",
            kind="interactive", where="tmux pane %9", terminal=Terminal("tmux", "%9"),
            shell_pid=None, directory="/", command=("claude",), env={},
        )

        def kill(pid, sig):
            raise PermissionError("not yours")

        restarter = SessionRestarter(
            tmp_path, RestartSettings(), terminals={"tmux": Tmux()}, kill=kill,
            progress=lambda line: None,
        )
        report = relaunch.RestartReport()
        closed = []
        restarter._close_batch([session], report, closed)
        assert closed == [] and released == ["%9"]
        assert report.skipped[0].reason == "not yours"


def test_restart_summary():
    assert restart_summary({"reopened": [1, 2, 3], "failed": []}) == "reopened 3 of 3 session(s)"
    assert restart_summary({"reopened": [1], "failed": [2]}).startswith("reopened 1 of 2 session(s);")


@POSIX_ONLY
def test_launcher_script_runs_the_resume_command(tmp_path):
    log = tmp_path / "log.json"
    fake = tmp_path / "bin" / "claude"
    fake.parent.mkdir()
    fake.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        f"with open({str(log)!r}, 'w') as f:\n"
        "    json.dump({'argv': sys.argv[1:], 'env': dict(os.environ), 'cwd': os.getcwd()}, f)\n"
    )
    fake.chmod(0o755)
    work = tmp_path / "it's here"
    work.mkdir()
    session = PlannedSession(
        pid=1,
        proc_start=None,
        session_id=SID,
        name=None,
        status="idle",
        kind="interactive",
        where="tmux pane %1",
        terminal=Terminal("tmux", "%1"),
        shell_pid=None,
        directory=str(work),
        command=("claude", "--model", "opus[1m]", "--name", "x y"),
        env={},
    )
    script = tmp_path / "launch.sh"
    script.write_text(
        launcher_script(session, {"ENABLE_X": "it's $HOME", "CLAUDE_CODE_RESUME_THRESHOLD_MINUTES": "9"})
    )
    subprocess.run(
        ["sh", str(script)],
        check=True,
        env={**os.environ, "PATH": f"{fake.parent}:{os.environ['PATH']}"},
        timeout=30,
    )
    got = json.loads(log.read_text())
    assert got["argv"] == ["--model", "opus[1m]", "--name", "x y", "--resume", SID]
    assert got["env"]["ENABLE_X"] == "it's $HOME"
    assert got["env"]["CLAUDE_CODE_RESUME_THRESHOLD_MINUTES"] == "9"
    assert os.path.samefile(got["cwd"], work)
    assert not script.exists(), "the launcher removes itself"


# ---------------------------------------------------------------------------
# End to end against real processes
# ---------------------------------------------------------------------------


class ShellTerminal:
    """A terminal backend whose typed line runs in ``sh``, in a session of
    its own like a terminal's shell; records what was typed."""

    def __init__(self, env: dict[str, str], app: str = "Muxy"):
        self.env = env
        self.app = app
        self.typed: list[str] = []
        self.windows: list[str] = []
        self.procs: list[subprocess.Popen] = []

    def _run(self, line):
        self.procs.append(
            subprocess.Popen(
                ["sh", "-c", line],
                env=self.env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        )

    def locate(self, term, session_pid, lineage):
        return term, None

    def open_window(self, term, directory, script):
        self.windows.append(str(script))
        self._run(f"sh '{script}'")
        return f"a new {self.app} window"

    def type_line(self, term, line):
        self.typed.append(line)
        self._run(line)


class Fleet:
    """Stand-in sessions, each a fake claude run from its own interactive
    ``sh`` the way a terminal runs one."""

    def __init__(self, tmp_path: Path):
        self.root = tmp_path
        self.claude_dir = tmp_path / ".claude"
        (self.claude_dir / "sessions").mkdir(parents=True)
        self.logs = tmp_path / "logs"
        self.logs.mkdir()
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        claude = self.bin / "claude"
        claude.write_text(f"#!{sys.executable}\n" + FAKE_CLAUDE.format(help=HELP))
        claude.chmod(0o755)
        (self.bin / "fake.json").write_text(json.dumps({
            "FAKE_CLAUDE_DIR": str(self.claude_dir), "FAKE_LOG_DIR": str(self.logs),
        }))
        muxy = self.bin / "muxy"
        muxy.write_text("#!/bin/sh\nexit 0\n")
        muxy.chmod(0o755)
        # The suite may itself run in tmux, iTerm2, Muxy or Claude Code: none
        # of their variables may decide where a stand-in session runs.
        ambient = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("TMUX", "ITERM_", "MUXY_", "TERM_PROGRAM"))
            and not carried_env({key: value})
            and key != "CLAUDECODE"
        }
        self.base_env = {
            **ambient,
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "FAKE_CLAUDE_DIR": str(self.claude_dir),
            "FAKE_LOG_DIR": str(self.logs),
        }
        self.shells: list[subprocess.Popen] = []
        self.terminals: list[ShellTerminal] = []
        self.state_dir = tmp_path / "relaunch"

    def terminal(self, monkeypatch) -> ShellTerminal:
        """The Muxy backend, for planning and for the restart."""
        terminal = ShellTerminal(self.base_env)
        self.terminals.append(terminal)
        monkeypatch.setitem(relaunch._BACKENDS, "muxy", terminal)
        return terminal

    def start(
        self,
        session_id: str,
        *,
        name: str,
        status: str = "idle",
        prompt: str = "",
        in_muxy: bool = True,
    ) -> int:
        """A session in a (fake) Muxy terminal, or with ``in_muxy`` off, in
        an editor's terminal cswap cannot type into."""
        work = self.root / name
        work.mkdir(exist_ok=True)
        where = {"MUXY_SESSION_ID": f"muxy-{name}"} if in_muxy else {"TERM_PROGRAM": "vscode"}
        env = {
            **self.base_env,
            **where,
            "FAKE_SESSION_ID": session_id,
            "FAKE_NAME": name,
            "FAKE_STATUS": status,
            "ENABLE_CLAUDEAI_MCP_SERVERS": "false",
            "CLAUDE_CODE_SESSION_ID": "inherited-from-a-parent-claude",
            "PWD": str(work),
        }
        shell = subprocess.Popen(
            ["sh", "-i"],
            cwd=work,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self.shells.append(shell)
        line = f"claude --model opus --name {name} --remote-control {name}"
        if prompt:
            line += f" '{prompt}'"
        shell.stdin.write(line.encode() + b"\n")
        shell.stdin.flush()
        return self.wait_for_record(session_id)

    def start_background(self, session_id: str, job_id: str) -> int:
        """A background session (``claude --bg``): no terminal, a job id."""
        env = {
            **self.base_env,
            "FAKE_SESSION_ID": session_id,
            "FAKE_NAME": f"job-{job_id}",
            "FAKE_KIND": "bg",
            "FAKE_JOB_ID": job_id,
        }
        subprocess.Popen(
            # Retitled like the real worker: its argv names nothing to run.
            ["claude bg-spare", "--bg-spare"],
            executable=str(self.bin / "claude"),
            cwd=self.root,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return self.wait_for_record(session_id)

    def wait_for_record(self, session_id: str, *, not_pid: int | None = None) -> int:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            for record in (self.claude_dir / "sessions").glob("*.json"):
                try:
                    data = json.loads(record.read_text())
                except (OSError, json.JSONDecodeError):
                    continue
                pid = data["pid"]
                if data["sessionId"] == session_id and pid != not_pid and relaunch.is_pid_alive(pid):
                    return pid
            time.sleep(0.05)
        raise AssertionError(f"no live record for {session_id}")

    def set_status(self, pid: int, status: str) -> None:
        path = self.claude_dir / "sessions" / f"{pid}.json"
        data = json.loads(path.read_text())
        data["status"] = status
        path.write_text(json.dumps(data))

    def log_of(self, pid: int) -> dict:
        return json.loads((self.logs / f"{pid}.json").read_text())

    def restarter(
        self, terminal: ShellTerminal, *, foreground=lambda pid: True, **settings
    ) -> SessionRestarter:
        """A restarter on the fake Muxy backend, with a fake tmux backend as
        restart.fallback (``self.fallback``)."""
        defaults = {
            "close_timeout_seconds": 5.0,
            "reopen_timeout_seconds": 20.0,
            "reopen_stagger_seconds": 0.0,
            "fallback": "tmux",
        }
        self.fallback = ShellTerminal(self.base_env, app="fallback")
        self.terminals.append(self.fallback)
        return SessionRestarter(
            self.state_dir,
            RestartSettings(**{**defaults, **settings}),
            claude_dir=self.claude_dir,
            progress=lambda line: None,
            terminals={"muxy": terminal, "tmux": self.fallback},
            foreground=foreground,
        )

    def close(self) -> None:
        for record in (self.claude_dir / "sessions").glob("*.json"):
            try:
                os.kill(json.loads(record.read_text())["pid"], signal.SIGKILL)
            except (OSError, ValueError, KeyError):
                pass
        for proc in [*self.shells, *(p for t in self.terminals for p in t.procs)]:
            proc.kill()
            proc.wait()


@pytest.fixture
def fleet(tmp_path):
    f = Fleet(tmp_path)
    yield f
    f.close()


@POSIX_ONLY
class TestRestartEndToEnd:
    def test_plan_reads_the_running_session(self, fleet, monkeypatch):
        fleet.terminal(monkeypatch)
        pid = fleet.start(SID, name="alpha", prompt="first prompt")
        plan = plan_restart(fleet.claude_dir)
        assert [s.pid for s in plan.sessions] == [pid]
        session = plan.sessions[0]
        assert session.terminal.kind == "muxy" and session.terminal.target == "muxy-alpha"
        assert session.shell_pid == fleet.shells[0].pid
        assert session.argv[-2:] == ("--resume", SID)
        assert "--model" in session.argv and "first prompt" not in session.argv
        assert session.dropped == ("first prompt",)
        assert session.env == {"ENABLE_CLAUDEAI_MCP_SERVERS": "false"}

    def test_close_switch_reopen(self, fleet, monkeypatch):
        terminal = fleet.terminal(monkeypatch)
        old_a = fleet.start(SID, name="alpha")
        other = "99999999-2222-3333-4444-555555555555"
        old_b = fleet.start(other, name="beta")
        alive_during_switch = []

        def switch():
            alive_during_switch.extend(relaunch.is_pid_alive(p) for p in (old_a, old_b))
            return {"from": None, "to": {"number": 2}, "warnings": []}

        restarter = fleet.restarter(terminal)
        result = restarter(switch)

        assert alive_during_switch == [False, False], "sessions close before the switch"
        new_a = fleet.wait_for_record(SID, not_pid=old_a)
        fleet.wait_for_record(other, not_pid=old_b)
        assert result["restart"]["failed"] == []
        assert {r["sessionId"] for r in result["restart"]["reopened"]} == {SID, other}
        assert result["warnings"] == []
        assert all(line.startswith(" sh ") for line in terminal.typed)

        log = fleet.log_of(new_a)
        assert log["argv"][1:] == [
            "--model", "opus", "--name", "alpha", "--remote-control", "alpha", "--resume", SID,
        ]
        assert log["env"]["ENABLE_CLAUDEAI_MCP_SERVERS"] == "false"
        assert log["env"]["CLAUDE_CODE_RESUME_THRESHOLD_MINUTES"] == "2147483647"
        assert log["env"]["CLAUDE_CODE_RESUME_INTERRUPTED_TURN"] == "1"
        assert log["env"].get("CLAUDE_CODE_SESSION_ID") != "inherited-from-a-parent-claude"
        assert os.path.samefile(log["cwd"], fleet.root / "alpha")
        assert not list(fleet.state_dir.glob("*.command")), "launchers remove themselves"
        plan = json.loads((fleet.state_dir / "plan.json").read_text())
        assert {e["state"] for e in plan["sessions"]} == {"reopened"}

    def test_resume_ask_leaves_the_summary_offer_alone(self, fleet, monkeypatch):
        terminal = fleet.terminal(monkeypatch)
        old = fleet.start(SID, name="alpha")
        restarter = fleet.restarter(terminal, resume="ask", continue_interrupted=False)
        restarter(lambda: {"from": None, "to": None, "warnings": []})
        env = fleet.log_of(fleet.wait_for_record(SID, not_pid=old))["env"]
        assert "CLAUDE_CODE_RESUME_THRESHOLD_MINUTES" not in env
        assert "CLAUDE_CODE_RESUME_INTERRUPTED_TURN" not in env

    def test_a_failed_switch_still_reopens_the_sessions(self, fleet, monkeypatch):
        terminal = fleet.terminal(monkeypatch)
        old = fleet.start(SID, name="alpha")
        restarter = fleet.restarter(terminal)

        def switch():
            raise SwitchError("the switch failed")

        with pytest.raises(SwitchError, match="the switch failed"):
            restarter(switch)
        assert fleet.wait_for_record(SID, not_pid=old) != old
        assert [s.session_id for s in restarter.last_report.reopened] == [SID]

    def test_a_working_session_is_closed_once_its_turn_ends(self, fleet, monkeypatch):
        terminal = fleet.terminal(monkeypatch)
        busy = fleet.start(SID, name="alpha", status="busy")
        restarter = fleet.restarter(terminal, busy_timeout_seconds=60.0)

        def sleep(seconds):
            # The turn ends while the restart waits.
            if relaunch.is_pid_alive(busy):
                fleet.set_status(busy, "idle")
            time.sleep(0.05)

        restarter.sleep = sleep
        result = restarter(lambda: {"from": None, "to": None, "warnings": []})
        assert result["restart"]["interrupted"] == []
        assert [r["sessionId"] for r in result["restart"]["reopened"]] == [SID]

    def test_working_sessions_are_closed_mid_turn_when_the_wait_runs_out(self, fleet, monkeypatch):
        terminal = fleet.terminal(monkeypatch)
        fleet.start(SID, name="alpha", status="busy")
        restarter = fleet.restarter(terminal, busy_timeout_seconds=0.0)
        result = restarter(lambda: {"from": None, "to": None, "warnings": []})
        assert [r["sessionId"] for r in result["restart"]["interrupted"]] == [SID]
        assert [r["sessionId"] for r in result["restart"]["reopened"]] == [SID]

    def test_abort_calls_the_switch_off_and_reopens_what_it_closed(self, fleet, monkeypatch):
        terminal = fleet.terminal(monkeypatch)
        idle = fleet.start(SID, name="alpha")
        busy_id = "99999999-2222-3333-4444-555555555555"
        busy = fleet.start(busy_id, name="beta", status="busy")
        restarter = fleet.restarter(terminal, busy_timeout_seconds=0.0, on_busy_timeout="abort")
        switched = []

        with pytest.raises(RestartBlockedError) as raised:
            restarter(lambda: switched.append(True) or {})

        assert switched == []
        assert [s.session_id for s in raised.value.busy] == [busy_id]
        assert relaunch.is_pid_alive(busy), "the working session is left alone"
        assert fleet.wait_for_record(SID, not_pid=idle) != idle, "the idle one comes back"

    def test_close_busy_flag_skips_the_wait(self, fleet, monkeypatch):
        terminal = fleet.terminal(monkeypatch)
        fleet.start(SID, name="alpha", status="busy")
        restarter = fleet.restarter(terminal, busy_timeout_seconds=600.0, on_busy_timeout="abort")
        restarter.close_busy = True
        result = restarter(lambda: {"from": None, "to": None, "warnings": []})
        assert [r["sessionId"] for r in result["restart"]["interrupted"]] == [SID]

    def test_a_session_that_exits_while_waiting_is_not_reopened(self, fleet, monkeypatch):
        terminal = fleet.terminal(monkeypatch)
        busy = fleet.start(SID, name="alpha", status="busy")
        restarter = fleet.restarter(terminal, busy_timeout_seconds=60.0)

        def sleep(seconds):
            if relaunch.is_pid_alive(busy):
                os.kill(busy, signal.SIGKILL)
            time.sleep(0.2)

        restarter.sleep = sleep
        result = restarter(lambda: {"from": None, "to": None, "warnings": []})
        assert [r["sessionId"] for r in result["restart"]["exited"]] == [SID]
        assert result["restart"]["reopened"] == []
        assert terminal.typed == []

    def test_a_session_that_moves_to_another_conversation_reopens_that_one(
        self, fleet, monkeypatch
    ):
        terminal = fleet.terminal(monkeypatch)
        busy = fleet.start(SID, name="alpha", status="busy")
        cleared = "77777777-2222-3333-4444-555555555555"
        restarter = fleet.restarter(terminal, busy_timeout_seconds=60.0)

        def sleep(seconds):
            # /clear starts a new conversation in the same process.
            if relaunch.is_pid_alive(busy):
                path = fleet.claude_dir / "sessions" / f"{busy}.json"
                data = json.loads(path.read_text())
                data.update(sessionId=cleared, status="idle")
                path.write_text(json.dumps(data))
            time.sleep(0.05)

        restarter.sleep = sleep
        result = restarter(lambda: {"from": None, "to": None, "warnings": []})
        assert [r["sessionId"] for r in result["restart"]["reopened"]] == [cleared]
        assert "--resume" in fleet.log_of(fleet.wait_for_record(cleared))["argv"]

    def test_a_new_plan_keeps_sessions_an_earlier_restart_left_closed(self, fleet, monkeypatch):
        terminal = fleet.terminal(monkeypatch)
        lost = fleet.start(SID, name="alpha")
        earlier = plan_restart(fleet.claude_dir)
        restarter = fleet.restarter(terminal)
        restarter._save(earlier.sessions)
        os.kill(lost, signal.SIGTERM)
        restarter._save_state(earlier.sessions[0], "failed")
        while relaunch.is_pid_alive(lost):
            time.sleep(0.05)

        # A later switch with nothing to restart must not forget it.
        restarter(lambda: {"from": None, "to": None, "warnings": []})
        entries = json.loads((fleet.state_dir / "plan.json").read_text())["sessions"]
        assert [(e["sessionId"], e["state"]) for e in entries] == [(SID, "failed")]
        assert [s.session_id for s in restarter.reopen_unfinished().reopened] == [SID]

    def test_a_terminal_cswap_cannot_type_into_reopens_in_the_fallback(self, fleet, monkeypatch):
        terminal = fleet.terminal(monkeypatch)
        old = fleet.start(SID, name="alpha", in_muxy=False)
        restarter = fleet.restarter(terminal)
        [session] = restarter.plan().sessions
        assert session.terminal is None and session.where.startswith("an editor's terminal")
        result = restarter(lambda: {"from": None, "to": None, "warnings": []})
        assert fleet.wait_for_record(SID, not_pid=old) != old
        assert [r["in"] for r in result["restart"]["reopened"]] == ["a new fallback window"]
        assert terminal.typed == [] and len(fleet.fallback.windows) == 1

    def test_with_no_fallback_such_a_session_is_left_running(self, fleet, monkeypatch):
        fleet.terminal(monkeypatch)
        fleet.start(SID, name="alpha", in_muxy=False)
        plan = plan_restart(fleet.claude_dir, fallback="none")
        assert plan.sessions == ()
        assert "restart.fallback is none" in plan.skipped[0].reason

    def test_a_terminal_that_does_not_take_it_back_gets_a_new_window_in_its_app(
        self, fleet, monkeypatch
    ):
        monkeypatch.setattr(relaunch, "_TERMINAL_RETURN_TIMEOUT_S", 0.2)
        terminal = fleet.terminal(monkeypatch)
        old = fleet.start(SID, name="alpha")
        restarter = fleet.restarter(terminal, foreground=lambda pid: False)
        result = restarter(lambda: {"from": None, "to": None, "warnings": []})
        assert fleet.wait_for_record(SID, not_pid=old) != old
        assert terminal.typed == [] and len(terminal.windows) == 1
        assert fleet.fallback.windows == []
        assert [r["in"] for r in result["restart"]["reopened"]] == ["a new Muxy window"]

    def test_a_session_its_terminal_brought_back_is_not_opened_twice(self, fleet, monkeypatch):
        monkeypatch.setattr(relaunch, "_TERMINAL_RETURN_TIMEOUT_S", 0.2)
        terminal = fleet.terminal(monkeypatch)
        fleet.start(SID, name="alpha")
        relaunched = []

        def foreground(pid):
            # A wrapper loop restarts claude on its own instead of handing
            # the terminal back to the shell.
            if not relaunched:
                relaunched.append(fleet.start(SID, name="alpha-wrapper"))
            return False

        restarter = fleet.restarter(terminal, foreground=foreground)
        result = restarter(lambda: {"from": None, "to": None, "warnings": []})
        assert terminal.windows == [] and fleet.fallback.windows == []
        assert [r["in"] for r in result["restart"]["reopened"]] == ["its terminal, on its own"]

    def test_background_sessions_respawn_after_the_switch(self, fleet, monkeypatch):
        terminal = fleet.terminal(monkeypatch)
        old = fleet.start_background(SID, "job1")
        restarter = fleet.restarter(terminal)
        [session] = restarter.plan().sessions
        assert session.kind == "background" and session.job_id == "job1"
        alive_during_switch = []

        def switch():
            alive_during_switch.append(relaunch.is_pid_alive(old))
            return {"from": None, "to": None, "warnings": []}

        result = restarter(switch)
        assert alive_during_switch == [True], "background sessions are not closed first"
        assert fleet.wait_for_record(SID, not_pid=old) != old
        assert [r["in"] for r in result["restart"]["reopened"]] == [
            "its background job (claude respawn)"
        ]

    def test_background_sessions_stay_put_when_the_switch_fails(self, fleet, monkeypatch):
        terminal = fleet.terminal(monkeypatch)
        old = fleet.start_background(SID, "job1")
        restarter = fleet.restarter(terminal)

        def switch():
            raise SwitchError("no")

        with pytest.raises(SwitchError):
            restarter(switch)
        assert relaunch.is_pid_alive(old)
        assert restarter.last_report.reopened == []

    def test_ctrl_c_while_waiting_reopens_what_was_closed(self, fleet, monkeypatch):
        terminal = fleet.terminal(monkeypatch)
        idle = fleet.start(SID, name="alpha")
        busy_id = "99999999-2222-3333-4444-555555555555"
        busy = fleet.start(busy_id, name="beta", status="busy")
        restarter = fleet.restarter(terminal, busy_timeout_seconds=60.0)
        pressed = []

        def sleep(seconds):
            # One Ctrl-C, while the idle session is on its way down.
            if not pressed:
                pressed.append(True)
                raise KeyboardInterrupt
            time.sleep(0.05)

        restarter.sleep = sleep
        switched = []

        with pytest.raises(KeyboardInterrupt):
            restarter(lambda: switched.append(True) or {})

        assert switched == []
        assert fleet.wait_for_record(SID, not_pid=idle) != idle, "the closed one is back"
        assert relaunch.is_pid_alive(busy), "the working one was never touched"

    @pytest.mark.skipif(
        threading.current_thread() is not threading.main_thread(),
        reason="signal handlers need the main thread",
    )
    def test_sigterm_mid_restart_still_reopens(self, fleet, monkeypatch):
        terminal = fleet.terminal(monkeypatch)
        old = fleet.start(SID, name="alpha")
        restarter = fleet.restarter(terminal)
        before = signal.getsignal(signal.SIGTERM)

        def switch():
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(1)  # the handler raises here
            return {}

        with pytest.raises(relaunch.RestartInterrupted):
            restarter(switch)
        assert fleet.wait_for_record(SID, not_pid=old) != old
        assert signal.getsignal(signal.SIGTERM) is before, "the old handler is back"

    def test_a_record_caught_mid_rewrite_is_not_taken_for_an_exit(self, fleet, monkeypatch):
        terminal = fleet.terminal(monkeypatch)
        busy = fleet.start(SID, name="alpha", status="busy")
        record = fleet.claude_dir / "sessions" / f"{busy}.json"
        good = json.loads(record.read_text())
        steps = iter(["torn", "idle"])

        def sleep(seconds):
            step = next(steps, None)
            if step == "torn":
                record.write_text('{"pid": ')  # what a reader sees mid-write
            elif step == "idle":
                record.write_text(json.dumps({**good, "status": "idle"}))
            time.sleep(0.05)

        restarter = fleet.restarter(terminal, busy_timeout_seconds=60.0)
        restarter.sleep = sleep
        result = restarter(lambda: {"from": None, "to": None, "warnings": []})
        assert result["restart"]["exited"] == []
        assert [r["sessionId"] for r in result["restart"]["reopened"]] == [SID]

    def test_idle_sessions_close_together(self, fleet, monkeypatch):
        terminal = fleet.terminal(monkeypatch)
        fleet.start(SID, name="alpha")
        fleet.start("99999999-2222-3333-4444-555555555555", name="beta")
        log = []
        restarter = fleet.restarter(terminal)
        restarter.kill = lambda pid, sig: (log.append(("kill", sig)), os.kill(pid, sig))
        restarter.sleep = lambda seconds: (log.append(("sleep", seconds)), time.sleep(0.05))
        restarter(lambda: {"from": None, "to": None, "warnings": []})
        assert log[:2] == [("kill", signal.SIGTERM), ("kill", signal.SIGTERM)], (
            "both are signalled before waiting on either"
        )

    def test_reopens_are_staggered(self, fleet, monkeypatch):
        terminal = fleet.terminal(monkeypatch)
        fleet.start(SID, name="alpha")
        fleet.start("99999999-2222-3333-4444-555555555555", name="beta")
        slept = []
        restarter = fleet.restarter(terminal, reopen_stagger_seconds=0.3)
        restarter.sleep = lambda seconds: (slept.append(seconds), time.sleep(0.05))
        restarter(lambda: {"from": None, "to": None, "warnings": []})
        assert slept.count(0.3) == 1, "one gap between two reopens"

    def test_settings_are_read_at_every_switch(self, fleet, monkeypatch):
        terminal = fleet.terminal(monkeypatch)
        old = fleet.start(SID, name="alpha")
        current = {"settings": RestartSettings(on_switch=False)}
        restarter = SessionRestarter(
            fleet.state_dir,
            load_settings=lambda: current["settings"],
            claude_dir=fleet.claude_dir,
            terminals={"muxy": terminal},
            foreground=lambda pid: True,
        )
        result = restarter(lambda: {"switched": True})
        assert result == {"switched": True} and relaunch.is_pid_alive(old)

        current["settings"] = RestartSettings(reopen_stagger_seconds=0.0)
        result = restarter(lambda: {"switched": True})
        assert [r["sessionId"] for r in result["restart"]["reopened"]] == [SID]

    def test_reopen_finishes_an_interrupted_restart(self, fleet, monkeypatch):
        terminal = fleet.terminal(monkeypatch)
        old = fleet.start(SID, name="alpha")
        plan = plan_restart(fleet.claude_dir)
        restarter = fleet.restarter(terminal)
        # A restart that died between the close and the reopen.
        restarter._save(plan.sessions)
        os.kill(old, signal.SIGTERM)
        restarter._save_state(plan.sessions[0], "closed")
        while relaunch.is_pid_alive(old):
            time.sleep(0.05)

        report = restarter.reopen_unfinished()
        assert [s.session_id for s in report.reopened] == [SID]
        assert restarter.reopen_unfinished().reopened == [], "nothing left to reopen"
