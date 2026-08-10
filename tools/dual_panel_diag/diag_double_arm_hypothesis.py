"""Diagnostic script - NOT part of the shipped app, no automated tests.

Two fixes for "the LED panel doesn't step on its first arm after
Calibration in dual-panel mode" have now been tried on real hardware and
NEITHER worked (see CLAUDE.md's dual-panel section for the full trail).
A follow-up sweep (tools/dual_panel_diag/diag_first_arm_after_calibration.py),
run from the ACTUAL real precondition this time (Calibration's own
all_leds_on()/all_leds_off() cycle, never mode 1), then tried 8 candidate
single-shot arm sequences from that precondition - including the current
shipped start_scanning() as a negative control. ALL 8 showed "no movement".
Raw data showed the exact signature this codebase's history already
documented for the ORIGINAL bug before its fix: isRunning reads '1' right
after the precondition (before any arm attempt), then '0' after EVERY
single arm attempt, with getCurrentLED never changing.

Across BOTH sweeps (12 old variants + 8 new variants = 20 single-shot
sequences total), the ONE thing never actually tested is genuinely arming
TWICE, with a real stop_scanning() in between, using the SAME command
sequence both times. The operator's own 100%-reliable manual fix - click
Start (fails), click Stop, click Start again (works) - inherently does
exactly this; every variant tried so far only ever attempted ONE arm per
freshly-forced precondition. If the panel needs to be armed once as a
"priming" step (regardless of which exact sequence) before a second,
identical attempt can succeed, that would explain why 20 different
single-shot sequences all failed uniformly - sequence CONTENT was never
the variable that mattered.

This script tests exactly that, with full 8-field state captured at 4
checkpoints (isRunning/getCurrentLED/getMode/getTriggerMode/
getCameraTrigger/getCameraTriggerState/getStopTrigger/getStopTriggerState -
the same fields tools/dual_panel_diag/diag_panel_query_state.py already
queries, reused here) so that if arm #2 succeeds where #1 (the IDENTICAL
code) didn't, the full state diff between them shows exactly what changed:

  1. After forcing the real Calibration precondition.
  2. After arm attempt #1 (the current shipped start_scanning(), unmodified).
  3. After a real stop_scanning() (between the two arm attempts).
  4. After arm attempt #2 (start_scanning() again - the identical call).

Each arm attempt is followed by an objective getCurrentLED-based stepping
check (two samples a few seconds apart), same detection method as both
previous sweeps.

Run from the repo root: .venv\\Scripts\\python.exe tools\\dual_panel_diag\\diag_double_arm_hypothesis.py
Takes roughly 3-5 minutes (one precondition force + two arm/sample cycles +
4 full 8-field queries, each needing its own hub switch(es)).
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from settings import load_settings
from engine.led_panel import LEDPanel
import engine.dual_panel_control as dual_panel_control
from engine.dual_panel_control import switched_to_stream_panel

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FIRST_SAMPLE_DELAY_S = 3.0
SECOND_SAMPLE_DELAY_S = 6.0


def _force_real_calibration_precondition(dual_panel_config):
    """Byte-for-byte what gui/pages/calibration_page.py's
    _capture_on_off_for_stream() does to the LED panel, for EACH stream in
    turn - same helper as diag_first_arm_after_calibration.py."""
    for stream_name in ("stream_a", "stream_b"):
        with switched_to_stream_panel(dual_panel_config, stream_name):
            LEDPanel.stop()
            LEDPanel.all_leds_on()
            time.sleep(0.5)
            LEDPanel.all_leds_off()
            # switched_to_stream_panel's own finally block sends
            # LEDPanel.reset() here, right before this `with` exits.


def _query_one_panel(label):
    return {
        "label": label,
        "isRunning": LEDPanel.is_running(),
        "getCurrentLED": LEDPanel.get_current_led(),
        "getMode": LEDPanel.get_mode(),
        "getTriggerMode": LEDPanel.get_trigger_mode(),
        "getCameraTrigger": LEDPanel.get_camera_trigger(),
        "getCameraTriggerState": LEDPanel.get_camera_trigger_state(),
        "getStopTrigger": LEDPanel.get_stop_trigger(),
        "getStopTriggerState": LEDPanel.get_stop_trigger_state(),
    }


def _query_both_panels(dual_panel_config, checkpoint_name):
    from engine.acroname_hub import AcronameHub

    hub = AcronameHub()
    if not hub.try_connect():
        print("  Failed to connect to the Acroname hub for querying - skipping ({}).".format(checkpoint_name))
        return None
    try:
        stream_a_port = dual_panel_config["stream_a_panel_port"]
        stream_b_port = dual_panel_config["stream_b_panel_port"]
        settle_s = dual_panel_config["hub_switch_settle_s"]

        hub.enable_ports([stream_a_port], False, delay_in_seconds=0)
        hub.disable_ports([stream_b_port])
        time.sleep(settle_s)
        panel_a = _query_one_panel("Panel A")

        hub.enable_ports([stream_b_port], False, delay_in_seconds=0)
        hub.disable_ports([stream_a_port])
        time.sleep(settle_s)
        panel_b = _query_one_panel("Panel B")
    finally:
        hub.disconnect()
    return {"checkpoint": checkpoint_name, "A": panel_a, "B": panel_b}


def _sample_current_led_twice(dual_panel_config):
    """Objective stepping check - same getCurrentLED-based detection both
    previous sweeps used, kept separate from the full 8-field query above
    since this one needs to run TWICE a few seconds apart."""
    from engine.acroname_hub import AcronameHub

    def _sample_once():
        hub = AcronameHub()
        if not hub.try_connect():
            raise RuntimeError("Failed to connect to the Acroname hub for sampling")
        try:
            stream_a_port = dual_panel_config["stream_a_panel_port"]
            stream_b_port = dual_panel_config["stream_b_panel_port"]
            settle_s = dual_panel_config["hub_switch_settle_s"]

            hub.enable_ports([stream_a_port], False, delay_in_seconds=0)
            hub.disable_ports([stream_b_port])
            time.sleep(settle_s)
            a_led = LEDPanel.get_current_led()

            hub.enable_ports([stream_b_port], False, delay_in_seconds=0)
            hub.disable_ports([stream_a_port])
            time.sleep(settle_s)
            b_led = LEDPanel.get_current_led()
        finally:
            hub.disconnect()
        return a_led, b_led

    print("  Waiting {}s, then sampling getCurrentLED...".format(FIRST_SAMPLE_DELAY_S))
    time.sleep(FIRST_SAMPLE_DELAY_S)
    led_a_1, led_b_1 = _sample_once()

    print("  Waiting {}s more, then sampling again to check for movement...".format(SECOND_SAMPLE_DELAY_S))
    time.sleep(SECOND_SAMPLE_DELAY_S)
    led_a_2, led_b_2 = _sample_once()

    stepped_a = led_a_1 != led_a_2
    stepped_b = led_b_1 != led_b_2
    print("  currentLED A {!r}->{!r} (stepped={})  B {!r}->{!r} (stepped={})".format(
        led_a_1, led_a_2, stepped_a, led_b_1, led_b_2, stepped_b,
    ))
    return {"led_a_1": led_a_1, "led_a_2": led_a_2, "stepped_a": stepped_a,
            "led_b_1": led_b_1, "led_b_2": led_b_2, "stepped_b": stepped_b}


def _print_checkpoint(checkpoint):
    if checkpoint is None:
        return
    print("=== {} ===".format(checkpoint["checkpoint"]))
    for key in ("A", "B"):
        panel = checkpoint[key]
        print("  {}: isRunning={!r} getCurrentLED={!r} getMode={!r} getTriggerMode={!r} "
              "getCameraTrigger={!r} getCameraTriggerState={!r} getStopTrigger={!r} "
              "getStopTriggerState={!r}".format(
                  panel["label"], panel["isRunning"], panel["getCurrentLED"], panel["getMode"],
                  panel["getTriggerMode"], panel["getCameraTrigger"], panel["getCameraTriggerState"],
                  panel["getStopTrigger"], panel["getStopTriggerState"],
              ))


def main():
    settings = load_settings(os.path.join(REPO_ROOT, "settings.yaml"))
    dual_panel_config = settings["dual_panel"]
    switch_time_ms = settings["test"]["switch_time_ms"]
    scan_direction = settings["test"]["scan_direction"]

    checkpoints = []
    verdicts = {}

    try:
        print("Forcing the REAL Calibration precondition (per-stream on/off cycle)...")
        _force_real_calibration_precondition(dual_panel_config)
        checkpoints.append(_query_both_panels(dual_panel_config, "1. After precondition"))

        print("\nArm attempt #1: calling the CURRENT SHIPPED start_scanning() (unmodified)...")
        dual_panel_control.start_scanning(switch_time_ms, scan_direction, dual_panel_config)
        checkpoints.append(_query_both_panels(dual_panel_config, "2. After arm attempt #1"))
        print("Sampling arm attempt #1 for stepping...")
        verdicts["arm_1"] = _sample_current_led_twice(dual_panel_config)

        print("\nCalling the real stop_scanning() (between the two arm attempts)...")
        dual_panel_control.stop_scanning(dual_panel_config)
        checkpoints.append(_query_both_panels(dual_panel_config, "3. After stop_scanning (between)"))

        print("\nArm attempt #2: calling start_scanning() again - IDENTICAL call to attempt #1...")
        dual_panel_control.start_scanning(switch_time_ms, scan_direction, dual_panel_config)
        checkpoints.append(_query_both_panels(dual_panel_config, "4. After arm attempt #2"))
        print("Sampling arm attempt #2 for stepping...")
        verdicts["arm_2"] = _sample_current_led_twice(dual_panel_config)

    finally:
        print("\nCleaning up (stop_scanning)...")
        dual_panel_control.stop_scanning(dual_panel_config)

        print("\n=== All checkpoints (side-by-side) ===")
        for checkpoint in checkpoints:
            _print_checkpoint(checkpoint)

        print("\n=== Verdict ===")
        for label, key in (("Arm attempt #1", "arm_1"), ("Arm attempt #2", "arm_2")):
            v = verdicts.get(key)
            if v is None:
                print("  {}: did not complete".format(label))
                continue
            if v["stepped_a"] and v["stepped_b"]:
                verdict = "STEPPED (both panels)"
            elif v["stepped_a"] or v["stepped_b"]:
                verdict = "PARTIAL (one panel only)"
            else:
                verdict = "no movement"
            print("  {}: {}".format(label, verdict))

        print("\nDone. Report the checkpoints and verdict above.")


if __name__ == "__main__":
    main()
