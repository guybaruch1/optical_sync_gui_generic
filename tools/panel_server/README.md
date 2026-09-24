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
4. `remote_python` (below) must point at a Python interpreter that
   actually has this repo's dependencies installed - it is NOT safe to
   assume a bare `python` on PATH does. A bare/system Python found via
   PATH can easily be some other, unrelated install with none of
   `requirements.txt` present, or - worse - a *different* package
   installed under the same import name (e.g. PyPI's `serial` package
   instead of `pyserial`, which imports fine as `import serial` but has
   no `Serial` class, producing a confusing `AttributeError` deep inside
   `dual_panel_control.py`'s relay code instead of an obvious
   `ModuleNotFoundError`). The reliable setup is a dedicated venv in this
   repo checkout:
   ```powershell
   cd C:\path\to\this\repo
   python -m venv .venv
   .venv\Scripts\pip install -r requirements.txt
   ```
   then point `remote_python` at `.../.venv/Scripts/python.exe` (see
   below) instead of plain `python`.

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
  # Defaults to "python" if omitted, but see step 4 above - point this at
  # a venv's python.exe instead, e.g.:
  # remote_python: "C:/path/to/this/repo/.venv/Scripts/python.exe"
  remote_python: python
```

## Troubleshooting

- "Panel server connection lost" - the ssh connection dropped or the
  Windows machine's sshd isn't reachable; restart the app after
  confirming `ssh <ssh_user>@<ssh_host>` works on its own from a terminal.
- Any `LEDPanel command failed after 3 retries` error means the SAME
  thing it would running locally on Windows - it made it all the way to
  `LED-Panel.exe`, which is still failing for a real hardware reason
  (check the physical panel/USB connection on the Windows machine).
