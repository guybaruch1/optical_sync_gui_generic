"""Acroname USB hub control, ported from docs/acroname_hub.py - lets
engine/dual_panel_control.py switch which of 2 physically separate LED
panels (each wired to its own downstream USB port on the hub) is currently
visible to the OS, since LED-Panel.exe always talks to whichever panel is
currently hub-exposed, never a specific one by identity.

No automated tests possible - this needs the real Acroname `brainstem` SDK
plus a physically connected hub, same "no tests by design" bucket as
engine/led_panel.py/engine/session_engine.py (see CLAUDE.md's "Live
Session pipeline" section for that established convention)."""

from functools import wraps
from typing import List, Sequence, Callable

import time


def raise_if_not_connected(method: Callable) -> Callable:
    @wraps(method)
    def inner(self, *method_args, **method_kwargs):
        if not self.is_connected():
            raise RuntimeError("Acroname hub is not connected!")
        return method(self, *method_args, **method_kwargs)
    return inner


class AcronameHub:
    def __init__(self):
        import brainstem
        self._bs = brainstem
        self._hub = None

    @property
    def _usb_location_to_port_number(self):
        return {
            self._bs.stem.USBHub3p:
                {
                    (4, 4): 0,
                    (4, 3): 1,
                    (4, 2): 2,
                    (4, 1): 3,
                    (3, 4): 4,
                    (3, 3): 5,
                    (3, 2): 6,
                    (3, 1): 7,
                },
            self._bs.stem.USBHub2x4:
                {
                    (2, 0): 0,
                    (3, 0): 1,
                    (4, 0): 2,
                    (5, 0): 3,
                }
        }

    def _validate_port_number(self, port_number: int) -> None:
        if port_number < 0 or port_number > self._hub.NUMBER_OF_DOWNSTREAM_USB - 1:
            raise ValueError(f"port number can be only within 0 and {self._hub.NUMBER_OF_DOWNSTREAM_USB - 1}")

    def connect(self) -> None:
        """Connect to the hub. Raises RuntimeError if fails"""
        for cls in [self._bs.stem.USBHub3p, self._bs.stem.USBHub2x4]:
            self._hub = cls()
            result = self._hub.discoverAndConnect(self._bs.link.Spec.USB)
            if result == self._bs.result.Result.NO_ERROR:
                break
        else:
            raise RuntimeError("Failed to connect to a hub. Please check connectivity.")

    def try_connect(self, retry: bool = False) -> bool:
        retries = 0 if retry else 2
        while retries < 3:
            try:
                self.connect()
                return True
            except Exception:
                retries += 1
        return False

    @raise_if_not_connected
    def is_device_attached(self, port: int) -> bool:
        DEVICE_BIT_MASK = 0x800000
        port_state = self._hub.usb.getPortState(port).value
        is_device_attached = bool((port_state & DEVICE_BIT_MASK) >> 23)
        return is_device_attached

    @raise_if_not_connected
    def enable_ports(self,
                     ports: Sequence[int],
                     disable_other_ports: bool = True,
                     delay_in_seconds: float = 1.0
                     ) -> None:
        """Set enable state to provided ports"""

        results = []
        other_ports = []
        if disable_other_ports:
            all_ports = range(self._hub.NUMBER_OF_DOWNSTREAM_USB)
            other_ports = list(set(all_ports) - set(ports))
        NO_ERROR = self._bs.result.Result.NO_ERROR
        # Enable ports
        for port in ports:
            results.append((port, self._hub.usb.setPortEnable(port) == NO_ERROR))
            time.sleep(delay_in_seconds)
        # Disable other ports (if needed)
        for port in other_ports:
            results.append((port, self._hub.usb.setPortDisable(port) == NO_ERROR))
            time.sleep(delay_in_seconds)
        failed_ports = [p for p, result in results if not result]
        if failed_ports:
            raise RuntimeError(f"Failed to interact with ports: {failed_ports}")

    @raise_if_not_connected
    def enable_all_ports(self, delay_in_seconds: int = 1) -> None:
        """Set enable state to all available ports"""
        self.enable_ports(ports=range(self._hub.NUMBER_OF_DOWNSTREAM_USB), disable_other_ports=False,
                          delay_in_seconds=delay_in_seconds)

    @raise_if_not_connected
    def disable_all_ports(self, delay_in_seconds: int = 1) -> None:
        """Set disable state to all available ports"""
        self.enable_ports(ports=[], disable_other_ports=True, delay_in_seconds=delay_in_seconds)

    @raise_if_not_connected
    def get_port_power(self, port: int) -> float:
        """Get port power consumption"""
        self._validate_port_number(port)
        micro_volt = self._hub.usb.getPortVoltage(port)
        micro_curr = self._hub.usb.getPortCurrent(port)
        volt = float(micro_volt.value) / 10.0 ** 6
        amps = float(micro_curr.value) / 10.0 ** 6
        return volt * amps

    def disconnect(self) -> None:
        if self.is_connected():
            self._hub.disconnect()

    def get_port_from_usb_location(self, location_identifier1: int, location_identifier2: int) -> int:
        return self._usb_location_to_port_number[type(self._hub)][(location_identifier1, location_identifier2)]

    @raise_if_not_connected
    def recycle_port(self, port: int, delay_in_seconds: int = 1) -> None:
        """Disable and enable a port with a delay between"""

        self.port_off(port, delay_in_seconds)
        self.port_on(port)

    @raise_if_not_connected
    def port_off(self, port: int, delay_in_seconds: int = 1) -> None:
        """ disable a port with optional delay after """

        self._validate_port_number(port)
        result = self._hub.usb.setPortDisable(port)
        time.sleep(delay_in_seconds)
        if result != self._bs.result.Result.NO_ERROR:
            raise RuntimeError(f"Failed to disable port #{port}: {result}")

    @raise_if_not_connected
    def port_on(self, port: int) -> None:
        """ enable a port """

        self._validate_port_number(port)
        result = self._hub.usb.setPortEnable(port)
        if result != self._bs.result.Result.NO_ERROR:
            raise RuntimeError(f"Failed to enable port #{port}: {result}")

    @raise_if_not_connected
    def discover_occupied_ports(self) -> List[int]:
        occupied_ports = []
        for port in range(self._hub.NUMBER_OF_DOWNSTREAM_USB):
            if self.is_device_attached(port):
                occupied_ports.append(port)
        return occupied_ports

    def is_connected(self) -> bool:
        if self._hub is None:
            return False
        return self._hub.isConnected()

    @property
    @raise_if_not_connected
    def number_of_usb_ports(self) -> int:
        return self._hub.NUMBER_OF_DOWNSTREAM_USB

    @property
    @raise_if_not_connected
    def serial(self) -> int:
        return self._hub.system.getSerialNumber().value

    @raise_if_not_connected
    def disable_ports(self, ports: List[int]) -> None:
        NO_ERROR = self._bs.result.Result.NO_ERROR
        for port in ports:
            res = self._hub.usb.setPortDisable(port)
            if res != NO_ERROR:
                raise RuntimeError(f"Failed to disable Acroname port {port}")

    @raise_if_not_connected
    def disable_ports_data(self, ports: List[int]) -> None:
        if not isinstance(ports, Sequence):
            ports = [ports]
        NO_ERROR = self._bs.result.Result.NO_ERROR
        for port in ports:
            res = self._hub.usb.setDataDisable(port)
            if res != NO_ERROR:
                raise RuntimeError(f"Failed to disable Acroname port {port} data")

    @raise_if_not_connected
    def enable_ports_data(self, ports: List[int]) -> None:
        if not isinstance(ports, Sequence):
            ports = [ports]
        NO_ERROR = self._bs.result.Result.NO_ERROR
        for port in ports:
            res = self._hub.usb.setDataEnable(port)
            if res != NO_ERROR:
                raise RuntimeError(f"Failed to disable Acroname port {port} data")

    @raise_if_not_connected
    def disable_ports_power(self, ports: List[int]) -> None:
        if not isinstance(ports, Sequence):
            ports = [ports]
        NO_ERROR = self._bs.result.Result.NO_ERROR
        for port in ports:
            res = self._hub.usb.setPowerDisable(port)
            if res != NO_ERROR:
                raise RuntimeError(f"Failed to disable Acroname port {port} power")

    @raise_if_not_connected
    def enable_ports_power(self, ports: List[int]) -> None:
        if not isinstance(ports, Sequence):
            ports = [ports]
        NO_ERROR = self._bs.result.Result.NO_ERROR
        for port in ports:
            res = self._hub.usb.setPowerEnable(port)
            if res != NO_ERROR:
                raise RuntimeError(f"Failed to disable Acroname port {port} power")
