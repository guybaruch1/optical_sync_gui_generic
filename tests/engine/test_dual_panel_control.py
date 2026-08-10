import threading
import time
from unittest.mock import patch, call

import pytest

import engine.dual_panel_control as dual_panel_control
from engine.dual_panel_control import (
    turn_all_leds_on, turn_all_leds_off, start_scanning, stop_scanning, switched_to_stream_panel,
    _relay_keepalive_loop,
)


# Matches the real rig this was built for: stream_a (IR) is port 1,
# stream_b (color) is port 0 - deliberately NOT 0/1 in stream order, so a
# test that silently assumed "a=0, b=1" would fail loudly.
DUAL_PANEL_CONFIG = {
    "stream_a_panel_port": 1, "stream_b_panel_port": 0, "relay_port": 6,
    "relay_com_port": "COM6", "hub_switch_settle_s": 3.0,
}


# --- dual_panel_config=None must take the EXACT same code path as before
# this feature existed - regression safety for every normal single-panel
# test, which never sets the Device Select checkbox. ---

def test_turn_all_leds_on_with_none_config_calls_ledpanel_directly():
    with patch("engine.dual_panel_control.LEDPanel") as mock_led_panel, \
         patch.object(dual_panel_control, "_run_on_both_panels") as mock_run_on_both:
        turn_all_leds_on(None)
        mock_led_panel.stop.assert_called_once()
        mock_led_panel.all_leds_on.assert_called_once()
        mock_run_on_both.assert_not_called()


def test_turn_all_leds_off_with_none_config_calls_ledpanel_directly():
    with patch("engine.dual_panel_control.LEDPanel") as mock_led_panel, \
         patch.object(dual_panel_control, "_run_on_both_panels") as mock_run_on_both:
        turn_all_leds_off(None)
        mock_led_panel.all_leds_off.assert_called_once()
        mock_run_on_both.assert_not_called()


def test_start_scanning_with_none_config_calls_ledpanel_directly_no_trigger_mode():
    with patch("engine.dual_panel_control.LEDPanel") as mock_led_panel, \
         patch.object(dual_panel_control, "_run_on_both_panels") as mock_run_on_both, \
         patch.object(dual_panel_control, "_relay_on") as mock_relay_on:
        start_scanning(5, 1, None)
        mock_led_panel.stop.assert_called_once()
        mock_led_panel.response_time_measurement_mode.assert_called_once()
        mock_led_panel.set_direction_single.assert_called_once_with(1)
        mock_led_panel.set_speed_ms.assert_called_once_with(5)
        mock_led_panel.start.assert_called_once()
        # The single-panel case has no concept of trigger mode at all.
        mock_led_panel.set_trigger_mode.assert_not_called()
        mock_led_panel.set_camera_trigger.assert_not_called()
        mock_run_on_both.assert_not_called()
        mock_relay_on.assert_not_called()


def test_start_scanning_defaults_scan_direction_to_1_when_none():
    with patch("engine.dual_panel_control.LEDPanel") as mock_led_panel, \
         patch.object(dual_panel_control, "_run_on_both_panels"), \
         patch.object(dual_panel_control, "_relay_on"):
        start_scanning(5, None, None)
        mock_led_panel.set_direction_single.assert_called_once_with(1)


def test_stop_scanning_with_none_config_calls_ledpanel_stop_directly():
    with patch("engine.dual_panel_control.LEDPanel") as mock_led_panel, \
         patch.object(dual_panel_control, "_run_on_both_panels") as mock_run_on_both:
        stop_scanning(None)
        mock_led_panel.stop.assert_called_once()
        mock_run_on_both.assert_not_called()


# --- A non-None dual_panel_config routes through _run_on_both_panels/
# _relay_on/_relay_off instead - these are mocked here (no real hardware);
# the actual Acroname-hub/relay mechanics are hardware-only, untested by
# design, same as engine/led_panel.py/engine/acroname_hub.py. ---

def test_turn_all_leds_on_with_dual_panel_config_routes_through_run_on_both_panels():
    with patch.object(dual_panel_control, "_run_on_both_panels") as mock_run_on_both:
        turn_all_leds_on(DUAL_PANEL_CONFIG)
        mock_run_on_both.assert_called_once()
        assert mock_run_on_both.call_args[0][0] == DUAL_PANEL_CONFIG


