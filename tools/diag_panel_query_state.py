"""Diagnostic script - NOT part of the shipped app, no automated tests.

Queries each panel's OWN reported state directly (via LED-Panel.exe's
read-only --isRunning/--getCurrentLED/--getMode/--getTriggerMode/
--getCameraTrigger/--getCameraTriggerState/--getStopTrigger/
--getStopTriggerState commands - see `LED-Panel.exe --help`), rather than
inferring it from external behavior (whether the LEDs visibly step or
not). Every one of these is read-only per the CLI's own documentation, so
querying should not itself disturb whatever state is being investigated.

Queries at three points:
  1. BEFORE doing anything - whatever was left over from a previous run
     (if this is a fresh process after an earlier one exited/crashed/was
     interrupted, this shows what state the panel's own firmware actually
     remembers, since its power is independent of USB/the host process).
  2. AFTER arming (engine.dual_panel_control.start_scanning()) and letting
     it settle - what "should be stepping" looks like.
  3. AFTER disarming (stop_scanning()) - what "cleanly stopped" looks like.

Run from the repo root: python tools/diag_panel_query_state.py
Compare the printed state across a run that visibly steps and one that
doesn't (letting each complete naturally) to see what actually differs -
this is real, direct evidence from the panel's own firmware instead of
another guess.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from settings import load_settings
from engine.led_panel import LEDPanel
import engine.dual_panel_control as dual_panel_control

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _query_one_panel(label):
    print("  {}: isRunning={!r} getCurrentLED={!r} getMode={!r} getTriggerMode={!r} "
          "getCameraTrigger={!r} getCameraTriggerState={!r} getStopTrigger={!r} "
          "getStopTriggerState={!r}".format(
              label,
              LEDPanel.is_running(),
              LEDPanel.get_current_led(),
              LEDPanel.get_mode(),
              LEDPanel.get_trigger_mode(),
              LEDPanel.get_camera_trigger(),
              LEDPanel.get_camera_trigger_state(),
              LEDPanel.get_stop_trigger(),
              LEDPanel.get_stop_trigger_state(),
          ))


def _query_both_panels(dual_panel_config, when):
    # Imported lazily, same reason engine/dual_panel_control.py's
    # _connect_hub does - the real `brainstem` SDK only needs to be
    # installed on a machine that actually runs this dual-panel-only tool.
    from engine.acroname_hub import AcronameHub

    hub = AcronameHub()
    if not hub.try_connect():
        print("Failed to connect to the Acroname hub for querying - skipping ({}).".format(when))
        return

    print("=== Panel state {} ===".format(when))
    try:
        stream_a_port = dual_panel_config["stream_a_panel_port"]
        stream_b_port = dual_panel_config["stream_b_panel_port"]
        settle_s = dual_panel_config["hub_switch_settle_s"]

        hub.enable_ports([stream_a_port], False, delay_in_seconds=0)
        hub.disable_ports([stream_b_port])
        time.sleep(settle_s)
        _query_one_panel("Panel A (stream_a_panel_port)")

        hub.enable_ports([stream_b_port], False, delay_in_seconds=0)
        hub.disable_ports([stream_a_port])
        time.sleep(settle_s)
        _query_one_panel("Panel B (stream_b_panel_port)")
    finally:
        hub.disconnect()


def main():
    settings = load_settings(os.path.join(REPO_ROOT, "settings.yaml"))
    dual_panel_config = settings["dual_panel"]
    switch_time_ms = settings["test"]["switch_time_ms"]
    scan_direction = settings["test"]["scan_direction"]

    _query_both_panels(dual_panel_config, "BEFORE doing anything (leftover from before, if any)")

    print("Arming dual-panel scanning (switch_time_ms={})...".format(switch_time_ms))
    dual_panel_control.start_scanning(switch_time_ms, scan_direction, dual_panel_config)
    print("Waiting 5s for it to settle...")
    time.sleep(5)
    _query_both_panels(dual_panel_config, "AFTER arming (should show stepping - watch the panels too)")

    print("Waiting 10s more (keep watching the panels)...")
    time.sleep(10)

    print("Disarming (stop_scanning)...")
    dual_panel_control.stop_scanning(dual_panel_config)
    _query_both_panels(dual_panel_config, "AFTER stop_scanning (cleanup)")

    print("Done - compare this printed state across a run where the LEDs visibly stepped and one where they didn't.")


if __name__ == "__main__":
    main()
