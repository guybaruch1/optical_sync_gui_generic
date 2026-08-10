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
dict (settings.yaml's `dual_panel:` section - stream_a_panel_port,
stream_b_panel_port, relay_port, relay_com_port, hub_switch_settle_s),
these functions instead route through
_run_on_both_panels: only ONE of 2 physically separate LED panels is ever
visible over USB at a time (they share one Acroname hub), so a single
LEDPanel.* call only ever reaches whichever panel currently happens to be
hub-exposed - reaching both means switching hub ports, sending the
command, switching again, sending it again. Keyed explicitly by STREAM
(stream_a_panel_port/stream_b_panel_port), not by an arbitrary "first
panel"/"second panel" - on the actual rig this was built for, stream_a
(IR)'s panel is port 1 and stream_b (color)'s panel is port 0, not the
other way around, so getting this mapping right matters.

`switched_to_stream_panel(dual_panel_config, stream_name)` is a separate,
lower-level entry point for callers (Calibration, ROI Select) that need to
calibrate/capture ONE stream at a time rather than driving both panels in
lockstep - it switches to that stream's OWN panel once and stays there for
the whole `with` block, letting the caller issue several plain LEDPanel
calls (all_leds_on, capture, all_leds_off, capture, ...) without paying a
repeated hub-switch+settle cost per call. Unlike turn_all_leds_on/off
(which always touch both panels, for calls that genuinely need both
lit/dark together), this is for the "one stream's calibration doesn't
depend on the other stream's panel state at all" case.

