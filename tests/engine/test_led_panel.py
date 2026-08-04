import pytest
from unittest.mock import patch, call, MagicMock
from subprocess import CalledProcessError

from engine.led_panel import LEDPanel


def test_all_leds_on_calls_stop_then_set_mode_5():
    with patch("engine.led_panel.check_call") as mock_check_call, patch("time.sleep"):
        LEDPanel.all_leds_on()
        commands = [c.args[0] for c in mock_check_call.call_args_list]
        assert commands[0] == ["LED-Panel.exe", "--stop"]
        assert commands[1] == ["LED-Panel.exe", "--setMode", "5"]


def test_set_speed_ms_converts_to_seconds_string():
    with patch("engine.led_panel.check_call") as mock_check_call, patch("time.sleep"):
        LEDPanel.set_speed_ms(1)
        mock_check_call.assert_called_once_with(["LED-Panel.exe", "--setTime", "0.0010"])


def test_set_mode_sends_only_set_mode_no_preceding_stop():
    # Unlike response_time_measurement_mode()/all_leds_on()/off()/
    # rolling_shutter_mode() (which all send --stop first), set_mode()
    # must NOT - confirmed via real-hardware testing that a --stop before
    # entering dual-panel trigger mode prevents the panel from actually
    # stepping once triggered.
    with patch("engine.led_panel.check_call") as mock_check_call, patch("time.sleep"):
        LEDPanel.set_mode(1)
        mock_check_call.assert_called_once_with(["LED-Panel.exe", "--setMode", "1"])


def test_run_retries_on_called_process_error_then_raises():
    with patch("engine.led_panel.check_call", side_effect=CalledProcessError(1, "cmd")) as mock_check_call, \
         patch("time.sleep"):
        # A command that never succeeds must not fail silently - every caller
        # (start(), set_speed_ms(), etc.) needs to know the panel never
        # actually received the command, instead of assuming it did.
        with pytest.raises(RuntimeError):
            LEDPanel._run("--stop")
        assert mock_check_call.call_count == 3  # 3 retries, per the original script's convention


def test_run_succeeds_without_raising_when_check_call_succeeds():
    with patch("engine.led_panel.check_call") as mock_check_call, patch("time.sleep"):
        LEDPanel._run("--stop")  # must not raise
        assert mock_check_call.call_count == 1


def test_run_recovers_after_a_transient_failure():
    with patch(
        "engine.led_panel.check_call",
        side_effect=[CalledProcessError(1, "cmd"), None],
    ) as mock_check_call, patch("time.sleep"):
        LEDPanel._run("--stop")  # succeeds on the 2nd attempt - must not raise
        assert mock_check_call.call_count == 2


def test_set_trigger_mode_sends_set_trigger_mode_command():
    with patch("engine.led_panel.check_call") as mock_check_call, patch("time.sleep"):
        LEDPanel.set_trigger_mode(2)
        mock_check_call.assert_called_once_with(["LED-Panel.exe", "--setTriggerMode", "2"])


def test_set_camera_trigger_true_sends_1():
    # LED-Panel.exe --help documents --setCameraTrigger as [0] Enable,
    # [1] Disable, but real-hardware testing showed following that
    # literally broke a previously-reliable workaround ("interrupt a run,
    # the next one steps") that 1-for-enabled never affected - the
    # documented mapping doesn't match this panel's actual firmware
    # behavior. Don't "fix" this again without confirming on real
    # hardware first.
    with patch("engine.led_panel.check_call") as mock_check_call, patch("time.sleep"):
        LEDPanel.set_camera_trigger(True)
        mock_check_call.assert_called_once_with(["LED-Panel.exe", "--setCameraTrigger", "1"])


def test_set_camera_trigger_false_sends_0():
    with patch("engine.led_panel.check_call") as mock_check_call, patch("time.sleep"):
        LEDPanel.set_camera_trigger(False)
        mock_check_call.assert_called_once_with(["LED-Panel.exe", "--setCameraTrigger", "0"])


def test_set_stop_trigger_true_sends_0():
    # Same [0]=Enable/[1]=Disable convention as set_camera_trigger.
    with patch("engine.led_panel.check_call") as mock_check_call, patch("time.sleep"):
        LEDPanel.set_stop_trigger(True)
        mock_check_call.assert_called_once_with(["LED-Panel.exe", "--setStopTrigger", "0"])


