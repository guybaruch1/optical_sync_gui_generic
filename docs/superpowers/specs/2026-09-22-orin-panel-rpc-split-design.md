# Orin/Windows Split: Remote LED-Panel Control — Design

## Context

Today the whole wizard runs on one Windows machine: the RealSense camera
(`pyrealsense2`), the GUI (PySide6), and the LED-panel hardware (Image
Engineering's `LED-Panel.exe` CLI, plus, in dual-panel setups, the Acroname
USB hub and the shared trigger relay) are all local to that one process.

The goal is to run the camera + GUI + sync measurement on an NVIDIA Orin
(aarch64 Linux) instead, while the LED-panel hardware stays attached to a
Windows machine (`LED-Panel.exe`'s vendor SDK is Windows-only - confirmed
from Image Engineering's own API manual and the x86 Windows installer; no
Linux/ARM build is known to exist, and the SDK only documents the C++ class
surface, not the underlying wire protocol, so reimplementing it directly
isn't realistic). Since the actual optical-sync measurement is computed
entirely from the Orin's own camera timestamps, network latency on the
panel-control path doesn't affect measurement accuracy - only start/stop/
configuration commands ever cross the network, never per-frame data.

**Hard requirement: one codebase, not a fork.** Running the whole app on a
single Windows box (today's setup) must keep working byte-for-byte
unchanged. The split must be an opt-in mode, not a rewrite.

**Transport: SSH only, no separate server/port to manage.** The Orin's own
process spawns the Windows-side control script over `ssh` as a subprocess
and talks to it over that subprocess's own stdin/stdout - there is no
listening TCP port anywhere, not even on loopback, and no separate "start
the server, then open a tunnel" sequence to remember. See §2.

## 1. Where the hardware chokepoints actually are

Two hardware-facing files own every real device touch in this app:

- **`engine/led_panel.py`** - `LEDPanel._run(args)` / `LEDPanel._query(args)`
  are the ONLY two places `LED-Panel.exe` is ever invoked. Every one of
  `LEDPanel`'s ~20 public static methods (`start`, `stop`, `all_leds_on`,
  `set_mode`, `set_speed_ms`, `get_current_led`, ...) funnels through one of
  these two. `LEDPanel` is imported directly by GUI pages
  (`roi_select_page.py`, `calibration_page.py`, `threshold_tuning_page.py`,
  `live_session_page.py`) and by `engine/session_engine.py` - not only
  through `dual_panel_control.py`.
- **`engine/dual_panel_control.py`** - centralizes the single-vs-dual-panel
  branch behind 5 public entry points (`turn_all_leds_on`,
  `turn_all_leds_off`, `start_scanning`, `stop_scanning`,
  `switched_to_stream_panel`). When `dual_panel_config is None` (the common,
  single-panel case), every one of these just calls plain `LEDPanel.*`
  methods - so once `LEDPanel._run`/`_query` are network-transparent, the
  entire single-panel path through this file is *already* remote-capable
  with zero changes to this file. Only the `dual_panel_config is not None`
  branch touches hardware `LEDPanel` can't reach on its own: the Acroname
  hub (`engine/acroname_hub.py`) and the shared trigger relay
  (`_relay_on`/`_relay_off`, plus their keepalive thread).

This means the remoting boundary doesn't need to touch the actual
hardware-timing logic (the double-arm priming fix, the relay-is-a-gate
behavior, the `_dual_panel_lock`, `--stop`-vs-`--reset` distinction, etc.) -
all of that keeps running, unmodified, wherever the hardware physically is.

**`_query` is diagnostics-only, never on the live path.** Grepping for
`LEDPanel.is_running`/`get_current_led`/`get_mode`/`get_trigger_mode`/
`get_camera_trigger*`/`get_stop_trigger*` shows every call site is under
`tools/dual_panel_diag/` - the real GUI/engine runtime (`dual_panel_control.py`,
every GUI page, `session_engine.py`) only ever calls `_run`-based methods.
This matters for §2: `_query`'s "needs a real native Windows console
attached" requirement (it reads `LED-Panel.exe`'s output straight out of
the console screen buffer via `pywin32`, since redirected stdout gets
nothing) is therefore never a constraint on the Orin's remote-control path -
only on someone directly running those diagnostic scripts on the Windows
box itself, exactly as today.

