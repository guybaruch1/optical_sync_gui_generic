"""D585/D535 Dedicated-RGB <-> Dual-RGB firmware mode switching.

Ported from the standalone d585_dual_rgb_mode.py script (mechanism
verified against real D585 hardware). Mode is identified by USB product
ID (PID), not device name - see librealsense's d500-factory.cpp /
d500-private.h for the PID tables.

get_mode() is pure (a PID lookup) and unit-tested. switch_mode()/
wait_for_reenumeration()/ensure_mode() talk to real hardware
(debug-protocol register write + hardware_reset(), which disconnects/
reconnects the device over USB) and are untested by design, the same
convention this project already uses for engine/session_engine.py and
engine/led_panel.py.
"""

import time

import pyrealsense2 as rs

MWD_OPCODE = 0x02
MODE_REG_START_ADDR = 0x80000064
MODE_REG_END_ADDR = 0x80000068
MODE_DEDICATED_RGB = 0
MODE_DUAL_RGB = 1

DUAL_RGB_PIDS = {"0C01", "0C04", "0C07"}
DEDICATED_RGB_PIDS = {"0C02", "0C05", "0C08"}

REENUMERATION_TIMEOUT_S = 15
REENUMERATION_POLL_INTERVAL_S = 0.5


def get_mode(device):
    """Returns 'dual', 'dedicated', or None (PID not recognized as either,
    or the device doesn't report a product ID at all)."""
    if not device.supports(rs.camera_info.product_id):
        return None
    pid = device.get_info(rs.camera_info.product_id)
    if pid in DUAL_RGB_PIDS:
        return "dual"
    if pid in DEDICATED_RGB_PIDS:
        return "dedicated"
    return None


def switch_mode(device, target_mode):
    """Writes the mode register via debug protocol, hardware-resets the
    device, and returns the serial number to re-enumerate against."""
    if not device.supports(rs.camera_info.product_id):
        raise RuntimeError("Device does not report a product ID - cannot determine RGB mode.")
    if not device.is_debug_protocol():
        raise RuntimeError("Device does not support the debug protocol - cannot switch RGB mode.")

    serial = device.get_info(rs.camera_info.serial_number)
    value = MODE_DUAL_RGB if target_mode == "dual" else MODE_DEDICATED_RGB
    data = [
        value & 0xFF,
        (value >> 8) & 0xFF,
        (value >> 16) & 0xFF,
        (value >> 24) & 0xFF,
    ]

    dp = device.as_debug_protocol()
    cmd = dp.build_command(MWD_OPCODE, MODE_REG_START_ADDR, MODE_REG_END_ADDR, 0, 0, data)
    dp.send_and_receive_raw_data(cmd)
    device.hardware_reset()

    return serial


def wait_for_reenumeration(ctx, serial, timeout_s=REENUMERATION_TIMEOUT_S):
    """Polls rs.context() until a device with the given serial number
    reappears (the PID may have changed - that's expected)."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        for dev in ctx.query_devices():
            if dev.get_info(rs.camera_info.serial_number) == serial:
                return dev
        time.sleep(REENUMERATION_POLL_INTERVAL_S)
    raise RuntimeError(
        "Device {!r} did not re-enumerate within {}s after hardware reset.".format(serial, timeout_s)
    )


def ensure_mode(ctx, device, target_mode):
    """Checks the device's current RGB mode and switches it to
    `target_mode` ("dual" or "dedicated") if it's currently the other one.
    Returns a device handle guaranteed to be in `target_mode` (possibly
    re-enumerated, if a switch happened) - a no-op if the device is already
    in that mode. Generalizes the old dual-only ensure_dual_rgb_mode so the
    operator can choose either direction (gui/pages/device_select_page.py's
    2C/3C radio choice), not just force Dual RGB."""
    mode = get_mode(device)
    if mode is None:
        pid = device.get_info(rs.camera_info.product_id)
        raise RuntimeError(
            "Product ID {!r} is not a recognized D535/D585 Dual/Dedicated RGB variant.".format(pid)
        )
    if mode == target_mode:
        return device

    serial = switch_mode(device, target_mode)
    new_device = wait_for_reenumeration(ctx, serial)

    new_mode = get_mode(new_device)
    if new_mode != target_mode:
        new_pid = new_device.get_info(rs.camera_info.product_id)
        raise RuntimeError(
            "Mode switch did not take effect - device re-enumerated with PID={!r} "
            "(expected a {} PID).".format(new_pid, target_mode)
        )
    return new_device
