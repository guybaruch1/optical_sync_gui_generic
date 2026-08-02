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
from subprocess import check_call, CalledProcessError

_logger = logging.getLogger(__name__)


class LEDPanel:
    cmd_delay = 0.1
    exe_name = "LED-Panel.exe"

    @staticmethod
    def _run(args):
        cmd = [LEDPanel.exe_name] + args.split()
        retries = 3
        _logger.info("Running cmd: %s", " ".join(cmd))
        last_error = None
        try:
            while retries > 0:
                try:
                    check_call(cmd)
                    return
                except (CalledProcessError, FileNotFoundError) as e:
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
        # Slaves the panel's stepping to an external trigger signal (the
        # camera's own trigger, once armed - see
        # engine/dual_panel_control.py) instead of free-running on its own
        # internal timer. Layered on top of response_time_measurement_mode
        # (--setMode 1)/set_speed_ms, not a replacement for them - needed
        # when 2 physically separate panels must stay in lockstep, since
        # each free-running independently on its own would drift.
        LEDPanel._run("--setTriggerMode {}".format(mode))

    @staticmethod
    def set_camera_trigger(enabled):
        LEDPanel._run("--setCameraTrigger {}".format(1 if enabled else 0))

    @staticmethod
    def all_leds_off():
        LEDPanel.stop()
        LEDPanel._run("--setMode 3")
