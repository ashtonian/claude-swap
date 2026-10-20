"""``autoswitch.enabled`` and ``autoswitch.triggers``.

Automatic switching is off until turned on: a live engine refuses to start,
a dry run always may. ``triggers`` narrows what may set off a switch — a
usage window over its line (5h, 7d, a per-model window) or the engine's
failover and consume-first moves — so "weekly" switches on the weekly
limits and waits out the 5-hour window, exhausted or not.
"""

from __future__ import annotations

import argparse
import json

import pytest

from claude_swap import cli
from claude_swap.autoswitch import (
    AutoSwitchEngine,
    ConfigWarningEvent,
    NoSwitchEvent,
    SwitchEvent,
    TickOutcome,
)
from claude_swap.exceptions import AutoSwitchDisabledError, ConfigError
from claude_swap.settings import (
    SETTING_SPECS,
    SWITCH_TRIGGERS,
    AutoSwitchSettings,
    load_settings,
    parse_setting_value,
    parse_triggers,
    settings_path,
)
from tests.test_autoswitch import EngineHarness

SPEC = SETTING_SPECS["autoswitch.triggers"]


def _usage(five: float, seven: float, **models: float) -> dict:
    usage = {"five_hour": {"pct": five}, "seven_day": {"pct": seven}}
    if models:
        usage["scoped"] = [{"name": name, "pct": pct} for name, pct in models.items()]
    return usage


@pytest.fixture
def fleet(temp_home):
    def build(**settings) -> EngineHarness:
        h = EngineHarness(temp_home, **settings)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.make_live("a@example.com", 1)
        return h

    return build


def _no_switch_reasons(h: EngineHarness) -> list[str]:
    return [e.reason for e in h.events if isinstance(e, NoSwitchEvent)]


class TestParseTriggers:
    def test_unset_allows_every_trigger(self):
        assert parse_triggers(None) == frozenset(SWITCH_TRIGGERS)

    @pytest.mark.parametrize(
        ("value", "allowed"),
        [
            ("weekly", {"7d", "model"}),
            ("all", set(SWITCH_TRIGGERS)),
            ("weekly,failover", {"7d", "model", "failover"}),
            ("5h", {"5h"}),
            ("consume-first,7d", {"7d", "consume-first"}),
        ],
    )
    def test_groups_expand(self, value, allowed):
        assert parse_triggers(value) == allowed

    def test_config_set_normalizes(self):
        assert parse_setting_value(SPEC, " Weekly , failover,weekly") == "weekly,failover"

    @pytest.mark.parametrize("value", ["", " , ", "5h,hourly"])
    def test_config_set_refuses_unknown_or_empty(self, value):
        with pytest.raises(ConfigError):
            parse_setting_value(SPEC, value)

    def test_load_drops_unknown_names(self, tmp_path, caplog):
        settings_path(tmp_path).write_text(
            json.dumps({"autoswitch": {"triggers": "7d,hourly"}})
        )
        assert load_settings(tmp_path).triggers == "7d"
        assert "hourly" in caplog.text

    @pytest.mark.parametrize("stored", [5, "", "hourly", None])
    def test_load_falls_back_to_every_trigger(self, tmp_path, stored):
        settings_path(tmp_path).write_text(json.dumps({"autoswitch": {"triggers": stored}}))
        assert load_settings(tmp_path).triggers is None

    def test_cli_flag_is_validated(self):
        assert cli._triggers_arg("weekly") == "weekly"
        with pytest.raises(argparse.ArgumentTypeError):
            cli._triggers_arg("hourly")


