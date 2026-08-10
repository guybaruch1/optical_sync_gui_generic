"""Diagnostic script - NOT part of the shipped app, no automated tests.

Two fixes have now been tried on real hardware for "the LED panel doesn't
step on its first arm after Calibration in dual-panel mode" and NEITHER
worked:

  1. switched_to_stream_panel() calling LEDPanel.reset() before
     disconnecting from each panel's hub port.
  2. start_scanning()'s dual-panel branch calling stop_scanning() itself
     first (automating "press Stop then press Start").

Both were informed guesses based on the ORIGINAL "only steps once" bug's
fix (see CLAUDE.md's dual-panel section) - but that original fix was found
by tools/dual_panel_diag/diag_arm_sequence_sweep.py's exhaustive 12-variant
sweep, and that sweep ALWAYS forced its test precondition via
dual_panel_control.stop_scanning() itself (see that script's own
run_variant). Every variant it ever tested was validated against a panel
that had PREVIOUSLY been armed into response-time-measurement mode (mode 1,
trigger mode 2) and then stopped - NEVER against a panel that has never
been in mode 1 at all, which is what Calibration's own
all_leds_on()/all_leds_off() (modes 5 then 3) actually leaves behind. Both
fixes above were essentially guesses about how to cure THAT specific
transition without ever having swept it - and both guesses were wrong.

This script fixes that gap: it forces the ACTUAL real precondition -
engine.dual_panel_control.switched_to_stream_panel(), then
LEDPanel.stop(); LEDPanel.all_leds_on(); LEDPanel.all_leds_off(), for EACH
stream in turn - byte-for-byte what gui/pages/calibration_page.py's
_capture_on_off_for_stream() actually does (minus the camera capture
calls, which don't touch the LED panel at all) - INCLUDING whatever
switched_to_stream_panel's own current reset()-before-disconnect fix does,
since that's the real code path a live run will hit, not a hypothetical
"no fix at all" precondition.

It also queries getMode()/isRunning() right after forcing that precondition
(before trying anything) - genuinely new data: the previous investigation
noted getMode always read '1' before AND after every arm attempt it tried,
but that was under the OTHER precondition (already mode 1). Under THIS
precondition it may read something else entirely (5, or 3, or whatever
"off" actually leaves behind), which could itself be informative.

Then it sweeps several candidate arm-sequence variants (including the
CURRENT shipped start_scanning() as its own negative control, to first
confirm this script's precondition/detection actually reproduces the
reported failure objectively - if that doesn't show as "no movement", stop
and report that BEFORE trusting any other result below) - detecting actual
stepping the same objective way the original sweep did: getCurrentLED
changing between two samples a few seconds apart, not by watching the
panels.

None of this mutates engine/dual_panel_control.py - same pattern
diag_arm_sequence_sweep.py established: calls the app's real
switched_to_stream_panel/_run_on_both_panels/_relay_on/_relay_off directly,
with throwaway configure functions defined only in this script. A variant
that works can be copied into start_scanning() deliberately afterward.

Slower than the original sweep per variant - forcing the real precondition
now means a full 2-stream on/off cycle (2 hub switches with settle time,
each with a 0.5s brightness-settle sleep) BEFORE each variant's own
2-panel arm+sample cycle. Expect roughly 90-150s per variant; with the
variants below, budget 15-25 minutes total. Watching the panels is NOT
required (detection is automatic), but if one variant visibly steps, feel
free to Ctrl+C once its own summary line prints rather than waiting for
the rest.

Run from the repo root: python tools/dual_panel_diag/diag_first_arm_after_calibration.py
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
    turn - the ACTUAL precondition Threshold Tuning's first start_scanning()
    call faces on real hardware, which the original arm-sequence sweep
    never tested against."""
    for stream_name in ("stream_a", "stream_b"):
        with switched_to_stream_panel(dual_panel_config, stream_name):
            LEDPanel.stop()
            LEDPanel.all_leds_on()
            time.sleep(0.5)
            LEDPanel.all_leds_off()
            # switched_to_stream_panel's own finally block sends
            # LEDPanel.reset() here, right before this `with` exits and the
            # hub disconnects - included for free since we're calling the
            # real function, not reimplementing it.


