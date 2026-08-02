"""Diagnostic script - NOT part of the shipped app, no automated tests.

Replays the ORIGINAL confirmed-working docs/acroname_hub.py __main__ demo
sequence, using this app's actual current engine/acroname_hub.py and
engine/led_panel.py (real code, not a stale copy) - reads port numbers/COM
port from settings.yaml's dual_panel: section instead of hardcoding them.

Purpose: confirm the exact known-good sequence still produces visible
continuous LED stepping on this rig RIGHT NOW, as an unambiguous baseline.
If this ALSO fails to step, the problem is environmental (wiring changed,
panel firmware, panel power, etc.) - NOT this app's integration of the
same sequence. If this WORKS, compare against tools/diag_app_start_scanning.py
(the app's actual integrated function) to see where they diverge.

Run from the repo root: python tools/diag_baseline_demo_sequence.py
Watch the physical panels while it runs - one LED should visibly step
across each panel, continuously, after the final relay pulse.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from settings import load_settings
from engine.acroname_hub import AcronameHub
from engine.led_panel import LEDPanel


def main():
    settings = load_settings(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "settings.yaml"))
    dp = settings["dual_panel"]
    stream_a_port = dp["stream_a_panel_port"]
    stream_b_port = dp["stream_b_panel_port"]
    relay_port = dp["relay_port"]
    relay_com_port = dp["relay_com_port"]
    switch_time_ms = 1

    print("Connecting to Acroname hub...")
    hub = AcronameHub()
    hub.disconnect()
    if not hub.try_connect():
        print("FAILED to connect to the Acroname hub.")
        return
    print("Connected. hub is{} connected".format("" if hub.is_connected() else " NOT"))

    print("Switching to stream_a's panel (port {})...".format(stream_a_port))
    hub.enable_ports([stream_a_port], True, delay_in_seconds=0)
    hub.disable_ports([stream_b_port])
    time.sleep(1)
    print("Configuring stream_a's panel: setMode 1, setTime {}ms, setTriggerMode 2, setCameraTrigger 1"
          .format(switch_time_ms))
    LEDPanel.response_time_measurement_mode()
    LEDPanel.set_speed_ms(switch_time_ms)
    LEDPanel.set_trigger_mode(2)
    LEDPanel.set_camera_trigger(True)

    time.sleep(1)
    print("Switching to stream_b's panel (port {}) + relay (port {})...".format(stream_b_port, relay_port))
    hub.enable_ports([stream_b_port, relay_port], True, delay_in_seconds=0)
    hub.disable_ports([stream_a_port])
    time.sleep(1)
    print("Configuring stream_b's panel: setMode 1, setTime {}ms, setTriggerMode 2, setCameraTrigger 1"
          .format(switch_time_ms))
    LEDPanel.response_time_measurement_mode()
    LEDPanel.set_speed_ms(switch_time_ms)
    LEDPanel.set_trigger_mode(2)
    LEDPanel.set_camera_trigger(True)

    print("occupied ports: {}".format(hub.discover_occupied_ports()))
    print("Disconnecting from hub...")
    hub.disconnect()

    print("Pulsing relay on {} to start both panels stepping...".format(relay_com_port))
    import serial
    s = serial.Serial(relay_com_port, 9600, timeout=1)
    time.sleep(2)
    s.write(bytes.fromhex("A00101A2"))
    print("Relay ON sent - WATCH THE PANELS NOW. Should see one LED stepping continuously on each.")
    time.sleep(5)
    s.write(bytes.fromhex("A00100A1"))
    s.close()
    print("Relay OFF sent. Done - report what you observed on the physical panels.")


if __name__ == "__main__":
    main()
