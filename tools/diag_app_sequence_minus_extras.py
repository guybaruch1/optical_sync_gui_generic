"""Diagnostic script - NOT part of the shipped app, no automated tests.

Same hub-switching/relay plumbing as engine.dual_panel_control.start_scanning
(reuses its actual _run_on_both_panels/_pulse_relay directly - real code,
not a copy), but configures each panel with ONLY the 4 commands the
confirmed-working demo script (docs/config_tigger_mode.bat) sends -
response_time_measurement_mode, set_speed_ms, set_trigger_mode(2),
set_camera_trigger(True) - deliberately WITHOUT the 2 extra commands
engine.dual_panel_control.start_scanning's real configure_one_panel()
closure currently adds (a leading LEDPanel.stop(), and
LEDPanel.set_direction_single(scan_direction)) that aren't present in that
known-good script.

Purpose: isolate whether removing those two extra commands restores
stepping. If THIS script makes the panels step but
tools/diag_app_start_scanning.py doesn't, the fix is removing/reordering
those two calls in engine/dual_panel_control.py's start_scanning.

Run from the repo root: python tools/diag_app_sequence_minus_extras.py
Watch the physical panels while it runs.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from settings import load_settings
from engine.led_panel import LEDPanel
import engine.dual_panel_control as dual_panel_control


def main():
    settings = load_settings(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "settings.yaml"))
    dual_panel_config = settings["dual_panel"]
    switch_time_ms = 1

    def configure_one_panel_minimal():
        # Exactly the 4 commands docs/config_tigger_mode.bat sends, in the
        # same order - nothing else.
        LEDPanel.response_time_measurement_mode()
        LEDPanel.set_speed_ms(switch_time_ms)
        LEDPanel.set_trigger_mode(2)
        LEDPanel.set_camera_trigger(True)

    print("Running the minimal (4-command, no stop()/set_direction_single()) sequence "
          "through the app's real hub-switching plumbing...")
    dual_panel_control._run_on_both_panels(dual_panel_config, configure_one_panel_minimal)
    print("Both panels configured. Pulsing relay...")
    dual_panel_control._pulse_relay(dual_panel_config)
    print("Relay pulsed - WATCH THE PANELS NOW. Should see one LED stepping continuously on each.")
    print("Waiting 10s...")
    time.sleep(10)
    print("Done - report what you observed on the physical panels (did they step? how many positions, if any?).")


if __name__ == "__main__":
    main()
