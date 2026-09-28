import json
import subprocess
import sys
import tempfile
from pathlib import Path

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


# --- Regression test for the missing sys.path bootstrap in
# tools/panel_server/panel_server_stdio.py.
#
# pytest.ini's `pythonpath = .` means every test ABOVE this line (which
# imports tools.panel_server.panel_server_stdio as a normal package import)
# never notices a missing sys.path bootstrap - the repo root is already on
# sys.path for the whole pytest process regardless of what the module
# itself does. But engine/panel_rpc_client.py launches this script as a
# real subprocess by absolute path over ssh (see _ensure_connected()):
# Python then puts the script's OWN directory (tools/panel_server/) at
# sys.path[0], NOT the repo root, so `from engine import ...` raises
# ModuleNotFoundError and the script exits immediately - a real-world
# break `pythonpath = .` can never catch.
#
# This test launches the actual script as a real subprocess, by its real
# file path, from a working directory that is NOT the repo root - the
# exact shape of the real ssh-launched failure - and confirms it starts up
# cleanly (imports succeed, its stdin-read/dispatch loop runs) rather than
# crashing. ---

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT_PATH = REPO_ROOT / "tools" / "panel_server" / "panel_server_stdio.py"


def test_script_starts_and_dispatches_when_launched_from_outside_the_repo():
    # A request for an unknown method - every real METHODS entry needs
    # actual LED-panel/Acroname hardware, so this is the only request that
    # can prove the dispatch loop ran without touching hardware.
    request_line = '{"id": 1, "method": "nonexistent", "args": []}\n'

    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH)],
        input=request_line,
        capture_output=True,
        text=True,
        timeout=10,
        cwd=tempfile.gettempdir(),  # NOT the repo root - proves no cwd dependency
    )

    assert result.returncode == 0, (
        "panel_server_stdio.py exited non-zero when launched from outside the "
        "repo root - stderr: {}".format(result.stderr)
    )
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(lines) == 1, "expected exactly one JSON response line, got: {!r}".format(result.stdout)

    response = json.loads(lines[0])
    assert response["id"] == 1
    assert "error" in response
    assert "nonexistent" in response["error"]
