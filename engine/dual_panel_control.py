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
    gui/pages/threshold_tuning_page.py's _on_switch_time_changed)."""
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
        # from actually stepping once triggered (presumably --stop resets
        # whatever internal state --setTriggerMode/--setCameraTrigger
        # establish), and the confirmed-working reference sequence
        # (docs/config_tigger_mode.bat) never sets direction either. Do not
        # add either of those back without re-confirming on real hardware
        # first.
        #
        # LEDPanel.reset() ("--reset": reset to starting position WITHOUT
        # stopping it - distinct from --stop, which does both) runs first,
        # unconditionally. Confirmed via real-hardware testing (isolating
        # stop_scanning()'s cleanup into its 3 separate pieces - releasing
        # the relay, toggling Acroname hub exposure to reach each panel,
        # and sending LEDPanel.stop() to each one - via tools/
        # diag_isolate_stop_scanning.py) that the relay release and hub
        # toggle are BOTH innocent on their own; only LEDPanel.stop()
        # (--stop) breaks the next run. reset() alone did NOT fix that (the
        # pattern persisted with it added), but is left in as a harmless,
        # cheap "known starting position" step - it did not regress the
        # working "interrupted run" case either.
        #
        # LEDPanel.start() ("--start": Start the LED Panel) was first tried
        # AFTER set_camera_trigger(True) (i.e. after already being placed
        # into External trigger mode), on the theory that it might restore
        # whatever internal "running" state a prior --stop clears -
        # confirmed via tools/diag_panel_query_state.py that this internal
        # state (LEDPanel.is_running(), queried via --isRunning) really is
        # the deciding factor: a run following one that completed normally
        # (--stop clears it to '0') never gets it back, since nothing in
        # this sequence used to set it; a run following one that was
        # interrupted before stop_scanning() ran (leaving isRunning='1'
        # from before) always worked, with getCurrentLED visibly changing
        # between queries - objective proof it was really stepping.
        #
        # That first position was reverted: calling --start once already
        # in External trigger mode made a panel begin stepping immediately
        # on its own internal clock, regardless of the trigger mode just
        # configured - since configure_one_panel runs separately per panel
        # (hub-switched, one at a time), panel A started the moment its own
        # --start ran, and panel B started later, whenever the hub got to
        # it - bypassing the shared relay pulse meant to start both at the
        # same instant in lockstep.
        #
        # Also tried BEFORE set_trigger_mode(2)/set_camera_trigger(True) -
        # also reverted: real-hardware testing (tools/diag_panel_query_state.py)
        # showed isRunning stayed '0' even with --start sent in this
        # position, so it provided no measurable benefit either way -
        # removed rather than kept as unproven complexity. See
        # _relay_on's own comment for the current hypothesis being tested
        # instead (a real relay transition, not --start, may be what's
        # actually needed to get isRunning set).
        def configure_one_panel():
            LEDPanel.reset()
            LEDPanel.set_mode(1)  # response-time-measurement mode, no preceding --stop
            LEDPanel.set_speed_ms(switch_time_ms)
            LEDPanel.set_trigger_mode(2)
            LEDPanel.set_camera_trigger(True)

        _run_on_both_panels(dual_panel_config, configure_one_panel)
        _relay_on(dual_panel_config)


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
        _relay_off()
        _run_on_both_panels(dual_panel_config, LEDPanel.stop)


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
    # Explicit OFF before ON - UNCONFIRMED as of this commit, real-hardware
    # testing needed. Per Image Engineering's own iQ-Trigger API docs (a
    # separate, purpose-built trigger box for driving devices like the LED
    # Panel - not what this rig uses, but its documented behavior is
    # edge/pulse-based: toggleState()/sequences, not a held-closed state),
    # the LED Panel's isRunning flag may be set by detecting a genuine
    # open->closed TRANSITION, not simply by the relay BEING closed. If the
    # relay was already physically closed from whatever ran before (e.g.
    # its own OFF write never landed, or _relay_off() above was a no-op as
    # described), sending ON again is a no-op edge-wise - no real
    # transition occurs, and isRunning never gets set. Sending OFF first
    # guarantees a real transition on the ON write below, regardless of
    # whatever state the relay actually started in.
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