## 2. Architecture: SSH-spawned stdio server

- **Windows side (hardware-attached machine)** needs: OpenSSH Server
  enabled (Settings -> Optional Features -> OpenSSH Server - built into
  Windows 10/11) with key-based login set up for the Orin (no password
  prompts), the repo checked out, and `LED-Panel.exe` on PATH (plus
  `brainstem`/Acroname hub only for dual-panel setups) - i.e. everything it
  already needs today, plus SSH access. No server process needs to be
  started ahead of time.

- A new script, **`tools/panel_server/panel_server_stdio.py`**, imports
  `engine.led_panel` and `engine.dual_panel_control` exactly as they exist
  today. It reads newline-delimited JSON requests from stdin in a loop and
  writes newline-delimited JSON responses to stdout:
  ```
  request:  {"id": 7, "method": "led_panel_run", "args": ["--start"]}
  response: {"id": 7, "result": null}
  # or, on failure:
  response: {"id": 7, "error": "LEDPanel command failed after 3 retries: ..."}
  ```
  Registered methods: `led_panel_run`, `led_panel_query` (kept for
  completeness/future diagnostics, though nothing on the live path calls it
  - see §1's `_query` note - and it inherits the same "needs a real console"
  caveat, so it's expected to fail when invoked this way), plus
  `dual_panel_turn_all_leds_on`, `dual_panel_turn_all_leds_off`,
  `dual_panel_start_scanning`, `dual_panel_stop_scanning`,
  `dual_panel_enter_stream_panel`, `dual_panel_exit_stream_panel` (see §4).
  All of the script's own logging goes to stderr (the default for
  `logging.StreamHandler`), never stdout, and every response write is
  flushed immediately - stdout is reserved exclusively for the JSON
  response stream.

