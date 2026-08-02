"""Centralizes every single-vs-dual-LED-panel branch in one place, so
every call site that used to talk to LEDPanel directly (ROI Select,
Calibration, Threshold Tuning, Live Session) instead calls
turn_all_leds_on/turn_all_leds_off/start_scanning/stop_scanning, passing
whatever `dual_panel_config` the operator's Device Select checkbox
resolved to.

`dual_panel_config` is `None` for the normal, single-panel case (every
camera/test that hasn't had the operator check "Use dual LED panel" on
Device Select) - every function below takes that exact same code path it
always has for `None`, so nothing changes for the common case. When it's a
dict (settings.yaml's `dual_panel:` section - panel_a_port, panel_b_port,
relay_port, relay_com_port, relay_pulse_duration_s), these functions
instead route through _run_on_both_panels: only ONE of 2 physically
separate LED panels is ever visible over USB at a time (they share one
Acroname hub), so a single LEDPanel.* call only ever reaches whichever
panel currently happens to be hub-exposed - reaching both means switching
hub ports, sending the command, switching again, sending it again.

_run_on_both_panels/_pulse_relay have NO automated tests - they need the
real Acroname `brainstem` SDK, a physically connected hub, and the actual
USB relay, same "no tests by design" bucket as engine/acroname_hub.py/
engine/led_panel.py. turn_all_leds_on/off/start_scanning/stop_scanning's
own BRANCHING (which path a given dual_panel_config takes) IS tested, by
mocking _run_on_both_panels/_pulse_relay/LEDPanel - see
tests/engine/test_dual_panel_control.py."""

import time

from engine.led_panel import LEDPanel


def turn_all_leds_on(dual_panel_config):
    if dual_panel_config is None:
        LEDPanel.stop()
        LEDPanel.all_leds_on()
    else:
        _run_on_both_panels(dual_panel_config, lambda: (LEDPanel.stop(), LEDPanel.all_leds_on()))


def turn_all_leds_off(dual_panel_config):
    if dual_panel_config is None:
        LEDPanel.all_leds_off()
    else:
        _run_on_both_panels(dual_panel_config, LEDPanel.all_leds_off)


def start_scanning(switch_time_ms, scan_direction, dual_panel_config):
    """The stepping/cycling mode Threshold Tuning + Live Session use. For
    the dual-panel case, this must run again in full - both panels
    reconfigured, relay re-pulsed - any time switch_time_ms/scan_direction
    change, since there's no way to update a single already-running panel
    live the way the single-panel case can (see
    gui/pages/threshold_tuning_page.py's _on_switch_time_changed)."""
    if dual_panel_config is None:
        LEDPanel.stop()
        LEDPanel.response_time_measurement_mode()
        LEDPanel.set_direction_single(scan_direction if scan_direction is not None else 1)
        LEDPanel.set_speed_ms(switch_time_ms)
        LEDPanel.start()
    else:
        def configure_one_panel():
            LEDPanel.stop()
            LEDPanel.response_time_measurement_mode()
            LEDPanel.set_direction_single(scan_direction if scan_direction is not None else 1)
            LEDPanel.set_speed_ms(switch_time_ms)
            LEDPanel.set_trigger_mode(2)
            LEDPanel.set_camera_trigger(True)

        _run_on_both_panels(dual_panel_config, configure_one_panel)
        _pulse_relay(dual_panel_config)


def stop_scanning(dual_panel_config):
    if dual_panel_config is None:
        LEDPanel.stop()
    else:
        _run_on_both_panels(dual_panel_config, LEDPanel.stop)


def _run_on_both_panels(dual_panel_config, action):
    # Imported here, not at module level - importing engine.acroname_hub
    # imports the real `brainstem` SDK inside AcronameHub.__init__, which
    # isn't installed on a machine with no Acroname hub - every other
    # single-panel test (the overwhelming majority) must be able to import
    # this whole module without that dependency being present at all.
    from engine.acroname_hub import AcronameHub

    hub = AcronameHub()
    if not hub.try_connect():
        raise RuntimeError("Failed to connect to the Acroname hub - check it's connected and powered.")
    try:
        panel_a_port = dual_panel_config["panel_a_port"]
        panel_b_port = dual_panel_config["panel_b_port"]
        relay_port = dual_panel_config["relay_port"]

        hub.enable_ports([panel_a_port], True, delay_in_seconds=0)
        hub.disable_ports([panel_b_port, relay_port])
        time.sleep(1)
        action()

        time.sleep(1)
        hub.enable_ports([panel_b_port, relay_port], True, delay_in_seconds=0)
        hub.disable_ports([panel_a_port])
        time.sleep(1)
        action()
    finally:
        hub.disconnect()


def _pulse_relay(dual_panel_config):
    # Imported here for the same reason _run_on_both_panels imports
    # AcronameHub lazily - pyserial only needs to be installed on a machine
    # that actually uses dual-panel mode.
    import serial

    com_port = dual_panel_config["relay_com_port"]
    pulse_duration_s = dual_panel_config["relay_pulse_duration_s"]
    s = serial.Serial(com_port, 9600, timeout=1)
    try:
        time.sleep(2)  # let the board finish reset after DTR toggle
        s.write(bytes.fromhex("A00101A2"))  # relay 1 ON - starts both panels stepping in lockstep
        time.sleep(pulse_duration_s)
        s.write(bytes.fromhex("A00100A1"))  # relay 1 OFF
    finally:
        s.close()
