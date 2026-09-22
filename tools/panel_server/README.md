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
  remote_python: python  # defaults to "python" since the remote machine is always Windows
```

## Troubleshooting

- "Panel server connection lost" - the ssh connection dropped or the
  Windows machine's sshd isn't reachable; restart the app after
  confirming `ssh <ssh_user>@<ssh_host>` works on its own from a terminal.
- Any `LEDPanel command failed after 3 retries` error means the SAME
  thing it would running locally on Windows - it made it all the way to
  `LED-Panel.exe`, which is still failing for a real hardware reason
  (check the physical panel/USB connection on the Windows machine).
