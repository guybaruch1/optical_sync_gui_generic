"""LEDPanel control, forked from optical_sync_poc_/led_panel_cli.py.

The only change from the original is the logger: that file imports
`from utils.Log.Logger import get_test_logger`, a package that is not
installed anywhere on this machine (confirmed:
`python -c "from utils.Log.Logger import get_test_logger"` raises
ModuleNotFoundError). Swapped for the stdlib logging module - behavior
and the LED-Panel.exe CLI reference are otherwise unchanged. See
optical_sync_poc_/CLAUDE.md's "LEDPanel CLI reference" section for the
mode numbers and the all_leds_off-vs-stop distinction.
"""

import logging
import time
from subprocess import check_call, CalledProcessError, TimeoutExpired

_logger = logging.getLogger(__name__)


class LEDPanel:
    cmd_delay = 0.1
    exe_name = "LED-Panel.exe"
    # check_call has no timeout by default - a real-hardware USB hiccup
    # (seen as "USB communication failed" on stdout, usually followed by a
    # normal CalledProcessError that this class's own retry loop already
    # handles) can instead make LED-Panel.exe hang indefinitely waiting on
    # the device, blocking the whole process forever with nothing to
    # retry. Every real command observed so far completes near-instantly
    # (well under 1s) - anything past this is treated as hung, not slow.
    cmd_timeout_s = 5.0

    @staticmethod
    def _run(args):
        cmd = [LEDPanel.exe_name] + args.split()
        retries = 3
        _logger.info("Running cmd: %s", " ".join(cmd))
        last_error = None
        try:
            while retries > 0:
                try:
                    check_call(cmd, timeout=LEDPanel.cmd_timeout_s)
                    return
                except (CalledProcessError, FileNotFoundError, TimeoutExpired) as e:
                    last_error = e
                    retries -= 1
                    _logger.error("Command returned with an error: %s", e)
                    _logger.info("Retries left: %d", retries)
                    if retries > 0:
                        time.sleep(0.5)
            # Exhausted all retries - the original code returned silently here,
            # which let every caller (start(), set_speed_ms(), etc.) believe the
            # panel command succeeded when it never did. A live session could
            # then run to completion with the LED panel never actually scanning,
            # producing a run's worth of misleading data with no indication
            # anything went wrong - so this must raise instead.
            raise RuntimeError(
                "LEDPanel command failed after {} retries: {} ({})".format(
                    3, cmd, last_error
                )
            )
        finally:
            time.sleep(LEDPanel.cmd_delay)

    @staticmethod
    def _query(args):
        """Like _run, but for read-only --get*/--isRunning commands that
        print their answer to stdout instead of just succeeding/failing.

        LED-Panel.exe writes that answer via the low-level WriteConsole
        API, which produces NOTHING when stdout is redirected to a pipe
        or file - confirmed on real hardware: `LED-Panel.exe --isRunning`
        prints fine in a real terminal, but
        `LED-Panel.exe --isRunning > out.txt` leaves out.txt empty.
        subprocess.check_output can't capture this at all (it redirects
        to a pipe internally), so this instead runs the command with NO
        redirection at all (check_call, same as _run - it inherits
        THIS process's own real console) and reads the text straight out
        of the console's own screen buffer, at the cursor position where
        it landed.

        Only works when this process itself has a real, native Windows
        console attached (a plain cmd.exe/PowerShell.exe window) - some
        IDE-integrated "Run" consoles use their own pseudo-console instead
        of attaching a real one, which raises RuntimeError here with a
        message saying so rather than a cryptic pywintypes error.
        Returns the raw stripped text - deliberately not parsed into an
        int/bool, since the exact output format for each of these query
        commands hasn't been confirmed against real hardware yet;
        callers/diagnostic scripts print it as-is."""
        import win32console

        cmd = [LEDPanel.exe_name] + args.split()
        _logger.info("Querying cmd: %s", " ".join(cmd))

        try:
            stdout_handle = win32console.GetStdHandle(win32console.STD_OUTPUT_HANDLE)
            info_before = stdout_handle.GetConsoleScreenBufferInfo()
        except Exception as exc:
            raise RuntimeError(
                "LEDPanel._query needs a real, native Windows console attached to capture "
                "LED-Panel.exe's --get*/--isRunning output (it writes via WriteConsole, which "
                "produces nothing under redirection) - run this from a plain cmd.exe/"
                "PowerShell.exe window, not an IDE-integrated console: {}".format(exc)
            )
        cursor_before = info_before["CursorPosition"]
        buffer_width = info_before["Size"].X

        retries = 3
        last_error = None
        try:
            while retries > 0:
                try:
                    check_call(cmd, timeout=LEDPanel.cmd_timeout_s)
                    return LEDPanel._read_console_output(stdout_handle, cursor_before, buffer_width)
                except (CalledProcessError, FileNotFoundError, TimeoutExpired) as e:
                    last_error = e
                    retries -= 1
                    _logger.error("Query returned with an error: %s", e)
                    if retries > 0:
                        time.sleep(0.5)
            raise RuntimeError(
                "LEDPanel query failed after {} retries: {} ({})".format(3, cmd, last_error)
            )
        finally:
            time.sleep(LEDPanel.cmd_delay)

    @staticmethod
    def _read_console_output(stdout_handle, cursor_before, buffer_width):
        """Reads every screen-buffer row from cursor_before (where the
        cursor was right before the query command ran) through wherever
        the cursor ended up after it - i.e. exactly the text that command
        just printed, nothing that was already on screen beforehand."""
        info_after = stdout_handle.GetConsoleScreenBufferInfo()
        cursor_after = info_after["CursorPosition"]

        import win32console

        lines = []
        y = cursor_before.Y
        while y <= cursor_after.Y:
            text = stdout_handle.ReadConsoleOutputCharacter(buffer_width, win32console.PyCOORDType(0, y))
            lines.append(text.rstrip())
            y += 1
        return "\n".join(lines).strip()

    @staticmethod
    def all_leds_on():
        LEDPanel.stop()
        LEDPanel._run("--setMode 5")

    @staticmethod
    def rolling_shutter_mode():
        LEDPanel.stop()
        LEDPanel._run("--setMode 4")

    @staticmethod
    def response_time_measurement_mode():
        LEDPanel.stop()
        LEDPanel._run("--setMode 1")

    @staticmethod
    def set_mode(mode):
        # Unlike response_time_measurement_mode()/all_leds_on()/off()/
        # rolling_shutter_mode(), deliberately does NOT send --stop first.
        # Confirmed via real-hardware testing (see engine/
        # dual_panel_control.py's start_scanning): sending --stop before
        # --setMode 1 in the dual-panel trigger-mode sequence prevents the
        # panel from actually entering continuous stepping once triggered -
        # --stop presumably resets whatever internal state
        # --setTriggerMode/--setCameraTrigger need afterward. The
        # single-panel scanning path doesn't use trigger mode and is
        # unaffected - it still uses response_time_measurement_mode()'s
        # --stop-then---setMode-1 sequence as before.
        LEDPanel._run("--setMode {}".format(mode))

    @staticmethod
    def set_display_brightness(brightness):
        LEDPanel._run("--setDisplayBrightness {}".format(str(brightness)))

    @staticmethod
    def set_speed_ms(ms):
        secs = float(ms) / 1000
        LEDPanel._run("--setTime {:.4f}".format(secs))

    @staticmethod
    def start():
        LEDPanel._run("--start")

    @staticmethod
    def stop():
        LEDPanel._run("--stop")

    @staticmethod
    def reset():
        LEDPanel._run("--reset")

    @staticmethod
    def set_direction_single(mode):
        LEDPanel._run("--setDirectionSingle {}".format(mode))

    @staticmethod
    def set_trigger_mode(mode):
        # Slaves the panel's stepping to an external trigger signal (this
        # rig's shared USB relay - see engine/dual_panel_control.py - NOT
        # the RealSense camera; nothing in this codebase configures the
        # camera to emit a hardware trigger) instead of free-running on its
        # own internal timer. Layered on top of set_mode(1)/set_speed_ms,
        # not a replacement for them - needed when 2 physically separate
        # panels must stay in lockstep, since each free-running
        # independently on its own would drift.
        LEDPanel._run("--setTriggerMode {}".format(mode))

    @staticmethod
    def set_camera_trigger(enabled):
        # LED-Panel.exe --help documents --setCameraTrigger <bool> as
        # [0] Enable, [1] Disable - the reverse of the "1=on" convention
        # used here. Briefly "corrected" to 0-for-enabled to match that,
        # but real-hardware testing showed that change broke the reliable
        # "interrupt a run, the next one steps" workaround, which
        # 1-for-enabled had never affected - i.e. the documented mapping
        # doesn't match how this specific panel's firmware actually
        # behaves (a stale doc relative to the firmware version, most
        # likely). Reverted back to 1-for-enabled; don't "fix" this again
        # without confirming on real hardware first.
        LEDPanel._run("--setCameraTrigger {}".format(1 if enabled else 0))

    @staticmethod
    def set_stop_trigger(enabled):
        # Same [0]=Enable/[1]=Disable convention set_camera_trigger used to
        # follow, per LED-Panel.exe --help - UNVERIFIED against real
        # hardware (see set_camera_trigger's own comment: that documented
        # mapping turned out not to match this panel's actual firmware
        # behavior, reverted after real-hardware testing). Not currently
        # called from anywhere - exposed for diagnostics/future use
        # (tools/diag_panel_query_state.py checks its current state via
        # get_stop_trigger/get_stop_trigger_state). Confirm the real
        # polarity on hardware before wiring this into anything that matters.
        LEDPanel._run("--setStopTrigger {}".format(0 if enabled else 1))

    @staticmethod
    def is_running():
        """[0] not running, [1] running - per LED-Panel.exe --help."""
        return LEDPanel._query("--isRunning")

    @staticmethod
    def get_current_led():
        """Currently activated LED, [0-999] - e.g. 344 means LED 44 in a
        10x10 array's row 3 (per LED-Panel.exe --help)."""
        return LEDPanel._query("--getCurrentLED")

    @staticmethod
    def get_mode():
        return LEDPanel._query("--getMode")

    @staticmethod
    def get_trigger_mode():
        return LEDPanel._query("--getTriggerMode")

    @staticmethod
    def get_camera_trigger():
        return LEDPanel._query("--getCameraTrigger")

    @staticmethod
    def get_camera_trigger_state():
        """[0] Inactive, [1] Active - whether a camera-trigger pulse is
        CURRENTLY being seen, not whether the feature is enabled (that's
        get_camera_trigger) - per LED-Panel.exe --help."""
        return LEDPanel._query("--getCameraTriggerState")

    @staticmethod
    def get_stop_trigger():
        return LEDPanel._query("--getStopTrigger")

    @staticmethod
    def get_stop_trigger_state():
        """[0] Inactive, [1] Active - same distinction as
        get_camera_trigger_state, for the separate Stop Trigger signal."""
        return LEDPanel._query("--getStopTriggerState")

    @staticmethod
    def all_leds_off():
        LEDPanel.stop()
        LEDPanel._run("--setMode 3")