def test_turn_all_leds_off_with_dual_panel_config_routes_through_run_on_both_panels():
    with patch.object(dual_panel_control, "_run_on_both_panels") as mock_run_on_both:
        turn_all_leds_off(DUAL_PANEL_CONFIG)
        mock_run_on_both.assert_called_once_with(DUAL_PANEL_CONFIG, dual_panel_control.LEDPanel.all_leds_off)


def test_start_scanning_with_dual_panel_config_arms_twice_with_a_real_stop_between():
    # Confirmed on real hardware (tools/dual_panel_diag/
    # diag_double_arm_hypothesis.py): a SINGLE arm cycle - even one
    # immediately preceded by stop_scanning() - never gets the panel
    # stepping on its first arm after Calibration (isRunning stays '0'
    # despite getCameraTriggerState correctly flipping to 1 - the panel
    # sees the trigger edge but doesn't trust it yet). A SECOND, IDENTICAL
    # arm cycle - after a real stop_scanning() - steps every time. See
    # start_scanning's own comment for the full reasoning.
    call_order = []
    with patch("engine.dual_panel_control.LEDPanel") as mock_led_panel, \
         patch.object(dual_panel_control, "stop_scanning",
                      side_effect=lambda cfg: call_order.append("stop_scanning")) as mock_stop_scanning, \
         patch.object(dual_panel_control, "_run_on_both_panels",
                      side_effect=lambda cfg, action: call_order.append("_run_on_both_panels")) as mock_run_on_both, \
         patch.object(dual_panel_control, "_relay_on",
                      side_effect=lambda cfg: call_order.append("_relay_on")) as mock_relay_on:
        start_scanning(5, 1, DUAL_PANEL_CONFIG)

        # Two full arm cycles, ONE stop_scanning() between them - not
        # before-then-once, which is what the previous (failed) fix did.
        assert mock_run_on_both.call_count == 2
        assert mock_relay_on.call_count == 2
        mock_stop_scanning.assert_called_once_with(DUAL_PANEL_CONFIG)
        assert call_order == ["_run_on_both_panels", "_relay_on", "stop_scanning",
                               "_run_on_both_panels", "_relay_on"]

        for call in mock_run_on_both.call_args_list:
            assert call[0][0] == DUAL_PANEL_CONFIG
        for call in mock_relay_on.call_args_list:
            assert call[0][0] == DUAL_PANEL_CONFIG

        # Actually invoke the action callback _run_on_both_panels was given
        # (both calls pass the SAME configure_one_panel closure), to
        # confirm it sends reset() first (a cheap "known starting
        # position" step), then the plain docs/config_tigger_mode.bat
        # sequence - set_mode/set_speed_ms/set_trigger_mode/
        # set_camera_trigger, nothing more. A long investigation piled a lot
        # of extra complexity onto this (start() in 3 positions, forced
        # transitions on set_camera_trigger/set_trigger_mode) chasing a bug
        # that turned out to live entirely in stop_scanning()'s own
        # LEDPanel.stop() call instead - see stop_scanning's own test/
        # comment. No --stop (response_time_measurement_mode()/stop() bake
        # one in, confirmed via real-hardware testing to break trigger-mode
        # stepping) and no set_direction_single (absent from the
        # confirmed-working reference sequence too).
        action = mock_run_on_both.call_args[0][1]
        action()
        mock_led_panel.reset.assert_called_once()
        mock_led_panel.stop.assert_not_called()
        mock_led_panel.response_time_measurement_mode.assert_not_called()
        mock_led_panel.set_direction_single.assert_not_called()
        mock_led_panel.start.assert_not_called()
        mock_led_panel.set_mode.assert_called_once_with(1)
        mock_led_panel.set_speed_ms.assert_called_once_with(5)
        mock_led_panel.set_trigger_mode.assert_called_once_with(2)
        mock_led_panel.set_camera_trigger.assert_called_once_with(True)


def test_start_scanning_with_none_config_does_not_call_stop_scanning_again():
    # The single-panel case already calls LEDPanel.stop() unconditionally
    # at the top of its own branch, regardless of precondition - it must
    # NOT also route through the dual-panel stop_scanning() automation.
    with patch("engine.dual_panel_control.LEDPanel"), \
         patch.object(dual_panel_control, "stop_scanning") as mock_stop_scanning, \
         patch.object(dual_panel_control, "_run_on_both_panels"), \
         patch.object(dual_panel_control, "_relay_on"):
        start_scanning(5, 1, None)
        mock_stop_scanning.assert_not_called()


