import pytest
from unittest.mock import patch, call
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