def test_set_stop_trigger_false_sends_1():
    with patch("engine.led_panel.check_call") as mock_check_call, patch("time.sleep"):
        LEDPanel.set_stop_trigger(False)
        mock_check_call.assert_called_once_with(["LED-Panel.exe", "--setStopTrigger", "1"])


class _FakeCoord:
    def __init__(self, x, y):
        self.X = x
        self.Y = y


def _fake_win32console_module(output_text="0", cursor_before=(0, 5), cursor_after=(0, 5), buffer_width=80):
    """win32console is imported lazily inside LEDPanel._query/
    _read_console_output (same reason engine/acroname_hub.py's real SDK is
    imported lazily elsewhere in this project) - patched via sys.modules,
    same pattern tests/engine/test_dual_panel_control.py already uses for
    engine.acroname_hub."""
    fake_handle = MagicMock()
    fake_handle.GetConsoleScreenBufferInfo.side_effect = [
        {"CursorPosition": _FakeCoord(*cursor_before), "Size": _FakeCoord(buffer_width, 25)},
        {"CursorPosition": _FakeCoord(*cursor_after), "Size": _FakeCoord(buffer_width, 25)},
    ]
    fake_handle.ReadConsoleOutputCharacter.return_value = output_text

    fake_module = MagicMock()
    fake_module.GetStdHandle.return_value = fake_handle
    fake_module.STD_OUTPUT_HANDLE = -11
    fake_module.PyCOORDType.side_effect = lambda x, y: _FakeCoord(x, y)
    return fake_module, fake_handle


def test_query_reads_text_from_the_console_screen_buffer():
    fake_module, fake_handle = _fake_win32console_module(output_text="1  ")
    with patch.dict("sys.modules", {"win32console": fake_module}), \
         patch("engine.led_panel.check_call") as mock_check_call, patch("time.sleep"):
        result = LEDPanel._query("--isRunning")
        assert result == "1"
        mock_check_call.assert_called_once_with(["LED-Panel.exe", "--isRunning"])


def test_query_reads_multiple_rows_if_the_cursor_moved_more_than_one_line():
    fake_module, fake_handle = _fake_win32console_module(
        output_text="row", cursor_before=(0, 5), cursor_after=(0, 6),
    )
    with patch.dict("sys.modules", {"win32console": fake_module}), \
         patch("engine.led_panel.check_call"), patch("time.sleep"):
        result = LEDPanel._query("--isRunning")
        assert result == "row\nrow"
        assert fake_handle.ReadConsoleOutputCharacter.call_count == 2


def test_query_raises_a_clear_error_when_no_real_console_is_attached():
    fake_module = MagicMock()
    fake_module.GetStdHandle.side_effect = Exception("The handle is invalid.")
    with patch.dict("sys.modules", {"win32console": fake_module}):
        with pytest.raises(RuntimeError, match="real, native Windows console"):
            LEDPanel._query("--isRunning")


def test_query_retries_on_called_process_error_then_raises():
    fake_module, _ = _fake_win32console_module()
    with patch.dict("sys.modules", {"win32console": fake_module}), \
         patch("engine.led_panel.check_call", side_effect=CalledProcessError(1, "cmd")) as mock_check_call, \
         patch("time.sleep"):
        with pytest.raises(RuntimeError):
            LEDPanel._query("--isRunning")
        assert mock_check_call.call_count == 3


@pytest.mark.parametrize("method_name, expected_args", [
    ("is_running", ["LED-Panel.exe", "--isRunning"]),
    ("get_current_led", ["LED-Panel.exe", "--getCurrentLED"]),
    ("get_mode", ["LED-Panel.exe", "--getMode"]),
    ("get_trigger_mode", ["LED-Panel.exe", "--getTriggerMode"]),
    ("get_camera_trigger", ["LED-Panel.exe", "--getCameraTrigger"]),
    ("get_camera_trigger_state", ["LED-Panel.exe", "--getCameraTriggerState"]),
    ("get_stop_trigger", ["LED-Panel.exe", "--getStopTrigger"]),
    ("get_stop_trigger_state", ["LED-Panel.exe", "--getStopTriggerState"]),
])
def test_query_methods_send_the_right_command(method_name, expected_args):
    fake_module, _ = _fake_win32console_module(output_text="0")
    with patch.dict("sys.modules", {"win32console": fake_module}), \
         patch("engine.led_panel.check_call") as mock_check_call, patch("time.sleep"):
        result = getattr(LEDPanel, method_name)()
        assert result == "0"
        mock_check_call.assert_called_once_with(expected_args)
