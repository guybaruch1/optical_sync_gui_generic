"""Diagnostic script - NOT part of the shipped app, no automated tests.

RESOLVED: this sweep's own results (11 of 12 variants showed no movement;
the only one that stepped free-ran the panel immediately, breaking
lockstep) are what proved no start_scanning-side fix exists at all - the
actual root cause was stop_scanning()'s own LEDPanel.stop() call, fixed by
switching it to LEDPanel.reset() instead (see engine/dual_panel_control.py's
stop_scanning comment, and CLAUDE.md's dual-panel section for the full
trail). Re-running this script TODAY will very likely show ALL 12
variants "STEPPED", including baseline_4cmd - NOT because any arm-sequence
hypothesis below became correct, but because the "force the broken
precondition" step (run_variant calling stop_scanning()) no longer
actually poisons anything, since that's exactly what got fixed. Kept as a
reusable sweep harness for isolating a similar issue in the future, not
because this investigation is still open.

Automates the single-variable "try one arm sequence, test on real
hardware, report back, try the next one" loop this whole investigation has
been doing by hand - every variant tried so far in
engine/dual_panel_control.py's start_scanning() (reset() alone, --start in
2 positions, forcing a real transition on set_camera_trigger/
set_trigger_mode, --start first) failed to make isRunning=1 or the panels
actually step, once run immediately after a normal stop_scanning()-
completed run. Rather than keep hand-editing start_scanning() and asking
for one more real-hardware round trip per idea, this sweeps a whole list
of VARIANTS in one unattended run and reports which (if any) actually
produce visible stepping - detected OBJECTIVELY via getCurrentLED changing
between two queries a few seconds apart (the same "objective proof of
stepping" signal used earlier in this investigation), not by watching the
panels.

Each variant is tested from the SAME broken precondition every time - a
real dual_panel_control.stop_scanning() call first (the exact function a
normal completed run already calls), so every variant faces the identical
"--stop was just sent" poisoning this bug is about, not a fresh/lucky
starting state.

None of this mutates engine/dual_panel_control.py - it calls the app's
real _run_on_both_panels/_relay_on/_relay_off directly (same pattern
tools/diag_app_sequence_minus_extras.py already established) with
throwaway configure functions defined only in this script, so a variant
that works can be copied into start_scanning() deliberately afterward,
rather than this script silently mutating shared code mid-sweep.

Takes roughly 45-90s per variant (hub-switch settle time dominates) - with
the variants below, expect on the order of 10-15 minutes total. Watching
the panels is NOT required (detection is automatic), but feel free to -
if one variant visibly steps, you can Ctrl+C once its own summary line
prints without waiting for the rest to finish.

Run from the repo root: python tools/diag_arm_sequence_sweep.py
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from settings import load_settings
from engine.led_panel import LEDPanel
import engine.dual_panel_control as dual_panel_control

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# How long to let a just-armed panel settle before the first getCurrentLED
# sample, and how long to wait between the first and second sample to give
# it a real chance to visibly move if it's actually stepping.
FIRST_SAMPLE_DELAY_S = 3.0
SECOND_SAMPLE_DELAY_S = 6.0


def _variant_baseline(switch_time_ms):
    # Exactly docs/config_tigger_mode.bat's 4 commands - the user's own
    # reference sequence, confirmed to ALSO fail post-stop (same as every
    # other variant), included here as the sweep's own baseline/control.
    LEDPanel.set_mode(1)
    LEDPanel.set_speed_ms(switch_time_ms)
    LEDPanel.set_trigger_mode(2)
    LEDPanel.set_camera_trigger(True)


def _variant_baseline_twice(switch_time_ms):
    for _ in range(2):
        LEDPanel.set_mode(1)
        LEDPanel.set_speed_ms(switch_time_ms)
        LEDPanel.set_trigger_mode(2)
        LEDPanel.set_camera_trigger(True)
        time.sleep(0.5)


def _variant_reset_then_baseline(switch_time_ms):
    LEDPanel.reset()
    _variant_baseline(switch_time_ms)


def _variant_reset_twice_then_baseline(switch_time_ms):
    LEDPanel.reset()
    LEDPanel.reset()
    _variant_baseline(switch_time_ms)


def _variant_camera_trigger_toggle(switch_time_ms):
    # Currently committed in start_scanning() as of this commit.
    LEDPanel.reset()
    LEDPanel.set_mode(1)
    LEDPanel.set_speed_ms(switch_time_ms)
    LEDPanel.set_camera_trigger(False)
    LEDPanel.set_trigger_mode(1)
    LEDPanel.set_trigger_mode(2)
    LEDPanel.set_camera_trigger(True)


def _variant_mode_toggle(switch_time_ms):
    # getMode has read '1' in every real-hardware capture so far, before
    # AND after every arm attempt - set_mode(1) may be just as much of a
    # same-value no-op as set_camera_trigger/set_trigger_mode turned out to
    # be. Untested until now.
    LEDPanel.reset()
    LEDPanel.set_mode(5)
    LEDPanel.set_mode(1)
    LEDPanel.set_speed_ms(switch_time_ms)
    LEDPanel.set_trigger_mode(2)
    LEDPanel.set_camera_trigger(True)


def _variant_full_toggle(switch_time_ms):
    # Kitchen sink - force a real transition on every single setting, not
    # just camera trigger/trigger mode.
    LEDPanel.reset()
    LEDPanel.set_mode(5)
    LEDPanel.set_mode(1)
    LEDPanel.set_speed_ms(switch_time_ms)
    LEDPanel.set_camera_trigger(False)
    LEDPanel.set_trigger_mode(1)
    LEDPanel.set_trigger_mode(2)
    LEDPanel.set_camera_trigger(True)


def _variant_start_first(switch_time_ms):
    # Currently committed in start_scanning() as of this commit.
    LEDPanel.start()
    LEDPanel.reset()
    LEDPanel.set_mode(1)
    LEDPanel.set_speed_ms(switch_time_ms)
    LEDPanel.set_camera_trigger(False)
    LEDPanel.set_trigger_mode(1)
    LEDPanel.set_trigger_mode(2)
    LEDPanel.set_camera_trigger(True)


def _variant_start_after(switch_time_ms):
    # Known from earlier manual real-hardware testing to make the panel
    # free-run immediately (breaking dual-panel lockstep) - kept here as a
    # POSITIVE CONTROL for this script's own detection logic: if this
    # variant ISN'T flagged as "stepped", the detection code itself is
    # broken, not the hardware, and every other result below is suspect.
    LEDPanel.reset()
    LEDPanel.set_mode(1)
    LEDPanel.set_speed_ms(switch_time_ms)
    LEDPanel.set_trigger_mode(2)
    LEDPanel.set_camera_trigger(True)
    LEDPanel.start()


def _variant_start_then_wait_then_baseline(switch_time_ms):
    LEDPanel.start()
    time.sleep(1.0)
    _variant_baseline(switch_time_ms)


def _variant_stop_transition_then_baseline(switch_time_ms):
    # Forces a genuine running->stopped transition immediately before
    # configuring - every OTHER variant's preceding poison-stop (see
    # run_variant) already leaves the panel stopped->stopped, which is
    # exactly the kind of same-value no-op already confirmed to matter for
    # the relay and for set_camera_trigger/set_trigger_mode. Untested
    # whether --stop itself has the same real-transition-vs-no-op split.
    LEDPanel.start()
    LEDPanel.stop()
    _variant_baseline(switch_time_ms)


def _variant_double_stop_then_baseline(switch_time_ms):
    LEDPanel.stop()
    _variant_baseline(switch_time_ms)


VARIANTS = [
    ("baseline_4cmd", _variant_baseline),
    ("baseline_4cmd_twice", _variant_baseline_twice),
    ("reset_then_baseline", _variant_reset_then_baseline),
    ("reset_twice_then_baseline", _variant_reset_twice_then_baseline),
    ("camera_trigger_toggle", _variant_camera_trigger_toggle),
    ("mode_toggle", _variant_mode_toggle),
    ("full_toggle", _variant_full_toggle),
    ("start_first", _variant_start_first),
    ("start_after", _variant_start_after),
    ("start_then_wait_then_baseline", _variant_start_then_wait_then_baseline),
    ("stop_transition_then_baseline", _variant_stop_transition_then_baseline),
    ("double_stop_then_baseline", _variant_double_stop_then_baseline),
]


def _sample_both_panels(dual_panel_config):
    """Hub-switches to each panel once and reads isRunning/getCurrentLED -
    cheap subset of tools/diag_panel_query_state.py's 8-field query, since
    this runs twice per variant and there are many variants."""
    from engine.acroname_hub import AcronameHub

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
        a_running, a_led = LEDPanel.is_running(), LEDPanel.get_current_led()

        hub.enable_ports([stream_b_port], False, delay_in_seconds=0)
        hub.disable_ports([stream_a_port])
        time.sleep(settle_s)
        b_running, b_led = LEDPanel.is_running(), LEDPanel.get_current_led()
    finally:
        hub.disconnect()
    return {"A": (a_running, a_led), "B": (b_running, b_led)}


def run_variant(name, configure_fn, dual_panel_config, switch_time_ms):
    print("\n=== Variant: {} ===".format(name))

    print("  Forcing the broken precondition (stop_scanning(), like a normal completed run)...")
    dual_panel_control.stop_scanning(dual_panel_config)

    print("  Configuring with this variant's sequence...")
    dual_panel_control._run_on_both_panels(dual_panel_config, lambda: configure_fn(switch_time_ms))

    print("  Closing relay...")
    dual_panel_control._relay_on(dual_panel_config)

    print("  Waiting {}s, then sampling isRunning/getCurrentLED...".format(FIRST_SAMPLE_DELAY_S))
    time.sleep(FIRST_SAMPLE_DELAY_S)
    sample1 = _sample_both_panels(dual_panel_config)

    print("  Waiting {}s more, then sampling again to check for movement...".format(SECOND_SAMPLE_DELAY_S))
    time.sleep(SECOND_SAMPLE_DELAY_S)
    sample2 = _sample_both_panels(dual_panel_config)

    print("  Releasing relay...")
    dual_panel_control._relay_off()

    stepped_a = sample1["A"][1] != sample2["A"][1]
    stepped_b = sample1["B"][1] != sample2["B"][1]
    result = {
        "name": name,
        "isRunning_A": sample1["A"][0], "isRunning_B": sample1["B"][0],
        "led_A_1": sample1["A"][1], "led_A_2": sample2["A"][1], "stepped_A": stepped_a,
        "led_B_1": sample1["B"][1], "led_B_2": sample2["B"][1], "stepped_B": stepped_b,
    }
    print("  Result: isRunning A={!r} B={!r}  currentLED A {!r}->{!r} (stepped={})  B {!r}->{!r} (stepped={})".format(
        result["isRunning_A"], result["isRunning_B"],
        result["led_A_1"], result["led_A_2"], stepped_a,
        result["led_B_1"], result["led_B_2"], stepped_b,
    ))
    return result


def main():
    settings = load_settings(os.path.join(REPO_ROOT, "settings.yaml"))
    dual_panel_config = settings["dual_panel"]
    switch_time_ms = 1

    print("Sweeping {} arm-sequence variants, ~45-90s each (~{}-{} min total). Each starts from a freshly "
          "forced 'stopped' state via stop_scanning(), matching a normal completed run.".format(
              len(VARIANTS), len(VARIANTS) * 45 // 60, len(VARIANTS) * 90 // 60))

    results = []
    try:
        for name, configure_fn in VARIANTS:
            results.append(run_variant(name, configure_fn, dual_panel_config, switch_time_ms))
    finally:
        print("\n=== Summary ===")
        for r in results:
            if r["stepped_A"] and r["stepped_B"]:
                verdict = "STEPPED (both panels)"
            elif r["stepped_A"] or r["stepped_B"]:
                verdict = "PARTIAL (one panel only - check isRunning/LED values above)"
            else:
                verdict = "no movement"
            print("  {:<32} {}".format(r["name"], verdict))

        print("\nCleaning up (stop_scanning)...")
        dual_panel_control.stop_scanning(dual_panel_config)
        print("Done. Report the Summary table above.")


if __name__ == "__main__":
    main()
