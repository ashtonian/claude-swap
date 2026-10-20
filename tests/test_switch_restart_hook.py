"""The switcher's ``around_switch`` seam: the restart wraps the credential
swap alone, and the display after it runs once the sessions are back."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from claude_swap.models import Platform
from claude_swap.switcher import ClaudeAccountSwitcher


def _switcher(temp_home: Path) -> ClaudeAccountSwitcher:
    s = ClaudeAccountSwitcher()
    s.platform = Platform.LINUX
    s._setup_directories()
    s._init_sequence_file()
    for num, email in ((1, "a@example.com"), (2, "b@example.com")):
        s._write_account_credentials(
            str(num), email,
            json.dumps({"claudeAiOauth": {"accessToken": f"sk-{num}", "refreshToken": f"rt-{num}"}}),
        )
        s._write_account_config(
            str(num), email,
            json.dumps({"oauthAccount": {"emailAddress": email, "accountUuid": f"uuid-{num}"}}),
        )
        data = s._get_sequence_data()
        data["accounts"][str(num)] = {
            "email": email, "uuid": f"uuid-{num}", "organizationUuid": "",
            "organizationName": "", "added": "2024-01-01T00:00:00Z",
        }
        data["sequence"] = sorted({*data["sequence"], num})
        data["activeAccountNumber"] = data["activeAccountNumber"] or num
        s._write_json(s.sequence_file, data)
    (temp_home / ".claude" / ".credentials.json").write_text(json.dumps({
        "claudeAiOauth": {"accessToken": "sk-1", "refreshToken": "rt-1"},
    }))
    (temp_home / ".claude.json").write_text(json.dumps({
        "oauthAccount": {"emailAddress": "a@example.com", "accountUuid": "uuid-1"},
    }))
    return s


def test_the_restart_wraps_the_swap_and_the_display_follows(temp_home, capsys):
    s = _switcher(temp_home)
    during = []

    def around(swap):
        op = swap()
        during.append(capsys.readouterr().out)
        return {**op, "restart": {"reopened": [], "failed": []}}

    s.around_switch = around
    with patch.object(s, "list_accounts"):
        s.switch_to("2")
    after = capsys.readouterr().out
    assert "Switched to" not in during[0], "nothing is shown while sessions are down"
    assert "Switched to Account-2" in after
    assert "Restart Claude Code" not in after and "next message" not in after


def test_a_switch_the_restart_let_through_keeps_its_advice(temp_home, capsys):
    s = _switcher(temp_home)
    s.around_switch = lambda swap: swap()  # restart.onSwitch off
    with patch.object(s, "list_accounts"):
        s.switch_to("2")
    out = capsys.readouterr().out
    assert "Restart Claude Code" in out or "next message" in out


def test_a_no_op_switch_never_reaches_the_restart(temp_home):
    s = _switcher(temp_home)
    calls = []
    s.around_switch = lambda swap: calls.append(swap) or swap()
    s.switch_to("1", json_output=True)
    assert calls == []