_run_on_both_panels/switched_to_stream_panel/_relay_on/_relay_off have NO
automated tests - they need the real Acroname `brainstem` SDK, a physically
connected hub, and the actual USB relay, same "no tests by design" bucket
as engine/acroname_hub.py/engine/led_panel.py. turn_all_leds_on/off/
start_scanning/stop_scanning's own BRANCHING (which path a given
dual_panel_config takes) IS tested, by mocking
_run_on_both_panels/_relay_on/_relay_off/LEDPanel - see
tests/engine/test_dual_panel_control.py."""

import threading
import time
from contextlib import contextmanager

from engine.led_panel import LEDPanel

# Defense-in-depth against two overlapping start_scanning()/stop_scanning()
# calls both touching the relay's serial connection at once - observed on
# real hardware (via gui/pages/threshold_tuning_page.py's switch-time
# control, before it was fixed to only apply on an explicit Confirm click)
# as "WriteFile failed (PermissionError(13, 'Access is denied.', ...))":
# two calls' bodies interleaving via QApplication.processEvents() could
# both attempt to open the same COM port. The GUI-level fix (Confirm
# button, disabled for the duration of the call) removes the one known way
# to trigger this today, but this lock closes the underlying race for any
# FUTURE caller too, not just that one button. RLock, not a plain Lock -
# start_scanning's dual-panel branch calls stop_scanning() internally (the
# double-arm fix), so a non-reentrant lock would deadlock the exact code
# path this is meant to protect.
_dual_panel_lock = threading.RLock()

# Tracks whether the dual-panel pair has already been through one
# successful "priming" arm cycle since the last time switched_to_stream_panel
# touched them (Calibration/ROI Select's own all_leds_on()/all_leds_off()
# cycle - see switched_to_stream_panel's own comment for why THAT
# specifically de-primes them), plus which switch_time_ms/scan_direction
# they were last successfully configured with. start_scanning()'s
# double-arm sequence is only actually needed on the FIRST arm after that;
# once primed, a LATER call with the SAME switch_time_ms/scan_direction
# doesn't even need to reconfigure - configure_one_panel()'s own
# LEDPanel.reset() only resets LED POSITION, never mode/trigger config, so
# a panel already sitting in mode 1/trigger mode 2/camera-trigger-enabled
# from the last arm is still fully configured; the only thing that
# actually needs to happen is re-triggering the relay (a direct serial
# connection, not the Acroname hub - genuinely fast, no hub-switch settle
# time at all). Only a genuine switch_time/scan_direction CHANGE (or the
# first arm since Calibration) needs the hub-switching reconfigure step,
# which is what actually dominates start_scanning()'s wall-clock cost.
# Module-level, matching this file's own _relay_connection pattern - resets
# to False/None on a fresh process (safe default: always do the slow,
# confirmed-working double-arm+reconfigure the first time this app has no
# prior evidence either panel is primed or configured).
_dual_panel_primed = {"primed": False, "switch_time_ms": None, "scan_direction": None}


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
    reconfigured, relay re-closed - any time switch_time_ms/scan_direction
    change, since there's no way to update a single already-running panel
    live the way the single-panel case can (see
    gui/pages/threshold_tuning_page.py's _on_confirm_switch_time_clicked)."""
    if dual_panel_config is None:
        LEDPanel.stop()
        LEDPanel.response_time_measurement_mode()
        LEDPanel.set_direction_single(scan_direction if scan_direction is not None else 1)
        LEDPanel.set_speed_ms(switch_time_ms)
        LEDPanel.start()
    else:
        # Deliberately NOT LEDPanel.stop()/response_time_measurement_mode()
        # (which sends --stop before --setMode 1) and NOT
        # set_direction_single() - confirmed via real-hardware testing that
        # sending --stop before entering trigger mode prevents the panel
        # from actually stepping once triggered, and the confirmed-working
        # reference sequence (docs/config_tigger_mode.bat) never sets
        # direction either. Do not add either of those back without
        # re-confirming on real hardware first.
        #
        # This is deliberately the plain, minimal sequence -
        # docs/config_tigger_mode.bat's own 4 commands, plus reset() as a
        # cheap "known starting position" step. A long investigation (see
        # git history around tools/dual_panel_diag/diag_panel_query_state.py and
        # tools/dual_panel_diag/diag_arm_sequence_sweep.py for the full trail) piled a lot
        # more onto this - LEDPanel.start() in 3 different positions,
        # forcing a real transition on set_camera_trigger/set_trigger_mode,
        # forcing a real transition on the relay itself - all of it chasing
        # the actual bug: a run following one that completed NORMALLY never
        # stepped on its next arm, while a run following one that was
        # INTERRUPTED before stop_scanning() ran always did. An automated
        # sweep of 12 variants (tools/dual_panel_diag/diag_arm_sequence_sweep.py) confirmed
        # none of that arm-sequence complexity ever fixed it - the ONLY
        # variant that produced stepping was calling --start right after
        # entering External trigger mode, which free-runs the panel on its
        # own clock immediately, breaking lockstep between the 2 panels.
        #
        # The actual root cause was never in this function - it's
        # LEDPanel.stop() (--stop), which stop_scanning() used to send at
        # the end of every dual-panel run. See stop_scanning's own comment
        # for the fix. This function no longer needs to compensate for that
        # poisoning at all, so it's back to the plain sequence.
        def configure_one_panel():
            LEDPanel.reset()
            LEDPanel.set_mode(1)  # response-time-measurement mode, no preceding --stop
            LEDPanel.set_speed_ms(switch_time_ms)
            LEDPanel.set_trigger_mode(2)
            LEDPanel.set_camera_trigger(True)

        def _arm_once():
            _run_on_both_panels(dual_panel_config, configure_one_panel)
            _relay_on(dual_panel_config)

        # Arms TWICE, with a real stop_scanning() in between - confirmed on
        # real hardware (tools/dual_panel_diag/diag_double_arm_hypothesis.py)
        # to be what actually fixes the panel failing to step on its first
        # arm after Calibration/ROI Select. Two earlier fixes on this exact
        # bug both failed: a plain LEDPanel.reset() in switched_to_stream_panel,
        # then this same stop_scanning()-once-before-arming approach with
        # only a SINGLE arm cycle. The diagnostic script proved something
        # neither guess anticipated: a single arm cycle - even one
        # immediately preceded by stop_scanning() - NEVER gets the panel
        # stepping on the very first arm in a session (isRunning stays '0',
        # getCurrentLED never changes), even though getCameraTriggerState
        # correctly flips to 1 (the panel DOES see the relay's trigger edge
        # electrically - the earlier "only steps once" investigation found
        # this exact same signature). A SECOND, IDENTICAL arm cycle - after
        # a real stop_scanning() releases the relay and resets both panels -
        # steps EVERY time, with zero difference in command CONTENT between
        # the two attempts. So sequence content was never the variable that
        # mattered across all 20 single-shot variants tried in this
        # investigation (12 in the original sweep, 8 in the follow-up) -
        # the panel's own trigger-detection logic needs to see one full
        # relay close->open "priming" cycle before it trusts the next one.
        #
        # Only actually needed on the FIRST arm since Calibration/ROI
        # Select last touched the panels (see _dual_panel_primed's own
        # comment) - every LATER start_scanning() call in the same session
        # (switch_time changes, Continue to Live Test, Live Session's own
        # Start) already steps fine with a single arm, so this only pays
        # the extra hub-switch/relay round-trip once per Calibration run,
        # not on every single Start press - real-hardware testing confirmed
        # doing the double-arm unconditionally on every call made the
        # common case noticeably slower for no benefit. Also simplifies
        # gui/pages/threshold_tuning_page.py's _on_switch_time_changed,
        # which re-runs start_scanning() without an intervening
        # stop_scanning() today - correct by construction now instead of
        # only working via _relay_on()'s own stale-connection guard.
        #
        # Further optimization on top of that: once primed, a call with the
        # SAME switch_time_ms/scan_direction as last time doesn't even need
        # to reconfigure - only the relay actually needs re-triggering (see
        # _dual_panel_primed's own comment for why config persists). This is
        # the common repeat-Start case (clicking Start again with the same
        # settings) - skips BOTH panels' hub-switch entirely, which is what
        # actually dominates the wall-clock cost, not the handful of
        # near-instant LEDPanel CLI commands sent during it. Trade-off:
        # since configure_one_panel()'s own reset() is skipped too, the
        # LEDs resume stepping from wherever they last stopped rather than
        # restarting at position 0 - acceptable since nothing in this app
        # depends on a scan always starting from LED 0.
        with _dual_panel_lock:
            settings_unchanged = (
                _dual_panel_primed["switch_time_ms"] == switch_time_ms
                and _dual_panel_primed["scan_direction"] == scan_direction
            )
            if _dual_panel_primed["primed"] and settings_unchanged:
                _relay_on(dual_panel_config)
            elif _dual_panel_primed["primed"]:
                _arm_once()
            else:
                _arm_once()
                stop_scanning(dual_panel_config)
                _arm_once()
                _dual_panel_primed["primed"] = True
            _dual_panel_primed["switch_time_ms"] = switch_time_ms
            _dual_panel_primed["scan_direction"] = scan_direction


