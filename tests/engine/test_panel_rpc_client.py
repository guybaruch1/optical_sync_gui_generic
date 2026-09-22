import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

import engine.panel_rpc_client as panel_rpc_client


@pytest.fixture(autouse=True)
def _reset_client_state():
    # Module-level connection state persists across tests in the same
    # process - reset it before/after every test so no test's outcome
    # depends on another test's leftover "connected" process.
    import itertools
    panel_rpc_client._state["process"] = None
    panel_rpc_client._state["config"] = None
    panel_rpc_client._state["next_id"] = itertools.count(1)
    yield
    panel_rpc_client._state["process"] = None
    panel_rpc_client._state["config"] = None
    panel_rpc_client._state["next_id"] = itertools.count(1)


class _FakeStdin:
    def __init__(self):
        self.written = []

    def write(self, data):
        self.written.append(data)

    def flush(self):
        pass

    def close(self):
        pass


class _FakeStdout:
    def __init__(self, lines):
        self._lines = list(lines)

    def readline(self):
        if not self._lines:
            return ""
        return self._lines.pop(0)


def _fake_process(response_lines):
    process = MagicMock()
    process.stdin = _FakeStdin()
    process.stdout = _FakeStdout(response_lines)
    process.poll.return_value = None
    return process


def test_led_panel_run_sends_a_request_and_returns_the_result():
    panel_rpc_client.configure("winuser", "winhost", "/repo")
    process = _fake_process(['{"id": 1, "result": null}\n'])
    with patch("subprocess.Popen", return_value=process) as mock_popen:
        result = panel_rpc_client.led_panel_run("--start")

    assert result is None
    request = json.loads(process.stdin.written[0])
    assert request == {"id": 1, "method": "led_panel_run", "args": ["--start"]}
    mock_popen.assert_called_once_with(
        ["ssh", "winuser@winhost", "python3", "-u", "/repo/tools/panel_server/panel_server_stdio.py"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1,
    )


def test_led_panel_query_returns_the_string_result():
    panel_rpc_client.configure("winuser", "winhost", "/repo")
    process = _fake_process(['{"id": 1, "result": "1"}\n'])
    with patch("subprocess.Popen", return_value=process):
        result = panel_rpc_client.led_panel_query("--isRunning")

    assert result == "1"


def test_dual_panel_functions_send_their_configured_args():
    panel_rpc_client.configure("winuser", "winhost", "/repo")
    config = {"stream_a_panel_port": 1}
    process = _fake_process([
        '{"id": 1, "result": null}\n',
        '{"id": 2, "result": null}\n',
        '{"id": 3, "result": null}\n',
        '{"id": 4, "result": null}\n',
        '{"id": 5, "result": null}\n',
        '{"id": 6, "result": null}\n',
    ])
    with patch("subprocess.Popen", return_value=process):
        panel_rpc_client.dual_panel_turn_all_leds_on(config)
        panel_rpc_client.dual_panel_turn_all_leds_off(config)
        panel_rpc_client.dual_panel_start_scanning(5, 1, config)
        panel_rpc_client.dual_panel_stop_scanning(config)
        panel_rpc_client.dual_panel_enter_stream_panel(config, "stream_a")
        panel_rpc_client.dual_panel_exit_stream_panel(config, "stream_a")

    requests = [json.loads(line) for line in process.stdin.written]
    assert [r["method"] for r in requests] == [
        "dual_panel_turn_all_leds_on", "dual_panel_turn_all_leds_off",
        "dual_panel_start_scanning", "dual_panel_stop_scanning",
        "dual_panel_enter_stream_panel", "dual_panel_exit_stream_panel",
    ]
    assert requests[2]["args"] == [5, 1, config]
    assert requests[4]["args"] == [config, "stream_a"]


def test_server_error_response_raises_runtime_error_with_that_message():
    panel_rpc_client.configure("winuser", "winhost", "/repo")
    process = _fake_process(['{"id": 1, "error": "LEDPanel command failed after 3 retries"}\n'])
    with patch("subprocess.Popen", return_value=process):
        with pytest.raises(RuntimeError, match="LEDPanel command failed after 3 retries"):
            panel_rpc_client.led_panel_run("--start")


def test_mismatched_response_id_raises_runtime_error():
    panel_rpc_client.configure("winuser", "winhost", "/repo")
    process = _fake_process(['{"id": 999, "result": null}\n'])
    with patch("subprocess.Popen", return_value=process):
        with pytest.raises(RuntimeError, match="response id mismatch"):
            panel_rpc_client.led_panel_run("--start")


def test_empty_readline_raises_connection_lost_error():
    panel_rpc_client.configure("winuser", "winhost", "/repo")
    process = _fake_process([""])  # remote process exited, pipe closed
    with patch("subprocess.Popen", return_value=process):
        with pytest.raises(RuntimeError, match="Panel server connection lost"):
            panel_rpc_client.led_panel_run("--start")


def test_calling_without_configure_raises_a_clear_error():
    with pytest.raises(RuntimeError, match="configure"):
        panel_rpc_client.led_panel_run("--start")


def test_reuses_the_same_process_across_multiple_calls():
    panel_rpc_client.configure("winuser", "winhost", "/repo")
    process = _fake_process(['{"id": 1, "result": null}\n', '{"id": 2, "result": null}\n'])
    with patch("subprocess.Popen", return_value=process) as mock_popen:
        panel_rpc_client.led_panel_run("--start")
        panel_rpc_client.led_panel_run("--stop")

    mock_popen.assert_called_once()  # only spawned once, not once per call


def test_close_terminates_the_process():
    panel_rpc_client.configure("winuser", "winhost", "/repo")
    process = _fake_process(['{"id": 1, "result": null}\n'])
    with patch("subprocess.Popen", return_value=process):
        panel_rpc_client.led_panel_run("--start")
        panel_rpc_client.close()

    process.terminate.assert_called_once()
    assert panel_rpc_client._state["process"] is None
