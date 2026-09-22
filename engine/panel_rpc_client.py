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


def configure(ssh_user, ssh_host, remote_repo_path, remote_python="python"):
    """Called once at startup (main.py) before any remote call is made.

    remote_python defaults to "python", not "python3" - the remote machine
    is by definition always Windows (the whole point of this feature is
    that the LED-panel hardware only works from Windows), and the
    python.org Windows installer creates python.exe/the py launcher, not
    python3.exe. Windows 10/11 actually ships its own python3.exe App
    Execution Alias stub that redirects to the Microsoft Store when no
    real Python 3 install is present, which fails non-interactively with a
    confusing error - "python" avoids that trap entirely.
    """
    _state["config"] = {
        "ssh_user": ssh_user,
        "ssh_host": ssh_host,
        "remote_repo_path": remote_repo_path,
        "remote_python": remote_python,
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
    # ssh itself concatenates everything after the target into ONE string
    # that the REMOTE shell re-splits on spaces - subprocess.Popen's own
    # list-argv protection only guards against the LOCAL shell. A
    # remote_repo_path containing a space (e.g. "C:/Users/someone/scripts/
    # Optical Sync/optical_sync_gui_generic") would otherwise get split
    # apart by the remote shell mid-path. Building the remote side as ONE
    # quoted argument keeps it intact.
    remote_cmd = '"{}" -u "{}"'.format(config["remote_python"], remote_script)
    process = subprocess.Popen(
        [
            "ssh",
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=10",
            "-o", "ServerAliveInterval=5",
            "-o", "ServerAliveCountMax=3",
            target,
            remote_cmd,
        ],
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

        try:
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
        except (json.JSONDecodeError, KeyError) as exc:
            raise RuntimeError(
                "Panel server returned malformed response: {} (line: {})".format(
                    type(exc).__name__, line.strip()
                )
            )


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
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
        _state["process"] = None