def stop_scanning(dual_panel_config):
    if dual_panel_config is None:
        LEDPanel.stop()
    else:
        # _relay_off() FIRST, before _run_on_both_panels touches the hub
        # again - _run_on_both_panels's own port-switching dance disables
        # relay_port while it switches to panel A first, which would yank
        # the USB device backing our already-open relay connection out from
        # under it (a real hardware failure: "WriteFile failed - Access is
        # denied" on the now-stale handle) if it ran before we release the
        # relay. relay_port is still in start_scanning's last-known-enabled
        # state here, untouched since the run began, so releasing it now is
        # safe.
        #
        # LEDPanel.reset() ("--reset": reset to starting position WITHOUT
        # stopping it), NOT LEDPanel.stop() ("--stop": stop AND reset to
        # starting position) - this was the actual root cause of the whole
        # "only steps once, or after an interrupted run" bug (see
        # start_scanning's own comment for the long trail that went into
        # confirming this). --stop sets some internal panel state that
        # nothing in start_scanning's own arm sequence can undo - relay
        # release is what actually freezes both panels in place (a
        # documented gate, not a one-shot pulse - see this module's own
        # docstring history), so --stop's extra "stop" behavior was always
        # redundant here anyway. reset() still returns the LEDs to a clean
        # starting position for the next run, without poisoning it.
        with _dual_panel_lock:
            _relay_off()
            _run_on_both_panels(dual_panel_config, LEDPanel.reset)


