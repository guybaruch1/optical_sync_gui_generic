"""Diagnostic script - NOT part of the shipped app, no automated tests.

Calls engine.dual_panel_control.start_scanning(...) directly - the REAL,
current app function, imported from the actual codebase (not a copy) -
with dual_panel_config built straight from settings.yaml.

Purpose: confirm whether the app's actual integrated function reproduces
the "doesn't step" bug in complete isolation from the rest of the GUI/
camera-capture machinery. If tools/diag_baseline_demo_sequence.py DOES
make the panels step but THIS script doesn't, the difference is in
start_scanning's own sequence (see tools/diag_app_sequence_minus_extras.py
next, to isolate exactly which extra step matters).

Run from the repo root: python tools/diag_app_start_scanning.py
Watch the physical panels while it runs.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from settings import load_settings
from engine.dual_panel_control import start_scanning, stop_scanning


def main():
    settings = load_settings(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "settings.yaml"))
    dual_panel_config = settings["dual_panel"]
    switch_time_ms = 1
    scan_direction = settings["test"]["scan_direction"]

    print("Calling start_scanning(switch_time_ms={}, scan_direction={}, dual_panel_config={!r})".format(
        switch_time_ms, scan_direction, dual_panel_config
    ))
    start_scanning(switch_time_ms, scan_direction, dual_panel_config)
    print("start_scanning() returned - WATCH THE PANELS NOW. Should see one LED stepping continuously on each.")
    print("Waiting 10s before stopping...")
    time.sleep(10)

    print("Calling stop_scanning(dual_panel_config)...")
    stop_scanning(dual_panel_config)
    print("Done - report what you observed on the physical panels (did they step? how many positions, if any?).")


if __name__ == "__main__":
    main()