def test_stop_scanning_with_dual_panel_config_releases_relay_before_touching_hub_again():
    call_order = []
    with patch.object(dual_panel_control, "_run_on_both_panels",
                       side_effect=lambda *a: call_order.append("_run_on_both_panels")) as mock_run_on_both, \
         patch.object(dual_panel_control, "_relay_off",
                       side_effect=lambda: call_order.append("_relay_off")) as mock_relay_off:
        stop_scanning(DUAL_PANEL_CONFIG)
        # LEDPanel.reset(), NOT LEDPanel.stop() - --stop was confirmed (via
        # tools/dual_panel_diag/diag_arm_sequence_sweep.py's exhaustive testing of the
        # arm-sequence side) to be the actual root cause of the "only steps
        # once, or after an interrupted run" bug: it sets some internal
        # panel state nothing in start_scanning's arm sequence can undo.
        # The relay release (right below) is what actually freezes both
        # panels in place - a documented gate, not a one-shot pulse - so
        # --stop's own "stop" behavior was always redundant here; reset()
        # still returns the LEDs to a clean starting position without the
        # poisoning.
        mock_run_on_both.assert_called_once_with(DUAL_PANEL_CONFIG, dual_panel_control.LEDPanel.reset)
        mock_relay_off.assert_called_once()
        # _relay_off() MUST run before _run_on_both_panels - that function's
        # own hub-port-switching dance disables relay_port while switching
        # to panel A first, which would yank the USB device backing an
        # already-open relay connection out from under it (a real hardware
        # failure: "WriteFile failed - Access is denied" on the now-stale
        # handle) if it ran first.
        assert call_order == ["_relay_off", "_run_on_both_panels"]


def test_run_on_both_panels_switches_hub_ports_and_calls_action_twice():
    fake_hub = None

    class FakeHub:
        def __init__(self):
            self.calls = []

        def try_connect(self):
            self.calls.append("try_connect")
            return True

        def enable_ports(self, ports, disable_other_ports, delay_in_seconds):
            self.calls.append(("enable", list(ports), disable_other_ports))

        def disable_ports(self, ports):
            self.calls.append(("disable", list(ports)))

        def disconnect(self):
            self.calls.append("disconnect")

    def fake_acroname_hub_module():
        module = type("module", (), {"AcronameHub": lambda: fake_hub})
        return module

    fake_hub = FakeHub()
    with patch.dict("sys.modules", {"engine.acroname_hub": fake_acroname_hub_module()}), \
         patch("time.sleep") as mock_sleep:
        action_calls = []
        dual_panel_control._run_on_both_panels(DUAL_PANEL_CONFIG, lambda: action_calls.append(1))

    assert action_calls == [1, 1]
    assert fake_hub.calls == [
        "try_connect",
        # disable_other_ports=False both times - the original demo script's
        # True would sweep-disable every OTHER port on the hub (e.g. the
        # camera, if it shares this hub), not just the 2 that should go off;
        # the very next disable_ports() call already narrowly targets those.
        # stream_a_panel_port=1 first, then stream_b_panel_port=0 - NOT
        # 0-then-1, confirming the fixture's real (reversed) port mapping
        # is actually being read, not an assumed 0/1 order.
        ("enable", [1], False), ("disable", [0, 6]),
        ("enable", [0, 6], False), ("disable", [1]),
        "disconnect",
    ]
    # Every settling sleep uses the configured hub_switch_settle_s, not a
    # hardcoded value - lets the operator tune it in settings.yaml alone.
    assert mock_sleep.call_args_list == [call(3.0)] * 3


def test_run_on_both_panels_raises_if_hub_connection_fails():
    class FakeHub:
        def try_connect(self):
            return False

    fake_hub = FakeHub()

    def fake_acroname_hub_module():
        return type("module", (), {"AcronameHub": lambda: fake_hub})

    with patch.dict("sys.modules", {"engine.acroname_hub": fake_acroname_hub_module()}):
        try:
            dual_panel_control._run_on_both_panels(DUAL_PANEL_CONFIG, lambda: None)
            assert False, "expected RuntimeError"
        except RuntimeError:
            pass


# --- switched_to_stream_panel: for callers (Calibration, ROI Select) that
# calibrate/capture ONE stream at a time rather than driving both panels
# together - switches to that stream's OWN panel once and stays there for
# the whole `with` block. ---