def _run_on_both_panels(dual_panel_config, action):
    hub = _connect_hub()
    try:
        stream_a_port = dual_panel_config["stream_a_panel_port"]
        stream_b_port = dual_panel_config["stream_b_panel_port"]
        relay_port = dual_panel_config["relay_port"]
        settle_s = dual_panel_config["hub_switch_settle_s"]

        # disable_other_ports=False - the ORIGINAL demo script this was
        # ported from passed True here, which (per AcronameHub.enable_ports)
        # disables EVERY downstream port on the hub not in the given list,
        # not just the other panel/relay port. That's fine in a standalone
        # demo script with nothing else attached, but on a real rig where
        # the camera (or anything else) might share this same hub, it would
        # silently cut that device's USB connection too. The explicit
        # disable_ports() calls right below already narrowly target exactly
        # the 2 ports that should go off - nothing outside
        # {stream_a_panel_port, stream_b_panel_port, relay_port} is ever
        # touched.
        hub.enable_ports([stream_a_port], False, delay_in_seconds=0)
        hub.disable_ports([stream_b_port, relay_port])
        time.sleep(settle_s)
        action()

        time.sleep(settle_s)
        hub.enable_ports([stream_b_port, relay_port], False, delay_in_seconds=0)
        hub.disable_ports([stream_a_port])
        time.sleep(settle_s)
        action()
    finally:
        hub.disconnect()


@contextmanager
def switched_to_stream_panel(dual_panel_config, stream_name):
    """For callers that calibrate/capture ONE stream at a time (Calibration,
    ROI Select) rather than driving both panels together - switches to
    whichever panel `stream_name` ("stream_a" or "stream_b") physically
    corresponds to ONCE, settles, and stays switched there for the whole
    `with` block, so the caller can issue several plain LEDPanel calls
    (all_leds_on, capture, all_leds_off, capture, ...) without repeating
    the hub-switch+settle cost per call - unlike turn_all_leds_on/off,
    which always reconfigure both panels because their callers genuinely
    need both lit/dark together.

    dual_panel_config=None: no-op, single-panel case - LEDPanel calls
    inside the block reach the one and only panel directly, same as
    always."""
    if dual_panel_config is None:
        yield
        return

    hub = _connect_hub()
    try:
        my_port = dual_panel_config["{}_panel_port".format(stream_name)]
        other_stream = "stream_b" if stream_name == "stream_a" else "stream_a"
        other_port = dual_panel_config["{}_panel_port".format(other_stream)]
        relay_port = dual_panel_config["relay_port"]

        hub.enable_ports([my_port], False, delay_in_seconds=0)
        hub.disable_ports([other_port, relay_port])
        time.sleep(dual_panel_config["hub_switch_settle_s"])
        yield
    finally:
        # Un-poison the panel before switching away, while it's still
        # hub-exposed on my_port (no extra hub switch needed). Both
        # Calibration's and ROI Select's own capture code (the only 2
        # callers of this context manager) end their per-stream block with
        # LEDPanel.all_leds_off() - which internally sends LEDPanel.stop()
        # (--stop) as its own first step. --stop sets internal panel state
        # that prevents the panel from actually stepping on its NEXT arm
        # via start_scanning - see start_scanning's own comment for the
        # full real-hardware-confirmed history of this exact failure mode.
        # That fix only ever covered stop_scanning's own explicit
        # LEDPanel.stop() call; it never covered THIS call site, which is
        # why - on real hardware - the panel would fail to step on the very
        # FIRST start_scanning() after Calibration specifically (whichever
        # stream's block ran last), even though every later
        # start_scanning/stop_scanning cycle within Threshold Tuning/Live
        # Session worked fine, and manually pressing Stop then Start again
        # (stop_scanning's own LEDPanel.reset()) always cleared it.
        # LEDPanel.reset() ("--reset": reset to starting position WITHOUT
        # stopping it) mirrors stop_scanning's own cure exactly. Best-effort
        # - swallows its own failure rather than masking whatever the
        # `with` block's body may have raised (a finally-block exception
        # always replaces one from the try in Python), same reasoning as
        # the callers' own all_leds_off() cleanup calls.
        try:
            LEDPanel.reset()
        except Exception:
            pass
        # This IS the de-priming action start_scanning's own comment refers
        # to - all_leds_on()/all_leds_off() (the caller's own capture code,
        # inside this `with` block) is what actually leaves the panel
        # needing a fresh double-arm next time; this is just the one
        # central place both callers (Calibration, ROI Select) route
        # through, so marking it here covers both without touching either
        # page. Deliberately unconditional (not wrapped in the try/except
        # above) - even if LEDPanel.reset() itself failed, the panel still
        # went through all_leds_on()/all_leds_off() inside this block, so
        # the next start_scanning() should still take the slow, safe path.
        # Clearing the tracked switch_time_ms/scan_direction too is not
        # strictly needed for correctness (primed=False alone already
        # forces the full path regardless), just avoids leaving stale
        # values sitting around.
        _dual_panel_primed["primed"] = False
        _dual_panel_primed["switch_time_ms"] = None
        _dual_panel_primed["scan_direction"] = None
        hub.disconnect()


