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

## 2. Architecture

- **Windows side (hardware-attached machine)**: a new script,
  `tools/panel_server/panel_rpc_server.py`, imports `engine.led_panel` and
  `engine.dual_panel_control` exactly as they exist today and starts a
  stdlib `xmlrpc.server.SimpleXMLRPCServer` bound to `127.0.0.1` only (never
  the machine's real LAN/wifi address - see §2a), registering:
  - `led_panel_run(args)` -> `LEDPanel._run(args)`
  - `led_panel_query(args)` -> `LEDPanel._query(args)`
  - `dual_panel_turn_all_leds_on(dual_panel_config)`
  - `dual_panel_turn_all_leds_off(dual_panel_config)`
  - `dual_panel_start_scanning(switch_time_ms, scan_direction, dual_panel_config)`
  - `dual_panel_stop_scanning(dual_panel_config)`
  - `dual_panel_enter_stream_panel(dual_panel_config, stream_name)` /
    `dual_panel_exit_stream_panel(dual_panel_config, stream_name)` (see §4)

  `xmlrpc` (stdlib) is chosen over adding a new dependency (Flask/FastAPI)
  or a pub-sub broker (MQTT/ZeroMQ): every one of these calls is already a
  blocking, synchronous, retrying subprocess/serial call, so request/response
  semantics are the right fit, not streaming. `xmlrpc` also automatically
  turns a server-side exception into an `xmlrpc.client.Fault` on the client,
  which is exactly the propagation `LEDPanel._run`'s "raise after exhausting
  retries" convention already relies on.

- **Orin side**: a new `engine/panel_rpc_client.py` wraps a single
  `xmlrpc.client.ServerProxy`, with one function per server method above,
  translating `xmlrpc.client.Fault` back into a plain `RuntimeError` (same
  exception type/convention `LEDPanel`/`dual_panel_control` already raise on
  failure) and translating a connection failure into a clear
  `RuntimeError("Cannot reach panel server at {host}:{port} - ...")` -
  matching this codebase's existing "fail loudly, no silent fallback"
  convention (see `ContinuousCapture.start()`'s own no-`can_resolve()`-
  pre-check reasoning).

- **`settings.yaml`** gains:
  ```yaml
  panel_connection:
    mode: local     # "local" (default, today's behavior) or "remote"
    host: localhost # always localhost - see §2a, the SSH tunnel is what
                     # actually reaches the Windows machine
    port: 8765
  ```
  `mode: local` is the default specifically so nothing about today's single-
  machine Windows setup changes unless this is edited.

  Neither `engine/led_panel.py` nor `engine/dual_panel_control.py` currently
  takes any settings dependency at all (`LEDPanel` is a pure CLI wrapper
  with plain class attributes like `exe_name`/`cmd_timeout_s`;
  `dual_panel_config` is threaded in explicitly by callers, not read from
  settings itself). To avoid making either file load `settings.yaml` on its
  own, `main.py` calls a new `engine.led_panel.configure_panel_connection(settings["panel_connection"])`
  once at startup (same spot it already calls `pg.setConfigOptions` before
  constructing anything) - this sets a module-level dict in
  `engine/led_panel.py` that both it and `dual_panel_control.py` (via
  `from engine.led_panel import PANEL_CONNECTION`) read from. Defaults to
  `{"mode": "local"}` if `configure_panel_connection` is never called (e.g.
  in tests), so existing tests that import these modules directly keep
  working unchanged.

## 2a. Reaching the Windows machine: SSH tunnel, not a bare port

The RPC server is never exposed directly on the wifi network. Instead:

- Windows enables **OpenSSH Server** (Settings -> Optional Features ->
  OpenSSH Server - built into Windows 10/11, nothing extra to install) and
  is set up for key-based login (no password auth), so the Orin can connect
  non-interactively.
- The Orin opens a local-forwarding tunnel before starting a session:
  ```bash
  ssh -N -L 8765:localhost:8765 <windows-user>@<windows-host>
  ```
  This forwards the Orin's own `localhost:8765` to the Windows machine's
  `localhost:8765`, which is exactly why the server binds to `127.0.0.1`
  (§2) and `settings.yaml`'s `panel_connection.host` is always `localhost`
  (§2) - the app itself never needs to know the Windows machine's real
  address; the tunnel is what actually crosses the network.
- This resolves the "no auth/encryption" gap a plain exposed TCP port would
  have, using SSH's existing authentication and encryption instead of any
  new code - the RPC layer itself stays exactly as simple as designed in
  §2, unaware a tunnel exists.
- Operationally, the tunnel and the Windows-side `panel_rpc_server.py`
  process both need to be up before an Orin session starts. Out of scope
  for this design to automate (see §7) - a documented manual step (or a
  simple wrapper script) for now, e.g. a `tools/panel_server/` README
  covering: enabling OpenSSH Server once, starting `panel_rpc_server.py` on
  Windows, and running the `ssh -L` command from the Orin before launching
  `main.py`.

## 3. `engine/led_panel.py` changes

`_run` and `_query` each gain one guard at the top:

```python
if PANEL_CONNECTION["mode"] == "remote":
    return panel_rpc_client.led_panel_run(args)   # or led_panel_query
```

Everything below that guard - the existing `check_call`/retry/timeout logic
- is untouched. Because every other method in the file (and every direct
`LEDPanel.*` caller elsewhere in the app) only ever reaches hardware through
these two functions, this one change makes the entire single-panel path,
wherever it's called from, transparently remote - no other file needs to
know or care.

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
call is redirected to the Windows server, which runs that exact same
unmodified code locally.

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

On the **server**, `dual_panel_enter_stream_panel`/`dual_panel_exit_stream_panel`
are two new plain functions holding the connected hub object in a
module-level variable between the two RPC calls (mirroring
`_relay_connection`'s existing module-level-state pattern) - this is the one
piece of genuinely new orchestration code this design adds, since a
generator-based context manager can't be "entered" and "left open" across
two separate RPC round-trips. Their bodies are a direct split of
`switched_to_stream_panel`'s existing try/yield/finally into two functions,
not new logic - `switched_to_stream_panel`'s local (non-remote) branch is
refactored to call these same two halves too, so the enter/exit behavior has
exactly one implementation regardless of which branch runs it.

## 5. Dependencies and deployment notes

- Orin's `requirements.txt` drops `pywin32` and `brainstem` (Windows/Acroname-
  hub-only; already lazily imported today, so no code depends on them being
  present) and needs `pyrealsense2` built from source for aarch64 - there is
  no official PyPI wheel for Linux ARM64; Intel documents the librealsense
  build process for Jetson boards.
- The Windows panel server needs everything it needs today
  (`LED-Panel.exe` on PATH, `pywin32`, and `brainstem` only if dual-panel),
  plus OpenSSH Server enabled and a key-based login set up for the Orin
  (§2a).
- Both machines must be reachable over the same wifi/LAN for the SSH tunnel
  to be established; beyond that, `host`/`port` in `settings.yaml` are
  always `localhost`/static (§2a) - no discovery needed.

## 6. Testing

- `tests/engine/test_led_panel.py`: new cases for `_run`/`_query`'s remote
  branch, mocking `panel_rpc_client` - verifies the guard dispatches
  correctly and that a `panel_rpc_client` `RuntimeError` propagates
  unchanged.
- `tests/engine/test_dual_panel_control.py`: new cases for each of the 4
  functions' remote branch and for `switched_to_stream_panel`'s remote
  enter/exit, mocking `panel_rpc_client` - same style as the existing
  local/dual-panel branch tests (mocking `_run_on_both_panels`/`_relay_on`/
  `_relay_off`/`LEDPanel`).
- New `tests/engine/test_panel_rpc_client.py`: mocks `xmlrpc.client.ServerProxy`
  to verify `Fault` -> `RuntimeError` translation and connection-error ->
  `RuntimeError` translation.
- `tools/panel_server/panel_rpc_server.py` itself gets no automated tests -
  same "hardware-only, no tests by design" bucket as `engine/led_panel.py`/
  `engine/session_engine.py` - but is smoke-tested manually (start it,
  confirm it registers the expected method names) since it strictly wires
  existing, already-tested functions to `SimpleXMLRPCServer`.

## 7. Explicitly out of scope

- **Auth/encryption on the RPC channel itself** is not this design's job -
  it's delegated entirely to the SSH tunnel (§2a). The RPC layer stays as
  simple as an unauthenticated `localhost`-only server precisely because
  the tunnel is what's actually trusted to cross the network.
- **Automating the tunnel + server startup.** Establishing the `ssh -L`
  tunnel and starting `panel_rpc_server.py` on Windows before a session are
  manual steps (or a simple documented wrapper script) for now - not
  auto-started by `main.py`, not a Windows service, not retried/reconnected
  if the tunnel drops mid-session.
- **Single-client assumption.** The server's module-level state (relay
  connection, priming flag, the new enter/exit hub-handle variable) assumes
  one Orin talking to it at a time, matching the existing single-process
  assumption `_dual_panel_lock` already relies on.
- **Orin display/GUI mechanics** (X11/Wayland/remote desktop for viewing the
  PySide6 wizard on the Orin) - a deployment detail, not part of this
  design.
- **Auto-discovery of the Windows machine's address** - the operator runs
  the `ssh -L` command with the Windows host explicitly; nothing in the app
  discovers it.
