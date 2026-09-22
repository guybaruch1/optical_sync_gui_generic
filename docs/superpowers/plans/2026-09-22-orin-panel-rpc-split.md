# Orin/Windows LED-Panel RPC Split Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the camera + GUI run on an NVIDIA Orin while the LED-panel hardware (Windows-only `LED-Panel.exe`, plus the Acroname hub/relay in dual-panel setups) stays on a Windows machine, reached over SSH - with the existing single-machine Windows setup completely unchanged when this feature isn't turned on.

**Architecture:** A `panel_connection.mode` setting (`local`/`remote`) gates two hardware-facing files (`engine/led_panel.py`, `engine/dual_panel_control.py`) at their existing chokepoints. In remote mode, calls are redirected through a new `engine/panel_rpc_client.py`, which spawns the Windows-side control script (`tools/panel_server/panel_server_stdio.py`) over `ssh` once and exchanges newline-delimited JSON requests/responses over that subprocess's stdin/stdout. The server script runs the exact same, unmodified hardware code the app already uses locally.

**Tech Stack:** Python 3 stdlib only (`subprocess`, `json`, `threading`) - no new dependencies.

**Spec:** [docs/superpowers/specs/2026-09-22-orin-panel-rpc-split-design.md](../specs/2026-09-22-orin-panel-rpc-split-design.md)

## Global Constraints

- `panel_connection.mode: local` is the default and must leave every existing single-machine Windows code path byte-for-byte unchanged.
- No new TCP listener anywhere, not even on loopback - SSH's own stdin/stdout pipe is the only transport.
- `LEDPanel._query`'s console-buffer trick is never called from the remote path (it's diagnostics-only, per the spec's §1) - do not try to make it work over SSH.
- `_run`'s `check_call` must redirect `LED-Panel.exe`'s stdout/stderr to `DEVNULL` unconditionally (not just in remote mode), so the fix applies identically regardless of which mode is active.
- Existing hardware-timing logic in `dual_panel_control.py` (the lock, the priming dict, the double-arm sequence, `--stop`-vs-`--reset`) must not be edited - only wrapped with a guard.

---

### Task 1: `engine/panel_rpc_client.py` - the SSH-stdio client

**Files:**
- Create: `engine/panel_rpc_client.py`
- Test: `tests/engine/test_panel_rpc_client.py`

**Interfaces:**
- Consumes: nothing from other tasks (stdlib only).
- Produces (used by Tasks 2 and 3):
  - `configure(ssh_user: str, ssh_host: str, remote_repo_path: str) -> None`
  - `led_panel_run(args: str) -> None`
  - `led_panel_query(args: str) -> str`
  - `dual_panel_turn_all_leds_on(dual_panel_config: dict) -> None`
  - `dual_panel_turn_all_leds_off(dual_panel_config: dict) -> None`
  - `dual_panel_start_scanning(switch_time_ms, scan_direction, dual_panel_config: dict) -> None`
  - `dual_panel_stop_scanning(dual_panel_config: dict) -> None`
  - `dual_panel_enter_stream_panel(dual_panel_config: dict, stream_name: str) -> None`
  - `dual_panel_exit_stream_panel(dual_panel_config: dict, stream_name: str) -> None`
  - `close() -> None`
  - All of the above raise `RuntimeError` on any failure (server-side error, or the connection being lost).

- [ ] **Step 1: Write the failing tests**

```python
# tests/engine/test_panel_rpc_client.py
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
    panel_rpc_client._state["process"] = None
    panel_rpc_client._state["config"] = None
    yield
    panel_rpc_client._state["process"] = None
    panel_rpc_client._state["config"] = None


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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/engine/test_panel_rpc_client.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'engine.panel_rpc_client'`

- [ ] **Step 3: Write the implementation**

```python
# engine/panel_rpc_client.py
"""SSH-spawned stdio client for the Windows-side LED-panel control script
(tools/panel_server/panel_server_stdio.py). Used only when
engine.led_panel.PANEL_CONNECTION["mode"] == "remote" - see
docs/superpowers/specs/2026-09-22-orin-panel-rpc-split-design.md.

Spawns the remote script once, lazily, over `ssh`, and reuses that one
subprocess (and its stdin/stdout pipes) for the rest of the process's
life - there is no separate server to start or port to open; `ssh` itself
is the only thing that crosses the network.
"""

import itertools
import json
import subprocess
import threading

_lock = threading.Lock()
_state = {"process": None, "config": None, "next_id": itertools.count(1)}


def configure(ssh_user, ssh_host, remote_repo_path):
    """Called once at startup (main.py) before any remote call is made."""
    _state["config"] = {
        "ssh_user": ssh_user,
        "ssh_host": ssh_host,
        "remote_repo_path": remote_repo_path,
    }


def _ensure_connected():
    process = _state["process"]
    if process is not None and process.poll() is None:
        return process

    config = _state["config"]
    if config is None:
        raise RuntimeError(
            "panel_rpc_client.configure() was never called - cannot reach a "
            "remote panel server without ssh_user/ssh_host/remote_repo_path"
        )
    remote_script = "{}/tools/panel_server/panel_server_stdio.py".format(
        config["remote_repo_path"]
    )
    target = "{}@{}".format(config["ssh_user"], config["ssh_host"])
    process = subprocess.Popen(
        ["ssh", target, "python3", "-u", remote_script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    _state["process"] = process
    return process


def _call(method, args):
    with _lock:
        process = _ensure_connected()
        request_id = next(_state["next_id"])
        request = json.dumps({"id": request_id, "method": method, "args": args})
        try:
            process.stdin.write(request + "\n")
            process.stdin.flush()
            line = process.stdout.readline()
        except (BrokenPipeError, OSError) as exc:
            raise RuntimeError("Panel server connection lost - {}".format(exc))

        if not line:
            raise RuntimeError("Panel server connection lost - remote process exited")

        response = json.loads(line)
        if response["id"] != request_id:
            raise RuntimeError(
                "Panel server response id mismatch: expected {}, got {}".format(
                    request_id, response["id"]
                )
            )
        if "error" in response:
            raise RuntimeError(response["error"])
        return response["result"]


def led_panel_run(args):
    return _call("led_panel_run", [args])


def led_panel_query(args):
    return _call("led_panel_query", [args])


def dual_panel_turn_all_leds_on(dual_panel_config):
    return _call("dual_panel_turn_all_leds_on", [dual_panel_config])


def dual_panel_turn_all_leds_off(dual_panel_config):
    return _call("dual_panel_turn_all_leds_off", [dual_panel_config])


def dual_panel_start_scanning(switch_time_ms, scan_direction, dual_panel_config):
    return _call(
        "dual_panel_start_scanning", [switch_time_ms, scan_direction, dual_panel_config]
    )


def dual_panel_stop_scanning(dual_panel_config):
    return _call("dual_panel_stop_scanning", [dual_panel_config])


def dual_panel_enter_stream_panel(dual_panel_config, stream_name):
    return _call("dual_panel_enter_stream_panel", [dual_panel_config, stream_name])


def dual_panel_exit_stream_panel(dual_panel_config, stream_name):
    return _call("dual_panel_exit_stream_panel", [dual_panel_config, stream_name])


def close():
    with _lock:
        process = _state["process"]
        if process is None:
            return
        try:
            process.stdin.close()
        except OSError:
            pass
        process.terminate()
        _state["process"] = None
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/engine/test_panel_rpc_client.py -v`
Expected: PASS (10 tests)

