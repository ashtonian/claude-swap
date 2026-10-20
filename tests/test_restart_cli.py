"""The CLI around the session restart: ``switch --restart/--no-restart/
--close-busy``, the detached worker a switch run from inside a session hands
over to, ``cswap sessions`` and ``cswap reopen``."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap import cli, relaunch
from claude_swap.relaunch import (
    PlannedSession,
    RestartPlan,
    RestartReport,
    SkippedSession,
    Terminal,
)

pytestmark = pytest.mark.session_restart

SESSION = PlannedSession(
    pid=4321,
    proc_start=None,
    session_id="11111111-2222-3333-4444-555555555555",
    name="agent-1",
    status="busy",
    kind="interactive",
    where="tmux pane %3",
    terminal=Terminal("tmux", "%3"),
    shell_pid=4320,
    directory="/work",
    command=("claude", "--model", "opus"),
    env={},
)


class FakeRestarter:
    def __init__(self, state_dir: Path, plan: RestartPlan):
        self.state_dir = state_dir
        self._plan = plan
        self.last_report = RestartReport(reopened=[SESSION])

    def plan(self) -> RestartPlan:
        return self._plan

    def enabled_now(self) -> bool:
        return True

    def __call__(self, switch):
        return switch()


def _main(argv: list[str]):
    with patch("claude_swap.cli.ClaudeAccountSwitcher") as switcher_cls, \
         patch.object(sys, "argv", ["claude-swap", *argv]), \
         patch("os.geteuid", return_value=1000, create=True), \
         patch("claude_swap.update_check.check_for_update", return_value=None):
        switcher_cls.return_value.current_account_number.return_value = "2"
        switcher_cls.return_value.account_email.return_value = "b@example.com"
        switcher_cls.return_value.resolve_account.return_value = ("2", "b@example.com", "")
        cli.main()
    return switcher_cls.return_value


class TestRestartFlags:
    def test_restart_flags_belong_to_switch(self, capsys):
        with pytest.raises(SystemExit) as exited:
            _main(["list", "--restart"])
        assert exited.value.code == 2
        assert "only be used with 'switch'" in capsys.readouterr().err

    def test_close_busy_needs_the_restart(self, capsys):
        with pytest.raises(SystemExit):
            _main(["switch", "2", "--no-restart", "--close-busy"])
        assert "drop --no-restart" in capsys.readouterr().err

    def test_flags_reach_the_restarter(self, tmp_path):
        seen = {}

        def restarter_for(backup_dir, **kwargs):
            seen.update(kwargs)
            return None

        with patch("claude_swap.relaunch.restarter_for", restarter_for):
            switcher = _main(["switch", "2", "--no-restart"])
        assert seen["enabled"] is False and seen["close_busy"] is False
        assert switcher.around_switch is None
        switcher.switch_to.assert_called_once()


class TestDetach:
    def _patched(self, tmp_path, plan):
        restarter = FakeRestarter(tmp_path / "relaunch", plan)
        spawned = []

        def spawn_worker(argv, log_path):
            spawned.append((list(argv), log_path))
            return 9999

        return restarter, spawned, (
            patch("claude_swap.relaunch.restarter_for", lambda *a, **k: restarter),
            patch("claude_swap.relaunch.spawn_worker", spawn_worker),
        )

    def test_a_switch_inside_a_session_hands_over_to_a_worker(self, tmp_path, capsys, monkeypatch):
        monkeypatch.delenv(relaunch.WORKER_ENV, raising=False)
        plan = RestartPlan(sessions=(), skipped=(), hosting=(SESSION,), unreadable=0)
        restarter, spawned, patches = self._patched(tmp_path, plan)
        with patches[0], patches[1]:
            switcher = _main(["switch", "2"])
        switcher.switch_to.assert_not_called()
        assert spawned == [(["switch", "2"], tmp_path / "relaunch" / "worker.log")]
        out = capsys.readouterr().out
        assert "agent-1 (pid 4321)" in out and "pid 9999" in out

    def test_json_reports_the_pending_worker(self, tmp_path, capsys, monkeypatch):
        monkeypatch.delenv(relaunch.WORKER_ENV, raising=False)
        plan = RestartPlan(sessions=(), skipped=(), hosting=(SESSION,), unreadable=0)
        _, _, patches = self._patched(tmp_path, plan)
        with patches[0], patches[1]:
            _main(["switch", "2", "--json"])
        payload = json.loads(capsys.readouterr().out)
        assert payload["pending"] is True and payload["workerPid"] == 9999
        assert payload["reason"] == "restarting-in-background"

    def test_no_hosting_session_switches_here(self, tmp_path, monkeypatch):
        monkeypatch.delenv(relaunch.WORKER_ENV, raising=False)
        plan = RestartPlan(sessions=(SESSION,), skipped=(), hosting=(), unreadable=0)
        restarter, spawned, patches = self._patched(tmp_path, plan)
        with patches[0], patches[1]:
            switcher = _main(["switch", "2"])
        assert spawned == []
        assert switcher.around_switch is restarter
        switcher.switch_to.assert_called_once()

    def test_the_worker_switches_and_notifies(self, tmp_path, monkeypatch):
        monkeypatch.setenv(relaunch.WORKER_ENV, "1")  # pid 1 never exits: no wait
        monkeypatch.setattr(relaunch, "wait_for_launcher", lambda timeout: None)
        plan = RestartPlan(sessions=(), skipped=(), hosting=(SESSION,), unreadable=0)
        _, spawned, patches = self._patched(tmp_path, plan)
        notes = []
        with patches[0], patches[1], \
             patch("claude_swap.cli.notify", lambda title, msg: notes.append((title, msg))):
            switcher = _main(["switch", "2"])
        assert spawned == []
        switcher.switch_to.assert_called_once()
        assert notes == [
            ("claude-swap: switched", "Account-2 (b@example.com) · reopened 1 of 1 session(s)")
        ]


class TestWorkerTarget:
    @pytest.mark.parametrize(
        ("argv", "expected"),
        [
            (["switch", "dev"], ["switch", "2"]),
            (["--switch-to", "dev", "--json"], ["--switch-to", "2", "--json"]),
            (["--switch-to=dev"], ["--switch-to=2"]),
        ],
    )
    def test_the_worker_gets_the_account_number(self, argv, expected):
        assert cli._with_target(argv, "dev", "2") == expected

    def test_an_alias_is_resolved_before_handing_over(self, tmp_path, monkeypatch):
        monkeypatch.delenv(relaunch.WORKER_ENV, raising=False)
        plan = RestartPlan(sessions=(), skipped=(), hosting=(SESSION,), unreadable=0)
        restarter = FakeRestarter(tmp_path / "relaunch", plan)
        spawned = []
        with patch("claude_swap.relaunch.restarter_for", lambda *a, **k: restarter), \
             patch("claude_swap.relaunch.spawn_worker",
                   lambda argv, log: spawned.append(list(argv)) or 1):
            _main(["switch", "dev"])
        assert spawned == [["switch", "2"]]

    def test_an_unexpected_worker_failure_is_announced(self, tmp_path, monkeypatch):
        monkeypatch.setenv(relaunch.WORKER_ENV, "1")
        monkeypatch.setattr(relaunch, "wait_for_launcher", lambda timeout: None)
        plan = RestartPlan(sessions=(), skipped=(), hosting=(), unreadable=0)
        restarter = FakeRestarter(tmp_path / "relaunch", plan)
        notes = []
        with patch("claude_swap.relaunch.restarter_for", lambda *a, **k: restarter), \
             patch("claude_swap.cli.notify", lambda title, msg: notes.append(title)), \
             patch("claude_swap.cli.ClaudeAccountSwitcher") as switcher_cls, \
             patch.object(sys, "argv", ["claude-swap", "switch", "2"]), \
             patch("os.geteuid", return_value=1000, create=True):
            switcher_cls.return_value.switch_to.side_effect = RuntimeError("boom")
            with pytest.raises(RuntimeError):
                cli.main()
        assert notes == ["claude-swap: switch failed"]

    def test_close_busy_asks_for_a_restart(self):
        seen = {}

        def restarter_for(backup_dir, **kwargs):
            seen.update(kwargs)
            return None

        with patch("claude_swap.relaunch.restarter_for", restarter_for):
            _main(["switch", "2", "--close-busy"])
        assert seen["enabled"] is True and seen["close_busy"] is True


class TestSessionsCommand:
    def test_json_lists_what_a_switch_restarts(self, temp_home, capsys):
        plan = RestartPlan(
            sessions=(SESSION,),
            skipped=(SkippedSession(77, "abc", None, "it runs in WarpTerminal"),),
            hosting=(),
            unreadable=0,
        )
        with patch("claude_swap.relaunch.plan_restart", lambda *a, **k: plan):
            cli._sessions_command(["--json"])
        out = json.loads(capsys.readouterr().out)
        assert out["onSwitch"] is True
        assert [s["pid"] for s in out["sessions"]] == [4321]
        assert out["skipped"][0]["reason"] == "it runs in WarpTerminal"

    def test_human_listing(self, temp_home, capsys):
        plan = RestartPlan(sessions=(SESSION,), skipped=(), hosting=(SESSION,), unreadable=1)
        with patch("claude_swap.relaunch.plan_restart", lambda *a, **k: plan):
            cli._sessions_command([])
        out = capsys.readouterr().out
        assert "closes and reopens 1 session(s)" in out
        assert "this command runs inside it" in out
        assert "1 session record(s) could not be read" in out


class TestReopenCommand:
    def test_nothing_to_reopen(self, temp_home, capsys):
        with pytest.raises(SystemExit) as exited:
            cli._reopen_command([])
        assert exited.value.code == 0
        assert "Nothing to reopen." in capsys.readouterr().out