def _query_mode_state(dual_panel_config):
    """Read-only getMode()/isRunning() on each panel right after the
    precondition above - new data the original investigation never had for
    THIS starting state."""
    result = {}
    for stream_name in ("stream_a", "stream_b"):
        with switched_to_stream_panel(dual_panel_config, stream_name):
            result[stream_name] = {"mode": LEDPanel.get_mode(), "is_running": LEDPanel.is_running()}
    return result


# --- Candidate arm sequences, tried from the REAL precondition above.
# The "current_shipped" entry in VARIANTS below (configure_fn=None) is
# handled specially in run_variant() - it calls the real, currently-
# committed dual_panel_control.start_scanning() directly instead of going
# through _run_on_both_panels/_relay_on like every other variant here, since
# that's the exact code path already shipped (including its own
# stop_scanning() call). It's a NEGATIVE CONTROL: this MUST show "no
# movement" below, matching the reported real-hardware failure - if it
# doesn't, this script's own precondition/detection is wrong, and every
# other result here is suspect. ---

def _variant_baseline_4cmd_no_reset(switch_time_ms):
    # Exactly docs/config_tigger_mode.bat's 4 commands, no reset() at all -
    # isolates whether reset() itself might be the problem from THIS
    # starting mode (as opposed to curing it, which is all it was ever
    # confirmed to do from the OTHER precondition).
    LEDPanel.set_mode(1)
    LEDPanel.set_speed_ms(switch_time_ms)
    LEDPanel.set_trigger_mode(2)
    LEDPanel.set_camera_trigger(True)


def _variant_reset_then_baseline(switch_time_ms):
    # What configure_one_panel() has always done - same as fix #1 alone,
    # without fix #2's stop_scanning() wrapping it. Isolates whether
    # stop_scanning()'s extra relay/hub round-trip made things WORSE.
    LEDPanel.reset()
    LEDPanel.set_mode(1)
    LEDPanel.set_speed_ms(switch_time_ms)
    LEDPanel.set_trigger_mode(2)
    LEDPanel.set_camera_trigger(True)


def _variant_mode1_before_reset(switch_time_ms):
    # Reversed order - forces a real mode transition INTO 1 (from whatever
    # Calibration left it in) BEFORE reset(), on the hypothesis that
    # reset()'s "starting position" semantics only mean something once
    # already in response-time-measurement mode.
    LEDPanel.set_mode(1)
    LEDPanel.reset()
    LEDPanel.set_speed_ms(switch_time_ms)
    LEDPanel.set_trigger_mode(2)
    LEDPanel.set_camera_trigger(True)


def _variant_mode1_reset_mode1(switch_time_ms):
    LEDPanel.set_mode(1)
    LEDPanel.reset()
    LEDPanel.set_mode(1)
    LEDPanel.set_speed_ms(switch_time_ms)
    LEDPanel.set_trigger_mode(2)
    LEDPanel.set_camera_trigger(True)


def _variant_settle_delay_then_baseline(switch_time_ms):
    # On the hypothesis the panel's firmware needs real time to settle
    # after leaving a static-display mode (5/3) before accepting new mode
    # commands cleanly - nothing in the real precondition/arm handoff
    # currently waits at all beyond switched_to_stream_panel's own fixed
    # hub_switch_settle_s (which is about USB/hub timing, not panel
    # firmware state).
    time.sleep(3.0)
    LEDPanel.reset()
    LEDPanel.set_mode(1)
    LEDPanel.set_speed_ms(switch_time_ms)
    LEDPanel.set_trigger_mode(2)
    LEDPanel.set_camera_trigger(True)


