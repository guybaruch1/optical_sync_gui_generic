from unittest.mock import patch, call

import engine.dual_panel_control as dual_panel_control
from engine.dual_panel_control import turn_all_leds_on, turn_all_leds_off, start_scanning, stop_scanning


DUAL_PANEL_CONFIG = {
    "panel_a_port": 0, "panel_b_port": 1, "relay_port": 6,
    "relay_com_port": "COM6", "relay_pulse_duration_s": 0.2, "hub_switch_settle_s": 3.0,
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
         patch.object(dual_panel_control, "_pulse_relay") as mock_pulse_relay:
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
        mock_pulse_relay.assert_not_called()


def test_start_scanning_defaults_scan_direction_to_1_when_none():
    with patch("engine.dual_panel_control.LEDPanel") as mock_led_panel, \
         patch.object(dual_panel_control, "_run_on_both_panels"), \
         patch.object(dual_panel_control, "_pulse_relay"):
        start_scanning(5, None, None)
        mock_led_panel.set_direction_single.assert_called_once_with(1)


def test_stop_scanning_with_none_config_calls_ledpanel_stop_directly():
    with patch("engine.dual_panel_control.LEDPanel") as mock_led_panel, \
         patch.object(dual_panel_control, "_run_on_both_panels") as mock_run_on_both:
        stop_scanning(None)
        mock_led_panel.stop.assert_called_once()
        mock_run_on_both.assert_not_called()


# --- A non-None dual_panel_config routes through _run_on_both_panels/
# _pulse_relay instead - these are mocked here (no real hardware); the
# actual Acroname-hub/relay mechanics are hardware-only, untested by
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


def test_start_scanning_with_dual_panel_config_configures_both_panels_and_pulses_relay():
    with patch("engine.dual_panel_control.LEDPanel") as mock_led_panel, \
         patch.object(dual_panel_control, "_run_on_both_panels") as mock_run_on_both, \
         patch.object(dual_panel_control, "_pulse_relay") as mock_pulse_relay:
        start_scanning(5, 1, DUAL_PANEL_CONFIG)

        mock_run_on_both.assert_called_once()
        assert mock_run_on_both.call_args[0][0] == DUAL_PANEL_CONFIG
        # Actually invoke the action callback _run_on_both_panels was given,
        # to confirm it configures trigger mode ON TOP of the same mode-1/
        # speed/direction setup the single-panel case uses, per stream.
        action = mock_run_on_both.call_args[0][1]
        action()
        mock_led_panel.stop.assert_called_once()
        mock_led_panel.response_time_measurement_mode.assert_called_once()
        mock_led_panel.set_direction_single.assert_called_once_with(1)
        mock_led_panel.set_speed_ms.assert_called_once_with(5)
        mock_led_panel.set_trigger_mode.assert_called_once_with(2)
        mock_led_panel.set_camera_trigger.assert_called_once_with(True)
        # .start() is never called for the dual-panel/trigger-mode case -
        # the relay pulse is what kicks off stepping, not --start.
        mock_led_panel.start.assert_not_called()

        mock_pulse_relay.assert_called_once_with(DUAL_PANEL_CONFIG)


def test_stop_scanning_with_dual_panel_config_routes_through_run_on_both_panels():
    with patch.object(dual_panel_control, "_run_on_both_panels") as mock_run_on_both:
        stop_scanning(DUAL_PANEL_CONFIG)
        mock_run_on_both.assert_called_once_with(DUAL_PANEL_CONFIG, dual_panel_control.LEDPanel.stop)


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
        ("enable", [0], False), ("disable", [1, 6]),
        ("enable", [1, 6], False), ("disable", [0]),
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