class TestTriggerGate:
    def test_weekly_ignores_the_5h_window_over_its_line(self, fleet):
        h = fleet(triggers="weekly")
        outcome = h.tick_with_usage({"1": _usage(95, 40), "2": _usage(0, 0)})
        assert outcome is TickOutcome.NO_ACTION
        assert h.active_number() == 1
        hold = next(e for e in h.events if isinstance(e, NoSwitchEvent))
        assert hold.reason == "trigger-off"
        assert "5h 95% >= 90%" in hold.detail and "weekly" in hold.detail

    def test_weekly_waits_out_an_exhausted_5h_window(self, fleet):
        h = fleet(triggers="weekly")
        assert h.tick_with_usage({"1": _usage(100, 40), "2": _usage(0, 0)}) is TickOutcome.NO_ACTION
        assert _no_switch_reasons(h) == ["trigger-off"]

    def test_weekly_switches_on_the_7d_window(self, fleet):
        h = fleet(triggers="weekly")
        assert h.tick_with_usage({"1": _usage(10, 95), "2": _usage(0, 0)}) is TickOutcome.SWITCHED
        assert h.active_number() == 2
        assert next(e for e in h.events if isinstance(e, SwitchEvent)).trigger == "proactive"

    def test_weekly_switches_on_the_7d_window_while_5h_is_over_too(self, fleet):
        h = fleet(triggers="weekly")
        assert h.tick_with_usage({"1": _usage(100, 96), "2": _usage(0, 0)}) is TickOutcome.SWITCHED
        assert next(e for e in h.events if isinstance(e, SwitchEvent)).trigger == "at-limit"

    def test_weekly_switches_on_a_named_model_window(self, fleet):
        h = fleet(triggers="weekly", model="Fable")
        outcome = h.tick_with_usage({
            "1": _usage(10, 40, Fable=97), "2": _usage(0, 0, Fable=0),
        })
        assert outcome is TickOutcome.SWITCHED

    def test_5h_only_ignores_the_weekly_window(self, fleet):
        h = fleet(triggers="5h")
        assert h.tick_with_usage({"1": _usage(10, 95), "2": _usage(0, 0)}) is TickOutcome.NO_ACTION
        assert _no_switch_reasons(h) == ["trigger-off"]

    def test_failover_needs_its_trigger(self, fleet):
        h = fleet(triggers="weekly", unhealthy_ticks=1)
        assert h.tick_with_usage({"1": None, "2": _usage(0, 0)}) is TickOutcome.NO_ACTION
        hold = next(e for e in h.events if isinstance(e, NoSwitchEvent))
        assert hold.reason == "trigger-off" and "failover" in hold.detail

    def test_failover_allowed(self, fleet):
        h = fleet(triggers="weekly,failover", unhealthy_ticks=1)
        assert h.tick_with_usage({"1": None, "2": _usage(0, 0)}) is TickOutcome.SWITCHED

    def test_consume_first_needs_its_trigger(self, fleet):
        h = fleet(triggers="weekly", strategy="consume-first")
        h.tick_with_usage({"1": _usage(10, 40), "2": _usage(0, 0)})
        assert _no_switch_reasons(h) == ["trigger-off"]

    def test_unset_keeps_every_trigger(self, fleet):
        h = fleet()
        assert h.tick_with_usage({"1": _usage(95, 40), "2": _usage(0, 0)}) is TickOutcome.SWITCHED

    def test_model_trigger_without_a_model_warns_once(self, fleet):
        h = fleet(triggers="model")
        h.tick_with_usage({"1": _usage(10, 10), "2": _usage(0, 0)})
        h.tick_with_usage({"1": _usage(10, 10), "2": _usage(0, 0)})
        warnings = [e for e in h.events if isinstance(e, ConfigWarningEvent)]
        assert len(warnings) == 1
        assert "autoswitch.model" in warnings[0].message


class TestAutoSwitchOffByDefault:
    def test_the_default_is_off(self):
        assert AutoSwitchSettings().enabled is False

    def test_a_live_engine_refuses_to_start(self, fleet):
        h = fleet()
        with pytest.raises(AutoSwitchDisabledError, match="autoswitch.enabled"):
            AutoSwitchEngine(h.switcher, AutoSwitchSettings(), h.events.append)

    def test_a_dry_run_may_start(self, fleet):
        h = fleet()
        engine = AutoSwitchEngine(h.switcher, AutoSwitchSettings(), h.events.append, dry_run=True)
        assert engine.dry_run is True

    def test_cswap_auto_says_how_to_turn_it_on(self, temp_home, capsys):
        with pytest.raises(SystemExit) as exited:
            cli._auto_command(["--once"])
        assert exited.value.code == 1
        assert "autoswitch.enabled true" in capsys.readouterr().err