def _variant_explicit_stop_first(switch_time_ms):
    # Deliberately reintroduces a real --stop before arming - the
    # established "never send --stop here" rule was only ever confirmed
    # from the OTHER precondition (already mode 1). Worth one honest test
    # from THIS precondition even though it contradicts existing guidance -
    # this is a throwaway diagnostic sequence, not a change to shipped code.
    LEDPanel.stop()
    LEDPanel.set_mode(1)
    LEDPanel.set_speed_ms(switch_time_ms)
    LEDPanel.set_trigger_mode(2)
    LEDPanel.set_camera_trigger(True)


def _variant_double_configure(switch_time_ms):
    for _ in range(2):
        LEDPanel.reset()
        LEDPanel.set_mode(1)
        LEDPanel.set_speed_ms(switch_time_ms)
        LEDPanel.set_trigger_mode(2)
        LEDPanel.set_camera_trigger(True)
        time.sleep(0.5)


VARIANTS = [
    ("current_shipped_NEGATIVE_CONTROL", None),  # handled specially in run_variant
    ("baseline_4cmd_no_reset", _variant_baseline_4cmd_no_reset),
    ("reset_then_baseline", _variant_reset_then_baseline),
    ("mode1_before_reset", _variant_mode1_before_reset),
    ("mode1_reset_mode1", _variant_mode1_reset_mode1),
    ("settle_delay_then_baseline", _variant_settle_delay_then_baseline),
    ("explicit_stop_first", _variant_explicit_stop_first),
    ("double_configure", _variant_double_configure),
]


def _sample_both_panels(dual_panel_config):
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


def run_variant(name, configure_fn, dual_panel_config, switch_time_ms, scan_direction):
    print("\n=== Variant: {} ===".format(name))

    print("  Forcing the REAL precondition (Calibration's own on/off cycle, per stream)...")
    _force_real_calibration_precondition(dual_panel_config)

    mode_state = _query_mode_state(dual_panel_config)
    print("  Panel state right after precondition: {}".format(mode_state))

    if configure_fn is None:
        print("  Running the CURRENT SHIPPED start_scanning() directly (negative control)...")
        dual_panel_control.start_scanning(switch_time_ms, scan_direction, dual_panel_config)
    else:
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

    print("  Releasing relay / cleaning up...")
    dual_panel_control.stop_scanning(dual_panel_config)

    stepped_a = sample1["A"][1] != sample2["A"][1]
    stepped_b = sample1["B"][1] != sample2["B"][1]
    result = {
        "name": name,
        "mode_state": mode_state,
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
    scan_direction = 1

    print("Sweeping {} arm-sequence variants from the REAL Calibration precondition, ~90-150s each "
          "(~{}-{} min total).".format(
              len(VARIANTS), len(VARIANTS) * 90 // 60, len(VARIANTS) * 150 // 60))
    print("First variant is a NEGATIVE CONTROL - it must show 'no movement' (matching the real bug) "
          "or this script's own precondition/detection is wrong and every later result is suspect.")

    results = []
    try:
        for name, configure_fn in VARIANTS:
            results.append(run_variant(name, configure_fn, dual_panel_config, switch_time_ms, scan_direction))
    finally:
        print("\n=== Summary ===")
        for r in results:
            if r["stepped_A"] and r["stepped_B"]:
                verdict = "STEPPED (both panels)"
            elif r["stepped_A"] or r["stepped_B"]:
                verdict = "PARTIAL (one panel only - check isRunning/LED values above)"
            else:
                verdict = "no movement"
            print("  {:<34} {}  [mode_state after precondition: {}]".format(r["name"], verdict, r["mode_state"]))

        print("\nCleaning up (stop_scanning)...")
        dual_panel_control.stop_scanning(dual_panel_config)
        print("Done. Report the Summary table above (including the negative control's result).")


if __name__ == "__main__":
    main()
