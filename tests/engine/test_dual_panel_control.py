import threading
import time
from unittest.mock import patch, call

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


def test_start_scanning_with_dual_panel_config_configures_both_panels_and_closes_relay():
    with patch("engine.dual_panel_control.LEDPanel") as mock_led_panel, \
         patch.object(dual_panel_control, "_run_on_both_panels") as mock_run_on_both, \
         patch.object(dual_panel_control, "_relay_on") as mock_relay_on:
        start_scanning(5, 1, DUAL_PANEL_CONFIG)

        mock_run_on_both.assert_called_once()
        assert mock_run_on_both.call_args[0][0] == DUAL_PANEL_CONFIG
        # Actually invoke the action callback _run_on_both_panels was given,
        # to confirm it sends EXACTLY the 4-command sequence confirmed on
        # real hardware to produce continuous stepping once triggered - no
        # --stop (response_time_measurement_mode()/stop() bake one in,
        # confirmed via real-hardware testing to break trigger-mode
        # stepping) and no set_direction_single (absent from the
        # confirmed-working reference sequence too).
        action = mock_run_on_both.call_args[0][1]
        action()
        mock_led_panel.stop.assert_not_called()
        mock_led_panel.response_time_measurement_mode.assert_not_called()
        mock_led_panel.set_direction_single.assert_not_called()
        mock_led_panel.set_mode.assert_called_once_with(1)
        mock_led_panel.set_speed_ms.assert_called_once_with(5)
        mock_led_panel.set_trigger_mode.assert_called_once_with(2)
        mock_led_panel.set_camera_trigger.assert_called_once_with(True)
        # .start() is never called for the dual-panel/trigger-mode case -
        # closing the relay is what kicks off stepping, not --start.
        mock_led_panel.start.assert_not_called()

        mock_relay_on.assert_called_once_with(DUAL_PANEL_CONFIG)


def test_stop_scanning_with_dual_panel_config_releases_relay_before_touching_hub_again():
    call_order = []
    with patch.object(dual_panel_control, "_run_on_both_panels",
                       side_effect=lambda *a: call_order.append("_run_on_both_panels")) as mock_run_on_both, \
         patch.object(dual_panel_control, "_relay_off",
                       side_effect=lambda: call_order.append("_relay_off")) as mock_relay_off:
        stop_scanning(DUAL_PANEL_CONFIG)
        mock_run_on_both.assert_called_once_with(DUAL_PANEL_CONFIG, dual_panel_control.LEDPanel.stop)
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


def test_switched_to_stream_panel_switches_to_stream_bs_own_port():
    fake_hub = _FakeHubForSwitch()

    def fake_acroname_hub_module():
        return type("module", (), {"AcronameHub": lambda: fake_hub})

    with patch.dict("sys.modules", {"engine.acroname_hub": fake_acroname_hub_module()}), \
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
         patch("time.sleep"):
        try:
            with switched_to_stream_panel(DUAL_PANEL_CONFIG, "stream_a"):
                raise ValueError("boom")
        except ValueError:
            pass

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


def test_relay_keepalive_loop_writes_periodically_until_stopped():
    conn = _FakeRelayConnection()
    stop_event = threading.Event()

    def stop_after_a_few_intervals():
        time.sleep(0.05)
        stop_event.set()

    stopper = threading.Thread(target=stop_after_a_few_intervals)
    stopper.start()
    _relay_keepalive_loop(conn, stop_event, interval_s=0.01)
    stopper.join()

    assert len(conn.writes) >= 2
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