class _FakeHubForSwitch:
    def __init__(self):
        self.calls = []

    def try_connect(self):
        self.calls.append("try_connect")
        return True

    def enable_ports(self, ports, disable_other_ports, delay_in_seconds):
        self.calls.append(("enable", list(ports), disable_other_ports))

    def disable_ports(self, ports):
        self.calls.append(("disable", list(ports)))

    def disconnect(self):
        self.calls.append("disconnect")


def test_switched_to_stream_panel_is_a_noop_when_config_is_none():
    entered = []
    with switched_to_stream_panel(None, "stream_a"):
        entered.append(True)
    assert entered == [True]  # must not raise / must not touch any hub


def test_switched_to_stream_panel_switches_to_stream_as_own_port():
    fake_hub = _FakeHubForSwitch()

    def fake_acroname_hub_module():
        return type("module", (), {"AcronameHub": lambda: fake_hub})

    with patch.dict("sys.modules", {"engine.acroname_hub": fake_acroname_hub_module()}), \
         patch("engine.dual_panel_control.LEDPanel") as mock_led_panel, \
         patch("time.sleep") as mock_sleep:
        with switched_to_stream_panel(DUAL_PANEL_CONFIG, "stream_a"):
            pass

    # stream_a's own port is 1 (per the fixture's real, reversed mapping) -
    # enabled; stream_b's port (0) + the relay port (6) disabled; only ONE
    # hub switch for the whole block, not one per action inside it.
    assert fake_hub.calls == [
        "try_connect",
        ("enable", [1], False), ("disable", [0, 6]),
        "disconnect",
    ]
    mock_sleep.assert_called_once_with(3.0)
    mock_led_panel.reset.assert_called_once()


def test_switched_to_stream_panel_switches_to_stream_bs_own_port():
    fake_hub = _FakeHubForSwitch()

    def fake_acroname_hub_module():
        return type("module", (), {"AcronameHub": lambda: fake_hub})

    with patch.dict("sys.modules", {"engine.acroname_hub": fake_acroname_hub_module()}), \
         patch("engine.dual_panel_control.LEDPanel"), \
         patch("time.sleep"):
        with switched_to_stream_panel(DUAL_PANEL_CONFIG, "stream_b"):
            pass

    assert fake_hub.calls == [
        "try_connect",
        ("enable", [0], False), ("disable", [1, 6]),
        "disconnect",
    ]


def test_switched_to_stream_panel_only_switches_once_for_multiple_actions_inside():
    fake_hub = _FakeHubForSwitch()

    def fake_acroname_hub_module():
        return type("module", (), {"AcronameHub": lambda: fake_hub})

    with patch.dict("sys.modules", {"engine.acroname_hub": fake_acroname_hub_module()}), \
         patch("engine.dual_panel_control.LEDPanel"), \
         patch("time.sleep") as mock_sleep:
        with switched_to_stream_panel(DUAL_PANEL_CONFIG, "stream_a"):
            pass  # caller would issue several plain LEDPanel calls here in real use

    # Exactly one enable/disable pair and one settle sleep for the whole
    # block - the whole point is NOT re-switching per action inside it.
    assert fake_hub.calls.count(("enable", [1], False)) == 1
    assert mock_sleep.call_count == 1


def test_switched_to_stream_panel_disconnects_even_if_block_raises():
    fake_hub = _FakeHubForSwitch()

    def fake_acroname_hub_module():
        return type("module", (), {"AcronameHub": lambda: fake_hub})

    with patch.dict("sys.modules", {"engine.acroname_hub": fake_acroname_hub_module()}), \
         patch("engine.dual_panel_control.LEDPanel"), \
         patch("time.sleep"):
        try:
            with switched_to_stream_panel(DUAL_PANEL_CONFIG, "stream_a"):
                raise ValueError("boom")
        except ValueError:
            pass

    assert fake_hub.calls[-1] == "disconnect"  # cleanup still ran despite the raise


# --- Regression: the panel would fail to step on the very FIRST
# start_scanning() after Calibration/ROI Select specifically - both end
# their per-stream capture with LEDPanel.all_leds_off(), which internally
# sends LEDPanel.stop() (--stop), the exact command already identified as
# poisoning the panel's next arm (see start_scanning's own comment). Manually
# pressing Stop then Start again always cleared it, because stop_scanning's
# own LEDPanel.reset() call undoes that poisoning - switched_to_stream_panel
# now does the same reset() automatically before switching away, so
# Calibration/ROI Select's own LED panel cleanup can no longer poison the
# panel for whatever start_scanning() call comes after them. ---