- [ ] **Step 5: Commit**

```bash
git add engine/panel_rpc_client.py tests/engine/test_panel_rpc_client.py
git commit -m "feat: add SSH-stdio client for remote LED-panel control"
```

---

### Task 2: `engine/led_panel.py` - remote-mode guard + `DEVNULL` fix

**Files:**
- Modify: `engine/led_panel.py`
- Modify: `settings.yaml`
- Modify: `tests/engine/test_led_panel.py`

**Interfaces:**
- Consumes: `engine.panel_rpc_client.led_panel_run`/`led_panel_query` (Task 1).
- Produces (used by Tasks 3, 4, 5):
  - `engine.led_panel.PANEL_CONNECTION: dict` (module-level, `{"mode": "local"}` by default)
  - `engine.led_panel.configure_panel_connection(config: dict) -> None`

- [ ] **Step 1: Add the `panel_connection` block to `settings.yaml`**

Add this block right after the existing `dual_panel:` section (before `paths:`):

```yaml
# Only read when the operator wants the camera + GUI running on a
# different machine (e.g. an NVIDIA Orin) than the one the LED-panel
# hardware is physically attached to - see engine/panel_rpc_client.py and
# tools/panel_server/panel_server_stdio.py. mode: local (the default)
# keeps this app's normal single-machine behavior completely unchanged;
# nothing below this line is read unless mode is switched to "remote".
panel_connection:
  mode: local
  # Only used when mode: remote - the Windows machine the LED-panel
  # hardware (LED-Panel.exe, and the Acroname hub/relay in dual-panel
  # setups) is physically attached to. Needs key-based SSH login already
  # set up for this to connect without a password prompt.
  ssh_user: ""
  ssh_host: ""
  # Where this same repo lives on that Windows machine, used to locate
  # tools/panel_server/panel_server_stdio.py to run over ssh.
  remote_repo_path: ""
```

- [ ] **Step 2: Write the failing tests**

These tests both add new remote-mode coverage AND update the existing
`_run`-based assertions to expect the new `stdout=DEVNULL, stderr=DEVNULL`
kwargs (the DEVNULL change makes the *old* assertions fail, which is
expected and correct here - fix them in the same step, not around it).

