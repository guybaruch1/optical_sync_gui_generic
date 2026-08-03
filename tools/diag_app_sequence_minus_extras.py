"""Diagnostic script - NOT part of the shipped app, no automated tests.

RESOLVED (part 1): real-hardware testing confirmed one root cause was
LEDPanel.response_time_measurement_mode()'s hidden leading --stop call
(this script originally called that convenience method here too, which
meant it never actually tested what its own docstring claimed - a real
lesson in double-checking what a "no side effects" convenience wrapper
actually does). engine/dual_panel_control.py's start_scanning has since
been fixed to use the new LEDPanel.set_mode(1) (no preceding --stop)
instead.

RESOLVED (part 2): a second, separate root cause - the relay is a GATE,
not a one-shot start pulse. It must stay closed (energized) for as long as
continuous stepping is wanted; releasing it freezes both panels wherever
they happen to be. engine/dual_panel_control.py's _pulse_relay (which
closed it again after a brief relay_pulse_duration_s) has been replaced
with _relay_on (closes it, leaves it closed)/_relay_off (releases it).

This script is kept as a way to re-verify the exact 4-command sequence in
isolation from the rest of the app, using the app's real hub-switching
plumbing (_run_on_both_panels/_relay_on/_relay_off).

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
        # same order - nothing else. set_mode(1), NOT
        # response_time_measurement_mode() (which sends --stop first and
        # was confirmed via real-hardware testing to break trigger-mode
        # stepping).
        LEDPanel.set_mode(1)
        LEDPanel.set_speed_ms(switch_time_ms)
        LEDPanel.set_trigger_mode(2)
        LEDPanel.set_camera_trigger(True)

    print("Running the minimal (4-command, no stop()/set_direction_single()) sequence "
          "through the app's real hub-switching plumbing...")
    dual_panel_control._run_on_both_panels(dual_panel_config, configure_one_panel_minimal)
    print("Both panels configured. Closing relay (kept closed, not just pulsed)...")
    dual_panel_control._relay_on(dual_panel_config)
    print("Relay closed - WATCH THE PANELS NOW. Should see one LED stepping continuously on each.")
    print("Waiting 10s...")
    time.sleep(10)
    print("Releasing relay...")
    dual_panel_control._relay_off()
    print("Done - report what you observed on the physical panels (did they step? how many positions, if any?).")


if __name__ == "__main__":
    main()