def _connect_hub():
    # Imported here, not at module level - importing engine.acroname_hub
    # imports the real `brainstem` SDK inside AcronameHub.__init__, which
    # isn't installed on a machine with no Acroname hub - every other
    # single-panel test (the overwhelming majority) must be able to import
    # this whole module without that dependency being present at all.
    from engine.acroname_hub import AcronameHub

    hub = AcronameHub()
    if not hub.try_connect():
        raise RuntimeError("Failed to connect to the Acroname hub - check it's connected and powered.")
    return hub


# The relay is a GATE, not a one-shot start pulse: real-hardware testing
# confirmed both panels only keep stepping WHILE the relay stays closed
# (energized) - releasing it freezes them wherever they happen to be. An
# earlier version of this code treated it as a brief kickoff (~0.2s pulse
# then release), assuming that matched docs/acroname_hub.py's reference
# script's 100s hold only because that script's author was watching it by
# eye, not because 100s was itself load-bearing - that assumption was
# wrong; the hold time IS load-bearing, for as long as continuous stepping
# is wanted. start_scanning's dual-panel branch now closes the relay and
# leaves it closed; stop_scanning is what releases it. The open serial
# connection is kept at module level (rather than threading a handle back
# through every start_scanning/stop_scanning call site) since this
# module's functions are plain module-level functions, not a class
# instance every caller already carries around.
_relay_connection = {"conn": None, "keepalive_thread": None, "keepalive_stop": None}

# How often _relay_on's background thread re-sends the same "ON" byte on
# the already-open connection, in seconds - real-hardware testing showed a
# test that runs for several minutes with the relay held open but
# UNTOUCHED (no further writes between the initial ON and the final OFF)
# can fail on its NEXT run, while one interrupted early always works -
# consistent with Windows USB Selective Suspend power-managing the
# relay's USB-serial adapter into an idle state after some seconds with no
# traffic, then not cleanly recovering. Re-asserting ON periodically resets
# whatever idle timer is responsible, regardless of the exact mechanism -
# well under typical USB idle-suspend timeouts (usually tens of seconds to
# a couple of minutes).
_RELAY_KEEPALIVE_INTERVAL_S = 30.0