```python
# tests/engine/test_led_panel.py
# Add near the top, alongside the existing imports:
from subprocess import DEVNULL

# ... then EDIT every existing check_call assertion for a _run-based
# method IN PLACE to include the new kwargs - replace each function body
# below inside its existing, same-named `def` in the file; do not add a
# second definition with the same name anywhere. The functions below are
# shown in full only so the exact expected assertion is unambiguous:

def test_set_speed_ms_converts_to_seconds_string():
    with patch("engine.led_panel.check_call") as mock_check_call, patch("time.sleep"):
        LEDPanel.set_speed_ms(1)
        mock_check_call.assert_called_once_with(
            ["LED-Panel.exe", "--setTime", "0.0010"],
            timeout=LEDPanel.cmd_timeout_s, stdout=DEVNULL, stderr=DEVNULL)


def test_set_speed_ms_accepts_a_fractional_value():
    with patch("engine.led_panel.check_call") as mock_check_call, patch("time.sleep"):
        LEDPanel.set_speed_ms(0.5)
        mock_check_call.assert_called_once_with(
            ["LED-Panel.exe", "--setTime", "0.0005"],
            timeout=LEDPanel.cmd_timeout_s, stdout=DEVNULL, stderr=DEVNULL)


def test_set_mode_sends_only_set_mode_no_preceding_stop():
    with patch("engine.led_panel.check_call") as mock_check_call, patch("time.sleep"):
        LEDPanel.set_mode(1)
        mock_check_call.assert_called_once_with(
            ["LED-Panel.exe", "--setMode", "1"],
            timeout=LEDPanel.cmd_timeout_s, stdout=DEVNULL, stderr=DEVNULL)


def test_set_trigger_mode_sends_set_trigger_mode_command():
    with patch("engine.led_panel.check_call") as mock_check_call, patch("time.sleep"):
        LEDPanel.set_trigger_mode(2)
        mock_check_call.assert_called_once_with(
            ["LED-Panel.exe", "--setTriggerMode", "2"],
            timeout=LEDPanel.cmd_timeout_s, stdout=DEVNULL, stderr=DEVNULL)


def test_set_camera_trigger_true_sends_1():
    with patch("engine.led_panel.check_call") as mock_check_call, patch("time.sleep"):
        LEDPanel.set_camera_trigger(True)
        mock_check_call.assert_called_once_with(
            ["LED-Panel.exe", "--setCameraTrigger", "1"],
            timeout=LEDPanel.cmd_timeout_s, stdout=DEVNULL, stderr=DEVNULL)


def test_set_camera_trigger_false_sends_0():
    with patch("engine.led_panel.check_call") as mock_check_call, patch("time.sleep"):
        LEDPanel.set_camera_trigger(False)
        mock_check_call.assert_called_once_with(
            ["LED-Panel.exe", "--setCameraTrigger", "0"],
            timeout=LEDPanel.cmd_timeout_s, stdout=DEVNULL, stderr=DEVNULL)


def test_set_stop_trigger_true_sends_0():
    with patch("engine.led_panel.check_call") as mock_check_call, patch("time.sleep"):
        LEDPanel.set_stop_trigger(True)
        mock_check_call.assert_called_once_with(
            ["LED-Panel.exe", "--setStopTrigger", "0"],
            timeout=LEDPanel.cmd_timeout_s, stdout=DEVNULL, stderr=DEVNULL)


def test_set_stop_trigger_false_sends_1():
    with patch("engine.led_panel.check_call") as mock_check_call, patch("time.sleep"):
        LEDPanel.set_stop_trigger(False)
        mock_check_call.assert_called_once_with(
            ["LED-Panel.exe", "--setStopTrigger", "1"],
            timeout=LEDPanel.cmd_timeout_s, stdout=DEVNULL, stderr=DEVNULL)


# New tests, appended at the end of the file:

@pytest.fixture(autouse=True)
def _reset_panel_connection_mode():
    # PANEL_CONNECTION is module-level state that persists across tests -
    # reset it to the real fresh-process default before/after every test.
    from engine.led_panel import PANEL_CONNECTION
    PANEL_CONNECTION.clear()
    PANEL_CONNECTION["mode"] = "local"
    yield
    PANEL_CONNECTION.clear()
    PANEL_CONNECTION["mode"] = "local"


def test_configure_panel_connection_sets_the_module_level_dict():
    from engine.led_panel import configure_panel_connection, PANEL_CONNECTION
    configure_panel_connection({"mode": "remote", "ssh_host": "orin-panel-host"})
    assert PANEL_CONNECTION == {"mode": "remote", "ssh_host": "orin-panel-host"}


def test_run_delegates_to_panel_rpc_client_when_mode_is_remote():
    from engine.led_panel import PANEL_CONNECTION
    PANEL_CONNECTION["mode"] = "remote"
    with patch("engine.panel_rpc_client.led_panel_run") as mock_remote_run, \
         patch("engine.led_panel.check_call") as mock_check_call:
        LEDPanel._run("--start")
        mock_remote_run.assert_called_once_with("--start")
        mock_check_call.assert_not_called()


def test_query_delegates_to_panel_rpc_client_when_mode_is_remote():
    from engine.led_panel import PANEL_CONNECTION
    PANEL_CONNECTION["mode"] = "remote"
    with patch("engine.panel_rpc_client.led_panel_query", return_value="1") as mock_remote_query, \
         patch("engine.led_panel.check_call") as mock_check_call:
        result = LEDPanel._query("--isRunning")
        assert result == "1"
        mock_remote_query.assert_called_once_with("--isRunning")
        mock_check_call.assert_not_called()


def test_run_propagates_a_runtime_error_from_panel_rpc_client():
    from engine.led_panel import PANEL_CONNECTION
    PANEL_CONNECTION["mode"] = "remote"
    with patch("engine.panel_rpc_client.led_panel_run",
               side_effect=RuntimeError("Panel server connection lost")):
        with pytest.raises(RuntimeError, match="Panel server connection lost"):
            LEDPanel._run("--start")
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/engine/test_led_panel.py -v`
Expected: the updated `test_set_*`/`test_run_*` assertions FAIL (missing
`stdout`/`stderr` kwargs), and the new `test_configure_panel_connection_*`/
`test_run_delegates_*`/`test_query_delegates_*` tests FAIL with
`ImportError`/`AttributeError` (`configure_panel_connection`/
`PANEL_CONNECTION` don't exist yet).

- [ ] **Step 4: Write the implementation**

```python
# engine/led_panel.py
# Change the import line at the top from:
#   from subprocess import check_call, CalledProcessError, TimeoutExpired
# to:
from subprocess import check_call, CalledProcessError, TimeoutExpired, DEVNULL

_logger = logging.getLogger(__name__)

# Gates every LEDPanel.* call between running LED-Panel.exe locally
# ("local", the default - unchanged single-machine behavior) and
# redirecting it over engine.panel_rpc_client to a Windows machine the
# hardware is actually attached to ("remote"). Set once at startup via
# configure_panel_connection() (main.py) - never called (e.g. tests
# importing this module directly) leaves this at its safe default.
PANEL_CONNECTION = {"mode": "local"}


def configure_panel_connection(config):
    """Called once at startup with settings["panel_connection"]."""
    PANEL_CONNECTION.clear()
    PANEL_CONNECTION.update(config)


class LEDPanel:
    cmd_delay = 0.1
    exe_name = "LED-Panel.exe"
    cmd_timeout_s = 5.0

    @staticmethod
    def _run(args):
        if PANEL_CONNECTION["mode"] == "remote":
            from engine import panel_rpc_client
            return panel_rpc_client.led_panel_run(args)

        cmd = [LEDPanel.exe_name] + args.split()
        retries = 3
        _logger.info("Running cmd: %s", " ".join(cmd))
        last_error = None
        try:
            while retries > 0:
                try:
                    # stdout/stderr redirected to DEVNULL unconditionally -
                    # this output was never read by any caller even before
                    # remote mode existed, but once this same _run() runs
                    # as a subprocess of panel_server_stdio.py (whose own
                    # stdout is the JSON-response pipe back to the remote
                    # caller), an unredirected LED-Panel.exe inheriting that
                    # fd would corrupt the response stream.
                    check_call(cmd, timeout=LEDPanel.cmd_timeout_s,
                               stdout=DEVNULL, stderr=DEVNULL)
                    return
                except (CalledProcessError, FileNotFoundError, TimeoutExpired) as e:
                    last_error = e
                    retries -= 1
                    _logger.error("Command returned with an error: %s", e)
                    _logger.info("Retries left: %d", retries)
                    if retries > 0:
                        time.sleep(0.5)
            raise RuntimeError(
                "LEDPanel command failed after {} retries: {} ({})".format(
                    3, cmd, last_error
                )
            )
        finally:
            time.sleep(LEDPanel.cmd_delay)

    @staticmethod
    def _query(args):
        # Keep the existing docstring here (currently lines 66-89) exactly
        # as it is today - don't touch a single word of it.
        if PANEL_CONNECTION["mode"] == "remote":
            from engine import panel_rpc_client
            return panel_rpc_client.led_panel_query(args)

        # Everything from here down (currently lines 90-126: the
        # `import win32console`, `cmd = [...]`, the retry loop calling
        # `LEDPanel._read_console_output`, and the final `raise
        # RuntimeError`/`finally: time.sleep(...)`) is copied verbatim,
        # completely unchanged - only indentation stays the same too,
        # since it's already inside the method body.
```

`_read_console_output` (today's lines 128-146) and every method from
`all_leds_on` (line 147) onward through the end of the file are not
touched at all by this task - only the import line at the top, the two
new module-level items (`PANEL_CONNECTION`, `configure_panel_connection`)
inserted just above `class LEDPanel:`, the two guards described above,
and `_run`'s `check_call` kwargs change.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/engine/test_led_panel.py -v`
Expected: PASS (all tests, old and new)

- [ ] **Step 6: Commit**

```bash
git add engine/led_panel.py settings.yaml tests/engine/test_led_panel.py
git commit -m "feat: gate LEDPanel hardware calls behind a local/remote mode"
```

---

### Task 3: `engine/dual_panel_control.py` - remote guards + enter/exit split

**Files:**
- Modify: `engine/dual_panel_control.py`
- Modify: `tests/engine/test_dual_panel_control.py`

**Interfaces:**
- Consumes: `engine.led_panel.PANEL_CONNECTION` (Task 2), `engine.panel_rpc_client.dual_panel_*` (Task 1).
- Produces (used by Task 4):
  - `engine.dual_panel_control.enter_stream_panel(dual_panel_config: dict, stream_name: str) -> None`
  - `engine.dual_panel_control.exit_stream_panel(dual_panel_config: dict, stream_name: str) -> None`

- [ ] **Step 1: Write the failing tests**

Append to `tests/engine/test_dual_panel_control.py`:

```python
from engine.dual_panel_control import enter_stream_panel, exit_stream_panel


# --- Remote mode: dual_panel_config is not None + PANEL_CONNECTION is
# "remote" delegates the WHOLE call to panel_rpc_client, instead of
# touching the (mocked-out, in these tests) local hub/relay machinery. ---

@pytest.fixture(autouse=True)
def _reset_panel_connection_mode():
    from engine.led_panel import PANEL_CONNECTION
    PANEL_CONNECTION.clear()
    PANEL_CONNECTION["mode"] = "local"
    yield
    PANEL_CONNECTION.clear()
    PANEL_CONNECTION["mode"] = "local"


def test_turn_all_leds_on_in_remote_mode_delegates_to_panel_rpc_client():
    from engine.led_panel import PANEL_CONNECTION
    PANEL_CONNECTION["mode"] = "remote"
    with patch("engine.panel_rpc_client.dual_panel_turn_all_leds_on") as mock_remote, \
         patch.object(dual_panel_control, "_run_on_both_panels") as mock_run_on_both:
        turn_all_leds_on(DUAL_PANEL_CONFIG)
        mock_remote.assert_called_once_with(DUAL_PANEL_CONFIG)
        mock_run_on_both.assert_not_called()


def test_turn_all_leds_off_in_remote_mode_delegates_to_panel_rpc_client():
    from engine.led_panel import PANEL_CONNECTION
    PANEL_CONNECTION["mode"] = "remote"
    with patch("engine.panel_rpc_client.dual_panel_turn_all_leds_off") as mock_remote, \
         patch.object(dual_panel_control, "_run_on_both_panels") as mock_run_on_both:
        turn_all_leds_off(DUAL_PANEL_CONFIG)
        mock_remote.assert_called_once_with(DUAL_PANEL_CONFIG)
        mock_run_on_both.assert_not_called()


def test_start_scanning_in_remote_mode_delegates_to_panel_rpc_client():
    from engine.led_panel import PANEL_CONNECTION
    PANEL_CONNECTION["mode"] = "remote"
    with patch("engine.panel_rpc_client.dual_panel_start_scanning") as mock_remote, \
         patch.object(dual_panel_control, "_run_on_both_panels") as mock_run_on_both, \
         patch.object(dual_panel_control, "_relay_on") as mock_relay_on:
        start_scanning(5, 1, DUAL_PANEL_CONFIG)
        mock_remote.assert_called_once_with(5, 1, DUAL_PANEL_CONFIG)
        mock_run_on_both.assert_not_called()
        mock_relay_on.assert_not_called()


def test_stop_scanning_in_remote_mode_delegates_to_panel_rpc_client():
    from engine.led_panel import PANEL_CONNECTION
    PANEL_CONNECTION["mode"] = "remote"
    with patch("engine.panel_rpc_client.dual_panel_stop_scanning") as mock_remote, \
         patch.object(dual_panel_control, "_run_on_both_panels") as mock_run_on_both, \
         patch.object(dual_panel_control, "_relay_off") as mock_relay_off:
        stop_scanning(DUAL_PANEL_CONFIG)
        mock_remote.assert_called_once_with(DUAL_PANEL_CONFIG)
        mock_run_on_both.assert_not_called()
        mock_relay_off.assert_not_called()


def test_single_panel_path_is_unaffected_by_remote_mode():
    # dual_panel_config=None must take the exact same local LEDPanel path
    # regardless of PANEL_CONNECTION - it's already covered by
    # engine/led_panel.py's own guard, one layer down.
    from engine.led_panel import PANEL_CONNECTION
    PANEL_CONNECTION["mode"] = "remote"
    with patch("engine.dual_panel_control.LEDPanel") as mock_led_panel, \
         patch("engine.panel_rpc_client.dual_panel_turn_all_leds_on") as mock_remote:
        turn_all_leds_on(None)
        mock_led_panel.stop.assert_called_once()
        mock_led_panel.all_leds_on.assert_called_once()
        mock_remote.assert_not_called()


def test_switched_to_stream_panel_in_remote_mode_calls_enter_then_exit():
    from engine.led_panel import PANEL_CONNECTION
    PANEL_CONNECTION["mode"] = "remote"
    call_order = []
    with patch("engine.panel_rpc_client.dual_panel_enter_stream_panel",
               side_effect=lambda cfg, name: call_order.append(("enter", name))) as mock_enter, \
         patch("engine.panel_rpc_client.dual_panel_exit_stream_panel",
               side_effect=lambda cfg, name: call_order.append(("exit", name))) as mock_exit:
        with switched_to_stream_panel(DUAL_PANEL_CONFIG, "stream_a"):
            call_order.append("inside")

    assert call_order == [("enter", "stream_a"), "inside", ("exit", "stream_a")]
    mock_enter.assert_called_once_with(DUAL_PANEL_CONFIG, "stream_a")
    mock_exit.assert_called_once_with(DUAL_PANEL_CONFIG, "stream_a")


def test_switched_to_stream_panel_in_remote_mode_still_exits_if_block_raises():
    from engine.led_panel import PANEL_CONNECTION
    PANEL_CONNECTION["mode"] = "remote"
    with patch("engine.panel_rpc_client.dual_panel_enter_stream_panel"), \
         patch("engine.panel_rpc_client.dual_panel_exit_stream_panel") as mock_exit:
        with pytest.raises(ValueError, match="boom"):
            with switched_to_stream_panel(DUAL_PANEL_CONFIG, "stream_a"):
                raise ValueError("boom")
    mock_exit.assert_called_once_with(DUAL_PANEL_CONFIG, "stream_a")


# --- enter_stream_panel/exit_stream_panel: the two standalone functions
# switched_to_stream_panel's LOCAL branch now calls internally, and that
# tools/panel_server/panel_server_stdio.py registers directly for the
# remote case. Same hub/LEDPanel behavior switched_to_stream_panel's
# existing tests above already cover end-to-end - these two just confirm
# the split itself didn't change anything. ---

def test_enter_then_exit_stream_panel_match_switched_to_stream_panel_behavior():
    fake_hub = _FakeHubForSwitch()

    def fake_acroname_hub_module():
        return type("module", (), {"AcronameHub": lambda: fake_hub})

    with patch.dict("sys.modules", {"engine.acroname_hub": fake_acroname_hub_module()}), \
         patch("engine.dual_panel_control.LEDPanel") as mock_led_panel, \
         patch("time.sleep") as mock_sleep:
        enter_stream_panel(DUAL_PANEL_CONFIG, "stream_a")
        exit_stream_panel(DUAL_PANEL_CONFIG, "stream_a")

    assert fake_hub.calls == [
        "try_connect",
        ("enable", [1], False), ("disable", [0, 6]),
        "disconnect",
    ]
    mock_sleep.assert_called_once_with(3.0)
    mock_led_panel.reset.assert_called_once()
    assert dual_panel_control._stream_panel_state["hub"] is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/engine/test_dual_panel_control.py -v`
Expected: the new remote-mode tests FAIL (no such guard yet), and
`enter_stream_panel`/`exit_stream_panel` tests FAIL with `ImportError`
(they don't exist yet). Every pre-existing test must still PASS unchanged
at this step (they exercise the untouched local path).

- [ ] **Step 3: Write the implementation**

```python
# engine/dual_panel_control.py
# Change the import line at the top from:
#   from engine.led_panel import LEDPanel
# to:
from engine.led_panel import LEDPanel, PANEL_CONNECTION

# ... _dual_panel_lock / _dual_panel_primed unchanged ...


def turn_all_leds_on(dual_panel_config):
    if dual_panel_config is None:
        LEDPanel.stop()
        LEDPanel.all_leds_on()
    elif PANEL_CONNECTION["mode"] == "remote":
        from engine import panel_rpc_client
        panel_rpc_client.dual_panel_turn_all_leds_on(dual_panel_config)
    else:
        _run_on_both_panels(dual_panel_config, lambda: (LEDPanel.stop(), LEDPanel.all_leds_on()))


def turn_all_leds_off(dual_panel_config):
    if dual_panel_config is None:
        LEDPanel.all_leds_off()
    elif PANEL_CONNECTION["mode"] == "remote":
        from engine import panel_rpc_client
        panel_rpc_client.dual_panel_turn_all_leds_off(dual_panel_config)
    else:
        _run_on_both_panels(dual_panel_config, LEDPanel.all_leds_off)


def start_scanning(switch_time_ms, scan_direction, dual_panel_config):
    """Keep the existing docstring (lines 106-111 today) exactly as-is."""
    if dual_panel_config is None:
        LEDPanel.stop()
        LEDPanel.response_time_measurement_mode()
        LEDPanel.set_direction_single(scan_direction if scan_direction is not None else 1)
        LEDPanel.set_speed_ms(switch_time_ms)
        LEDPanel.start()
        return

    if PANEL_CONNECTION["mode"] == "remote":
        from engine import panel_rpc_client
        panel_rpc_client.dual_panel_start_scanning(switch_time_ms, scan_direction, dual_panel_config)
        return

    # Today (lines 118-226), everything from here down is the body of an
    # `else:` block. Keep every line of it - the comments included - byte
    # for byte, just re-indented one level shallower (drop the `else:`
    # wrapper, since the two guards above already `return`ed for every
    # other case): the `configure_one_panel()`/`_arm_once()` closures, and
    # the closing `with _dual_panel_lock:` block ending in
    # `_dual_panel_primed["scan_direction"] = scan_direction`. Do not
    # rewrite, reformat, or "clean up" any of it - it encodes real-hardware
    # findings (see the comments themselves).


def stop_scanning(dual_panel_config):
    if dual_panel_config is None:
        LEDPanel.stop()
        return

    if PANEL_CONNECTION["mode"] == "remote":
        from engine import panel_rpc_client
        panel_rpc_client.dual_panel_stop_scanning(dual_panel_config)
        return

    with _dual_panel_lock:
        _relay_off()
        _run_on_both_panels(dual_panel_config, LEDPanel.reset)


# _run_on_both_panels (today's lines 258-290) is not touched by this task
# at all.


_stream_panel_state = {"hub": None}


def enter_stream_panel(dual_panel_config, stream_name):
    """The 'enter' half of switched_to_stream_panel's body, pulled into its
    own function so tools/panel_server/panel_server_stdio.py can register
    it directly for the remote case, paired with exit_stream_panel below -
    a generator-based context manager can't be entered and left open
    across two separate round-trips, so this is what actually holds the
    connected hub across them (module-level, mirroring _relay_connection's
    existing pattern in this same file)."""
    hub = _connect_hub()
    my_port = dual_panel_config["{}_panel_port".format(stream_name)]
    other_stream = "stream_b" if stream_name == "stream_a" else "stream_a"
    other_port = dual_panel_config["{}_panel_port".format(other_stream)]
    relay_port = dual_panel_config["relay_port"]

    hub.enable_ports([my_port], False, delay_in_seconds=0)
    hub.disable_ports([other_port, relay_port])
    time.sleep(dual_panel_config["hub_switch_settle_s"])
    _stream_panel_state["hub"] = hub


def exit_stream_panel(dual_panel_config, stream_name):
    """The 'exit' half - see enter_stream_panel's docstring."""
    hub = _stream_panel_state["hub"]
    try:
        LEDPanel.reset()
    except Exception:
        pass
    _dual_panel_primed["primed"] = False
    _dual_panel_primed["switch_time_ms"] = None
    _dual_panel_primed["scan_direction"] = None
    hub.disconnect()
    _stream_panel_state["hub"] = None


@contextmanager
def switched_to_stream_panel(dual_panel_config, stream_name):
    # Keep the existing docstring here (today's lines 293-305) exactly as
    # it is - don't touch a word of it.
    if dual_panel_config is None:
        yield
        return

    if PANEL_CONNECTION["mode"] == "remote":
        from engine import panel_rpc_client
        panel_rpc_client.dual_panel_enter_stream_panel(dual_panel_config, stream_name)
        try:
            yield
        finally:
            panel_rpc_client.dual_panel_exit_stream_panel(dual_panel_config, stream_name)
        return

    enter_stream_panel(dual_panel_config, stream_name)
    try:
        yield
    finally:
        exit_stream_panel(dual_panel_config, stream_name)
```

Note the `stream_name` parameter is unused inside `exit_stream_panel`'s
body itself (the hub to disconnect is whatever `_stream_panel_state`
already holds) - it's kept in the signature anyway so both this function
and its remote RPC counterpart share one call shape.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/engine/test_dual_panel_control.py -v`
Expected: PASS (all tests, old and new)

- [ ] **Step 5: Commit**

```bash
git add engine/dual_panel_control.py tests/engine/test_dual_panel_control.py
git commit -m "feat: gate dual-panel hardware calls behind a local/remote mode"
```

---

### Task 4: `tools/panel_server/panel_server_stdio.py` - the Windows-side script

**Files:**
- Create: `tools/panel_server/__init__.py` (empty)
- Create: `tools/panel_server/panel_server_stdio.py`
- Test: `tests/engine/test_panel_server_stdio.py`

**Interfaces:**
- Consumes: `LEDPanel._run`/`_query` (existing), `dual_panel_control.turn_all_leds_on`/`turn_all_leds_off`/`start_scanning`/`stop_scanning`/`enter_stream_panel`/`exit_stream_panel` (existing + Task 3).
- Produces: `handle_request(line: str, methods: dict = METHODS) -> dict` (pure framing logic, used by its own tests and by `main()`), `METHODS: dict`, `main() -> None`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/engine/test_panel_server_stdio.py
import json

from tools.panel_server.panel_server_stdio import handle_request


def test_handle_request_dispatches_to_the_matching_method_and_returns_result():
    methods = {"double": lambda args: args[0] * 2}
    line = json.dumps({"id": 1, "method": "double", "args": [21]})

    response = handle_request(line, methods=methods)

    assert response == {"id": 1, "result": 42}


def test_handle_request_returns_an_error_for_an_unknown_method():
    response = handle_request(
        json.dumps({"id": 2, "method": "nope", "args": []}), methods={}
    )
    assert response == {"id": 2, "error": "Unknown method: nope"}


def test_handle_request_catches_an_exception_from_the_method_and_returns_an_error():
    def boom(args):
        raise RuntimeError("panel exploded")

    response = handle_request(
        json.dumps({"id": 3, "method": "boom", "args": []}),
        methods={"boom": boom},
    )
    assert response == {"id": 3, "error": "panel exploded"}


def test_handle_request_defaults_args_to_an_empty_list_when_missing():
    methods = {"noop": lambda args: args}
    response = handle_request(
        json.dumps({"id": 4, "method": "noop"}), methods=methods
    )
    assert response == {"id": 4, "result": []}


def test_methods_table_has_exactly_the_expected_entries():
    from tools.panel_server.panel_server_stdio import METHODS
    assert set(METHODS.keys()) == {
        "led_panel_run", "led_panel_query",
        "dual_panel_turn_all_leds_on", "dual_panel_turn_all_leds_off",
        "dual_panel_start_scanning", "dual_panel_stop_scanning",
        "dual_panel_enter_stream_panel", "dual_panel_exit_stream_panel",
    }
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/engine/test_panel_server_stdio.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'tools.panel_server.panel_server_stdio'`

- [ ] **Step 3: Write the implementation**

```python
# tools/panel_server/__init__.py
# (empty file)
```

```python
# tools/panel_server/panel_server_stdio.py
"""Runs on the Windows machine the LED-panel hardware is physically
attached to - the remote end of engine/panel_rpc_client.py's ssh
subprocess. See docs/superpowers/specs/2026-09-22-orin-panel-rpc-split-design.md.

Reads newline-delimited JSON requests from stdin, dispatches each to one
of the plain LEDPanel/dual_panel_control functions below (the exact same
hardware code this app already uses when running entirely on one Windows
machine - nothing here is new hardware logic), and writes a
newline-delimited JSON response to stdout for each one.

stdout is reserved EXCLUSIVELY for the JSON response stream - all of this
script's own logging goes to stderr (logging's own default stream), and
engine/led_panel.py's LEDPanel._run() redirects LED-Panel.exe's own
stdout/stderr to DEVNULL specifically so it can never write into this
same fd and corrupt a response mid-stream.
"""

import json
import logging
import sys

from engine import dual_panel_control
from engine.led_panel import LEDPanel

_logger = logging.getLogger(__name__)

METHODS = {
    "led_panel_run": lambda args: LEDPanel._run(*args),
    "led_panel_query": lambda args: LEDPanel._query(*args),
    "dual_panel_turn_all_leds_on": lambda args: dual_panel_control.turn_all_leds_on(*args),
    "dual_panel_turn_all_leds_off": lambda args: dual_panel_control.turn_all_leds_off(*args),
    "dual_panel_start_scanning": lambda args: dual_panel_control.start_scanning(*args),
    "dual_panel_stop_scanning": lambda args: dual_panel_control.stop_scanning(*args),
    "dual_panel_enter_stream_panel": lambda args: dual_panel_control.enter_stream_panel(*args),
    "dual_panel_exit_stream_panel": lambda args: dual_panel_control.exit_stream_panel(*args),
}


def handle_request(line, methods=METHODS):
    """Pure framing logic (no I/O) - parses one request line, dispatches
    it, and returns the response dict main() will serialize. `methods` is
    a parameter (not a hardcoded lookup of the module-level METHODS) so
    tests can inject a fake dispatch table with no real hardware."""
    request = json.loads(line)
    request_id = request["id"]
    method = methods.get(request["method"])
    if method is None:
        return {"id": request_id, "error": "Unknown method: {}".format(request["method"])}
    try:
        result = method(request.get("args", []))
    except Exception as exc:
        return {"id": request_id, "error": str(exc)}
    return {"id": request_id, "result": result}


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        response = handle_request(line)
        sys.stdout.write(json.dumps(response) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/engine/test_panel_server_stdio.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Run the full test suite to confirm no regressions anywhere**

Run: `.venv\Scripts\python.exe -m pytest -v`
Expected: PASS (every test in the suite, including Tasks 1-3's)

- [ ] **Step 6: Commit**

```bash
git add tools/panel_server/__init__.py tools/panel_server/panel_server_stdio.py tests/engine/test_panel_server_stdio.py
git commit -m "feat: add the Windows-side stdio panel-control server script"
```

---

### Task 5: Wire it up in `main.py` + deployment docs

**Files:**
- Modify: `main.py`
- Modify: `requirements.txt`
- Create: `tools/panel_server/README.md`

**Interfaces:**
- Consumes: `engine.led_panel.configure_panel_connection` (Task 2), `engine.panel_rpc_client.configure` (Task 1).
- Produces: nothing further downstream - this is the last task.

- [ ] **Step 1: Wire `main.py`**

```python
# main.py
import sys

import pyqtgraph as pg
import pyrealsense2 as rs
from PySide6.QtWidgets import QApplication

from gui.main_window import MainWindow
from state.gui_state import load_gui_state
from settings import load_settings
from engine.led_panel import configure_panel_connection
from engine import panel_rpc_client


def main():
    pg.setConfigOptions(antialias=True)

    app = QApplication(sys.argv)
    ctx = rs.context()
    gui_state = load_gui_state()
    settings = load_settings()

    configure_panel_connection(settings["panel_connection"])
    if settings["panel_connection"]["mode"] == "remote":
        panel_rpc_client.configure(
            settings["panel_connection"]["ssh_user"],
            settings["panel_connection"]["ssh_host"],
            settings["panel_connection"]["remote_repo_path"],
        )

    window = MainWindow(ctx, gui_state, settings)
    window.showMaximized()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Verify by import (no hardware/display needed for this check)**

Run: `.venv\Scripts\python.exe -c "import ast; ast.parse(open('main.py').read())"`
Expected: no output (syntax is valid) - `main.py` itself isn't unit-tested
(it needs a real display/QApplication, same as before this change), so
this is just a syntax sanity check, not a behavior test.

- [ ] **Step 3: Add Orin/Windows split notes to `requirements.txt`**

Add this comment block at the very top of `requirements.txt`, above the
existing `pyrealsense2==...` line:

```
# --- Running on an NVIDIA Orin (aarch64 Linux) instead of Windows, with
# the LED-panel hardware left on a separate Windows machine (see
# docs/superpowers/specs/2026-09-22-orin-panel-rpc-split-design.md) ---
# - pyrealsense2 has no official PyPI wheel for Linux ARM64 - build
#   librealsense from source with Python bindings (Intel documents this
#   for Jetson boards) instead of `pip install pyrealsense2`.
# - pywin32 and brainstem (below) are only ever needed on the Windows
#   machine the LED-panel hardware is attached to - skip both here.
```

- [ ] **Step 4: Write `tools/panel_server/README.md`**

```markdown
# Panel server (Orin/Windows split)

Only relevant if `settings.yaml`'s `panel_connection.mode` is `remote` -
i.e. the camera + GUI run on a different machine (e.g. an NVIDIA Orin)
than the one the LED-panel hardware is physically attached to. See
`docs/superpowers/specs/2026-09-22-orin-panel-rpc-split-design.md` for the
full design.

## One-time setup on the Windows machine (the one with the LED panel)

1. Enable OpenSSH Server: Settings -> Optional Features -> Add a feature
   -> OpenSSH Server -> Install.
2. Set up key-based login for the other machine's user (so connecting
   doesn't prompt for a password) - copy that user's public key into
   `C:\Users\<windows-user>\.ssh\authorized_keys`.
3. Make sure this same repo is checked out somewhere on this machine, and
   `LED-Panel.exe` is on PATH (and `brainstem`/the Acroname hub is set up,
   for dual-panel setups) - exactly what running this app locally on
   Windows already needs.

## Every time you want to run in remote mode

Nothing needs to be started ahead of time on the Windows machine - the
other machine's own app starts the panel server itself over `ssh` the
first time it needs to touch the panel. Just set, on the machine running
the GUI:

```yaml
panel_connection:
  mode: remote
  ssh_user: <windows-user>
  ssh_host: <windows machine's address on the shared network>
  remote_repo_path: <where this repo lives on the Windows machine, e.g. C:/Users/<user>/optical_sync_gui_generic>
```

## Troubleshooting

- "Panel server connection lost" - the ssh connection dropped or the
  Windows machine's sshd isn't reachable; restart the app after
  confirming `ssh <ssh_user>@<ssh_host>` works on its own from a terminal.
- Any `LEDPanel command failed after 3 retries` error means the SAME
  thing it would running locally on Windows - it made it all the way to
  `LED-Panel.exe`, which is still failing for a real hardware reason
  (check the physical panel/USB connection on the Windows machine).
```

- [ ] **Step 5: Run the full test suite one more time**

Run: `.venv\Scripts\python.exe -m pytest -v`
Expected: PASS (every test in the suite)

- [ ] **Step 6: Commit**

```bash
git add main.py requirements.txt tools/panel_server/README.md
git commit -m "feat: wire up remote panel-connection mode in main.py, add deployment docs"
```

---

## Manual verification (not automated - needs real hardware on both machines)

Once all 5 tasks are merged, before trusting this on a real rig:

1. On the Windows machine: confirm `ssh <ssh_user>@<ssh_host> python3 -u <remote_repo_path>/tools/panel_server/panel_server_stdio.py` starts and blocks waiting on stdin (Ctrl+C to stop it) - confirms SSH/Python/paths are all correct before involving the Orin at all.
2. On the Orin: set `panel_connection.mode: remote` with real `ssh_user`/`ssh_host`/`remote_repo_path`, run the app, and step through ROI Select or Calibration for a single-panel setup - confirm `LEDPanel.all_leds_on()`/`all_leds_off()` visibly control the real panel.
3. For a dual-panel rig: run through Calibration (exercises `switched_to_stream_panel`'s remote enter/exit) then Threshold Tuning's Start/Stop (exercises `start_scanning`/`stop_scanning`) and confirm both panels still step in lockstep exactly as they do single-machine today.
4. Kill the SSH connection mid-session (e.g. disable Windows' wifi briefly) and confirm the next panel command raises a clear `RuntimeError` instead of hanging or silently doing nothing.
