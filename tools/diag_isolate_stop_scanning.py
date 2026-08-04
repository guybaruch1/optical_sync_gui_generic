"""Diagnostic script - NOT part of the shipped app, no automated tests.

RESOLVED: this script is what originally isolated the root cause of the
"only steps once, or after an interrupted run" bug - comparing its
"relay_only"/"toggle_only"/"full" variants confirmed that releasing the
relay and toggling Acroname hub port-exposure are BOTH innocent on their
own, and only sending LEDPanel.stop() (--stop) to each panel breaks the
next run. engine/dual_panel_control.py's stop_scanning() has since been
fixed to call LEDPanel.reset() instead for the dual-panel case - see its
own comment for the fix and CLAUDE.md's dual-panel section for the full
trail (real-hardware testing via tools/diag_panel_query_state.py/
tools/diag_arm_sequence_sweep.py that ruled out every start_scanning-side
fix first). The "full" variant below now exercises the FIXED
stop_scanning() (LEDPanel.reset(), not .stop()) - kept as a way to
re-isolate a similar issue in the future, not because this one is still
open.

Isolates WHICH part of dual-panel stop_scanning()'s cleanup is
responsible for a given behavior on the NEXT run - these always happen
together in the real stop_scanning(), so this has never been tested in
isolation:

  1. Releasing the relay (_relay_off()).
  2. The Acroname hub port-toggling _run_on_both_panels does to reach
     BOTH panels one at a time (switch hub-exposure to panel A, then to
     panel B, then disconnect) - a side effect of needing to individually
     address each panel over USB, since only one is hub-exposed at a time.
  3. Sending LEDPanel.reset() to each panel once it's reached (was
     LEDPanel.stop() before the fix above).

HOW TO USE: set VARIANT below, run this script (it arms+steps for 10s,
runs just that one cleanup variant, then exits), then immediately run
this script AGAIN (or tools/diag_app_start_scanning.py) and watch whether
THAT next run steps or not. Repeat with a different VARIANT to compare.

Run from the repo root: python tools/diag_isolate_stop_scanning.py
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from settings import load_settings
from engine.led_panel import LEDPanel
import engine.dual_panel_control as dual_panel_control

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Which cleanup behavior to run after arming+stepping - see module
# docstring for what each one isolates.
#
#   "none"        - do nothing at all. Relay stays closed, hub ports stay
#                   exactly as arming left them - closest to what an
#                   interrupted run leaves behind, but via a clean exit
#                   instead of a real Ctrl+C (sanity check: does a clean
#                   exit with zero cleanup behave the same as an actual
#                   interrupt?).
#   "relay_only"  - release the relay (_relay_off()) - nothing else. No
#                   hub connection, no panel command at all.
#   "toggle_only" - release the relay, then toggle Acroname hub exposure
#                   to panel A then panel B (the exact same switching
#                   _run_on_both_panels always does) - but with NO
#                   LEDPanel command sent to either one while exposed.
#   "full"        - the real stop_scanning() (relay release + hub toggle
#                   + LEDPanel.reset() on each panel) - current real
#                   behavior, for comparison against the others.
VARIANT = "toggle_only"


def main():
    settings = load_settings(os.path.join(REPO_ROOT, "settings.yaml"))
    dual_panel_config = settings["dual_panel"]
    switch_time_ms = settings["test"]["switch_time_ms"]
    scan_direction = settings["test"]["scan_direction"]

    print("VARIANT = {!r}".format(VARIANT))
    print("Arming dual-panel scanning...")
    dual_panel_control.start_scanning(switch_time_ms, scan_direction, dual_panel_config)
    print("Watch the panels - should be stepping now. Waiting 10s...")
    time.sleep(10)

    print("Running cleanup variant {!r}...".format(VARIANT))
    if VARIANT == "none":
        print("Doing nothing at all - panels stay armed/stepping, relay stays closed.")
    elif VARIANT == "relay_only":
        dual_panel_control._relay_off()
        print("Relay released - panels should freeze in place now. Hub/panels untouched.")
    elif VARIANT == "toggle_only":
        dual_panel_control._relay_off()
        dual_panel_control._run_on_both_panels(dual_panel_config, lambda: None)
        print("Relay released, hub toggled to each panel and back - no panel command sent.")
    elif VARIANT == "full":
        dual_panel_control.stop_scanning(dual_panel_config)
        print("Full stop_scanning() ran (relay release + hub toggle + LEDPanel.reset() on each).")
    else:
        raise ValueError("Unknown VARIANT {!r}".format(VARIANT))

    print(
        "Done - now run this script again (same or a different VARIANT) or "
        "tools/diag_app_start_scanning.py, and report whether THAT next run steps."
    )


if __name__ == "__main__":
    main()