def _relay_keepalive_loop(conn, stop_event, interval_s=_RELAY_KEEPALIVE_INTERVAL_S):
    """Runs on its own background thread for as long as the relay stays
    armed - Event.wait() both sleeps AND doubles as the stop signal, so
    _relay_off() setting stop_event wakes this immediately rather than
    waiting out a full interval. Any write failure just ends the thread
    quietly (rather than crashing it with an unhandled exception in a
    background thread, or raising into whichever thread happens to be
    running at the time) - the next real interaction with the relay
    (_relay_off's own write, or the next run's _relay_on) is what actually
    surfaces a genuinely dead connection."""
    while not stop_event.wait(interval_s):
        try:
            conn.write(bytes.fromhex("A00101A2"))  # relay 1 ON - re-asserted, not a fresh trigger
        except Exception:
            return


def _relay_on(dual_panel_config):
    # Imported here for the same reason _connect_hub imports AcronameHub
    # lazily - pyserial only needs to be installed on a machine that
    # actually uses dual-panel mode.
    import serial

    # Closes any stale still-open connection first - e.g.
    # gui/pages/threshold_tuning_page.py's _on_switch_time_changed calls
    # start_scanning() again in full without an intervening stop_scanning()
    # whenever switch_time_ms changes mid-run. Only affects THIS process's
    # own tracked connection though - _relay_connection["conn"] is always
    # None in a fresh process, so this is a no-op on the very common case
    # of a brand new run, regardless of whatever physical state the relay
    # was actually left in by whatever ran before.
    _relay_off()
    com_port = dual_panel_config["relay_com_port"]
    s = serial.Serial(com_port, 9600, timeout=1)
    time.sleep(2)  # let the board finish reset after DTR toggle
    # Explicit OFF before ON - per Image Engineering's own iQ-Trigger API
    # docs (a separate, purpose-built trigger box for driving devices like
    # the LED Panel - not what this rig uses, but its documented behavior is
    # edge/pulse-based: toggleState()/sequences, not a held-closed state),
    # the LED Panel's isRunning flag may be set by detecting a genuine
    # open->closed TRANSITION, not simply by the relay BEING closed. Confirmed
    # via real-hardware testing (tools/dual_panel_diag/diag_panel_query_state.py) that this
    # guaranteed transition DOES reach the panel - getCameraTriggerState
    # flips 0->1 right after arming, proving the panel electrically detects
    # the pulse - but isRunning still stayed '0' and the panel still didn't
    # step. So this relay-edge guarantee is confirmed harmless/correct (and
    # kept - it rules out "the relay itself never produced a real edge" as a
    # variable) but is NOT sufficient on its own; the missing edge must be
    # elsewhere - see configure_one_panel's own comment for the next
    # hypothesis being tested (the panel's OWN config commands, not just the
    # relay, may need a forced transition too).
    s.write(bytes.fromhex("A00100A1"))  # relay 1 OFF - guarantee a known-open starting state
    time.sleep(0.2)  # let the board register the OFF state before flipping back on
    s.write(bytes.fromhex("A00101A2"))  # relay 1 ON - now a guaranteed real rising edge
    _relay_connection["conn"] = s

    stop_event = threading.Event()
    thread = threading.Thread(target=_relay_keepalive_loop, args=(s, stop_event), daemon=True)
    _relay_connection["keepalive_stop"] = stop_event
    _relay_connection["keepalive_thread"] = thread
    thread.start()


def _relay_off():
    s = _relay_connection["conn"]
    if s is None:
        return

    # Stop and join the keepalive thread BEFORE this thread touches the
    # connection itself - pyserial's Serial isn't documented as safe for
    # concurrent access from multiple threads, so the keepalive thread must
    # have fully finished (not mid-write) before the final OFF write/close.
    stop_event = _relay_connection["keepalive_stop"]
    if stop_event is not None:
        stop_event.set()
    thread = _relay_connection["keepalive_thread"]
    if thread is not None:
        thread.join(timeout=5.0)

    s.write(bytes.fromhex("A00100A1"))  # relay 1 OFF
    s.close()
    _relay_connection["conn"] = None
    _relay_connection["keepalive_stop"] = None
    _relay_connection["keepalive_thread"] = None