- **Orin side**: a new `engine/panel_rpc_client.py` lazily spawns
  ```bash
  ssh <ssh_user>@<ssh_host> python3 -u <remote_repo_path>/tools/panel_server/panel_server_stdio.py
  ```
  as a single long-lived `subprocess.Popen` (stdin/stdout as pipes), reused
  for the rest of the process's life and closed on app exit. A module-level
  lock serializes calls (writing a request and reading its matching
  response must be atomic per caller, since concurrent GUI-thread/
  preview-thread calls would otherwise interleave on the same pipe - the
  existing `_dual_panel_lock` in `dual_panel_control.py` already serializes
  dual-panel calls for a different reason, but plain single-panel `LEDPanel`
  calls aren't otherwise serialized today, so this lock is new). Response
  ids are checked against the request just sent as a cheap corruption
  guard, even though calls are strictly one-at-a-time. A `Fault`-equivalent
  `{"error": ...}` response is raised as a plain `RuntimeError` with that
  message (matching `LEDPanel._run`'s own raise-after-retries convention);
  the subprocess exiting or a broken pipe raises a clear
  `RuntimeError("Panel server connection lost - ...")` - no silent retry,
  matching this codebase's existing "fail loudly" convention (see
  `ContinuousCapture.start()`'s own no-`can_resolve()`-pre-check reasoning).
  Reconnecting after a drop means restarting the app (see §7).

- **`settings.yaml`** gains:
  ```yaml
  panel_connection:
    mode: local          # "local" (default, today's behavior) or "remote"
    ssh_host: ""          # Windows machine's address, only used when mode: remote
    ssh_user: ""
    remote_repo_path: ""  # where this repo lives on the Windows machine
  ```
  `mode: local` is the default specifically so nothing about today's
  single-machine Windows setup changes unless this is edited.

  Neither `engine/led_panel.py` nor `engine/dual_panel_control.py` currently
  takes any settings dependency at all (`LEDPanel` is a pure CLI wrapper
  with plain class attributes like `exe_name`/`cmd_timeout_s`;
  `dual_panel_config` is threaded in explicitly by callers, not read from
  settings itself). To avoid making either file load `settings.yaml` on its
  own, `main.py` calls a new
  `engine.led_panel.configure_panel_connection(settings["panel_connection"])`
  once at startup (same spot it already calls `pg.setConfigOptions` before
  constructing anything) - this sets a module-level dict in
  `engine/led_panel.py` that both it and `dual_panel_control.py` (via
  `from engine.led_panel import PANEL_CONNECTION`) read from. Defaults to
  `{"mode": "local"}` if `configure_panel_connection` is never called (e.g.
  in tests), so existing tests that import these modules directly keep
  working unchanged.

  Why SSH alone, with no separate server+tunnel: the Windows-side process
  only ever needs to exist for the life of one Orin session, and spawning
  it as the remote end of an `ssh` command means the Orin's own app owns
  its whole lifecycle (start it, detect it dying, close it on exit) instead
  of relying on a human (or a scheduled task) to have started a separate
  server ahead of time and separately opened a port-forward. SSH's own
  authentication and encryption cover the channel - no new auth code needed,
  and no port is ever exposed on the wifi network at all, not even on
  loopback.

## 3. `engine/led_panel.py` changes

`_run` and `_query` each gain one guard at the top:

```python
if PANEL_CONNECTION["mode"] == "remote":
    return panel_rpc_client.led_panel_run(args)   # or led_panel_query
```

Everything below that guard - the existing `check_call`/retry/timeout logic
- is untouched, with one small addition: `_run`'s `check_call` now passes
`stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL`. This is unconditional,
not remote-mode-specific: `_run`'s output was never read by any caller even
today, so discarding it changes nothing locally - but it matters once
`panel_server_stdio.py` runs `_run` as a subprocess of a Python process
whose own stdout is the JSON-response pipe back to the Orin. Without this,
`LED-Panel.exe` would inherit that same stdout fd and any output it printed
would land directly in the middle of the JSON response stream, corrupting
it. (`_query` is unaffected - it already deliberately avoids redirection,
and per §1 is never invoked from the remote path anyway.)

Because every other method in the file (and every direct `LEDPanel.*`
caller elsewhere in the app) only ever reaches hardware through `_run`/
`_query`, this one change makes the entire single-panel path, wherever it's
called from, transparently remote - no other file needs to know or care.

## 4. `engine/dual_panel_control.py` changes

Each of the 4 plain functions gains a guard, only on the
`dual_panel_config is not None` branch (the `None`/single-panel branch is
untouched - it's already covered by §3):

```python
def start_scanning(switch_time_ms, scan_direction, dual_panel_config):
    if dual_panel_config is None:
        ...unchanged...
    elif PANEL_CONNECTION["mode"] == "remote":
        return panel_rpc_client.dual_panel_start_scanning(
            switch_time_ms, scan_direction, dual_panel_config
        )
    else:
        ...unchanged existing dual-panel body...
```

Same shape for `turn_all_leds_on`, `turn_all_leds_off`, `stop_scanning`. The
existing dual-panel bodies - the lock, the priming dict, the double-arm
sequence, `_run_on_both_panels`, `_relay_on`/`_relay_off` - are never edited;
in remote mode they simply don't run on the Orin at all, because the whole
call is redirected to the Windows-side stdio server, which runs that exact
same unmodified code locally.

`switched_to_stream_panel` is a generator-based context manager, so it can't
delegate as a single call the way the other 4 do - its body needs to keep
running across whatever the caller does inside the `with` block. It's
refactored (behavior-preserving) into two halves so both the local and
remote cases can share the structure:

```python
@contextmanager
def switched_to_stream_panel(dual_panel_config, stream_name):
    if dual_panel_config is None:
        yield
        return
    if PANEL_CONNECTION["mode"] == "remote":
        panel_rpc_client.dual_panel_enter_stream_panel(dual_panel_config, stream_name)
        try:
            yield
        finally:
            panel_rpc_client.dual_panel_exit_stream_panel(dual_panel_config, stream_name)
        return
    ...unchanged existing local hub.connect()/enable_ports/.../hub.disconnect() body...
```

On the **stdio server**, `dual_panel_enter_stream_panel`/
`dual_panel_exit_stream_panel` are two new plain functions holding the
connected hub object in a module-level variable between the two JSON
requests (mirroring `_relay_connection`'s existing module-level-state
pattern) - this is the one piece of genuinely new orchestration code this
design adds, since a generator-based context manager can't be "entered" and
"left open" across two separate round-trips. Their bodies are a direct
split of `switched_to_stream_panel`'s existing try/yield/finally into two
functions, not new logic - `switched_to_stream_panel`'s local (non-remote)
branch is refactored to call these same two halves too, so the enter/exit
behavior has exactly one implementation regardless of which branch runs it.

## 5. Dependencies and deployment notes

- Orin's `requirements.txt` drops `pywin32` and `brainstem` (Windows/Acroname-
  hub-only; already lazily imported today, so no code depends on them being
  present) and needs `pyrealsense2` built from source for aarch64 - there is
  no official PyPI wheel for Linux ARM64; Intel documents the librealsense
  build process for Jetson boards. It needs an `ssh` client, which is
  effectively always already present on Linux.
- The Windows machine needs everything it needs today (`LED-Panel.exe` on
  PATH, `pywin32`, and `brainstem` only if dual-panel), plus OpenSSH Server
  enabled and a key-based login set up for the Orin (§2) - nothing needs to
  be manually started ahead of time.
- Both machines must be reachable over the same wifi/LAN for the `ssh`
  connection; `ssh_host`/`ssh_user`/`remote_repo_path` in `settings.yaml`
  are static config, no discovery.

## 6. Testing

- `tests/engine/test_led_panel.py`: new cases for `_run`/`_query`'s remote
  branch, mocking `panel_rpc_client` - verifies the guard dispatches
  correctly and that a `panel_rpc_client` `RuntimeError` propagates
  unchanged; a case confirming `_run`'s local `check_call` is invoked with
  `stdout=DEVNULL, stderr=DEVNULL`.
- `tests/engine/test_dual_panel_control.py`: new cases for each of the 4
  functions' remote branch and for `switched_to_stream_panel`'s remote
  enter/exit, mocking `panel_rpc_client` - same style as the existing
  local/dual-panel branch tests (mocking `_run_on_both_panels`/`_relay_on`/
  `_relay_off`/`LEDPanel`).
- New `tests/engine/test_panel_rpc_client.py`: mocks `subprocess.Popen` (a
  fake stdin/stdout pair) to verify request/response framing, id matching,
  `{"error": ...}` -> `RuntimeError` translation, and a closed/broken pipe
  -> `RuntimeError` translation.
- New `tests/engine/test_panel_server_stdio.py`: tests the script's own
  dispatch logic (parse a request line, call the matching registered
  function, serialize its result or exception) against a fake dispatch
  table - pure framing logic, no real hardware or subprocess needed.
- `panel_server_stdio.py`'s actual wiring to `LEDPanel`/`dual_panel_control`
  gets no automated tests beyond that - same "hardware-only, no tests by
  design" bucket as `engine/led_panel.py`/`engine/session_engine.py` - but
  is smoke-tested manually (run it locally, pipe a couple of hand-typed
  JSON request lines into it with real hardware attached, confirm the
  responses).

## 7. Explicitly out of scope

- **No automatic reconnect on a dropped SSH connection mid-session.** If
  the `ssh` subprocess dies (network hiccup, Windows machine sleeps, etc.),
  the next panel command raises `RuntimeError("Panel server connection
  lost - ...")` and stays failed - recovering means restarting the Orin
  app. Not solved here.
- **Single-client assumption.** The stdio server's module-level state
  (relay connection, priming flag, the new enter/exit hub-handle variable)
  assumes one Orin talking to it at a time, matching the existing
  single-process assumption `_dual_panel_lock` already relies on.
- **Orin display/GUI mechanics** (X11/Wayland/remote desktop for viewing
  the PySide6 wizard on the Orin) - a deployment detail, not part of this
  design.
- **Auto-discovery of the Windows machine's address** - `ssh_host`/
  `ssh_user`/`remote_repo_path` are explicit static config; nothing in the
  app discovers them.