def test_switched_to_stream_panel_resets_panel_before_disconnecting():
    fake_hub = _FakeHubForSwitch()

    def fake_acroname_hub_module():
        return type("module", (), {"AcronameHub": lambda: fake_hub})

    with patch.dict("sys.modules", {"engine.acroname_hub": fake_acroname_hub_module()}), \
         patch("engine.dual_panel_control.LEDPanel") as mock_led_panel, \
         patch("time.sleep"):
        with switched_to_stream_panel(DUAL_PANEL_CONFIG, "stream_a"):
            mock_led_panel.all_leds_off()  # what Calibration/ROI Select's own cleanup does

    mock_led_panel.reset.assert_called_once()
    # Reset happens while STILL hub-exposed on my_port - no extra hub
    # switch beyond the single enable/disable pair already asserted
    # elsewhere; disconnect is still the very last hub call.
    assert fake_hub.calls[-1] == "disconnect"


def test_switched_to_stream_panel_is_a_noop_when_config_is_none_does_not_reset():
    # The single-panel case never routes through here at all (no hub, no
    # panel of its own to un-poison) - start_scanning's own unconditional
    # LEDPanel.stop() at the top of its single-panel branch already handles
    # this case regardless of precondition.
    with patch("engine.dual_panel_control.LEDPanel") as mock_led_panel:
        with switched_to_stream_panel(None, "stream_a"):
            pass
    mock_led_panel.reset.assert_not_called()


def test_switched_to_stream_panel_reset_failure_does_not_mask_block_exception():
    fake_hub = _FakeHubForSwitch()

    def fake_acroname_hub_module():
        return type("module", (), {"AcronameHub": lambda: fake_hub})

    with patch.dict("sys.modules", {"engine.acroname_hub": fake_acroname_hub_module()}), \
         patch("engine.dual_panel_control.LEDPanel") as mock_led_panel, \
         patch("time.sleep"):
        mock_led_panel.reset.side_effect = RuntimeError("panel command failed")
        with pytest.raises(ValueError, match="boom"):
            with switched_to_stream_panel(DUAL_PANEL_CONFIG, "stream_a"):
                raise ValueError("boom")

    # Cleanup still completed despite reset() itself failing.
    assert fake_hub.calls[-1] == "disconnect"

    assert "disconnect" in fake_hub.calls


# --- _relay_keepalive_loop: the one piece of the relay-holding machinery
# that's genuinely testable without real hardware - a timed loop writing to
# whatever connection object it's given, using a real threading.Event so
# _relay_off()'s own stop-and-join behavior is exercised faithfully. ---

class _FakeRelayConnection:
    def __init__(self):
        self.writes = []

    def write(self, data):
        self.writes.append(data)


class _FailingRelayConnection:
    def write(self, data):
        raise OSError("simulated USB write failure")


class _FakeStopEvent:
    """Deterministic stand-in for threading.Event - wait() returns False
    (keep looping) for the first `false_count` calls, then True (stop) -
    avoids racing real wall-clock timing between two threads, which was
    flaky under load (a slow scheduler tick can make fewer iterations fit
    in a fixed real-time window than expected)."""

    def __init__(self, false_count):
        self.calls = 0
        self.false_count = false_count

    def wait(self, timeout):
        self.calls += 1
        return self.calls > self.false_count


def test_relay_keepalive_loop_writes_periodically_until_stopped():
    conn = _FakeRelayConnection()
    stop_event = _FakeStopEvent(false_count=3)

    _relay_keepalive_loop(conn, stop_event, interval_s=0.01)

    assert len(conn.writes) == 3
    assert all(w == bytes.fromhex("A00101A2") for w in conn.writes)


def test_relay_keepalive_loop_exits_immediately_if_already_stopped():
    conn = _FakeRelayConnection()
    stop_event = threading.Event()
    stop_event.set()

    _relay_keepalive_loop(conn, stop_event, interval_s=10.0)

    assert conn.writes == []


def test_relay_keepalive_loop_stops_silently_on_write_error():
    stop_event = threading.Event()

    # Must return without raising, even though every write() call fails -
    # an uncaught exception on this background thread would otherwise just
    # print a traceback and die silently anyway; this does the same without
    # the noise.
    _relay_keepalive_loop(_FailingRelayConnection(), stop_event, interval_s=0.01)
