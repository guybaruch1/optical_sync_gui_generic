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
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

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
