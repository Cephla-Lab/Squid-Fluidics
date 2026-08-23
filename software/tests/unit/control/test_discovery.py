# tests/unit/control/test_discovery.py
"""One port search, one failure shape, for all three serial devices.

Before this, each driver had its own copy of the loop and its own failure:
the syringe pump a bare AttributeError, the temperature controller a
ValueError, the microcontroller IOError('No Controller Found') naming
nothing. These tests pin the shared helper and that every driver actually
routes through it -- an unplugged device must read as an operator problem
(which device, which serial, what is present), never a driver bug.
"""

from types import SimpleNamespace

import pytest

from fluidics.control.controller import FluidController
from fluidics.control.discovery import DeviceNotFoundError, find_serial_port
from fluidics.control.syringe_pump import SyringePump
from fluidics.control.temperature_controller import TCMController


def fake_ports(monkeypatch, ports):
    entries = [SimpleNamespace(device=d, serial_number=sn) for d, sn in ports]
    monkeypatch.setattr("serial.tools.list_ports.comports", lambda: entries)


class TestFindSerialPort:
    def test_finds_the_matching_port(self, monkeypatch):
        fake_ports(monkeypatch, [("/dev/ttyACM0", "AAA"), ("/dev/ttyACM1", "BBB")])
        assert find_serial_port("BBB", "Widget") == "/dev/ttyACM1"

    def test_missing_device_names_everything_the_operator_needs(self, monkeypatch):
        fake_ports(monkeypatch, [("/dev/ttyACM0", "AAA")])
        with pytest.raises(DeviceNotFoundError) as excinfo:
            find_serial_port("ZZZ", "Widget")
        message = str(excinfo.value)
        assert "Widget" in message
        assert "'ZZZ'" in message
        assert "/dev/ttyACM0 (sn AAA)" in message

    def test_no_ports_at_all_says_none(self, monkeypatch):
        fake_ports(monkeypatch, [])
        with pytest.raises(DeviceNotFoundError, match="present: none"):
            find_serial_port("ZZZ", "Widget")

    def test_a_none_serial_number_never_matches(self, monkeypatch):
        """Ports without a USB serial report None. A half-filled config must
        not silently pick whichever of those enumerates first."""
        fake_ports(monkeypatch, [("/dev/ttyACM0", None)])
        with pytest.raises(DeviceNotFoundError):
            find_serial_port(None, "Widget")


class TestDriversRouteThroughIt:
    """Constructing each real driver against an empty bus must fail with
    DeviceNotFoundError before touching any hardware library."""

    def test_syringe_pump(self, monkeypatch):
        fake_ports(monkeypatch, [])
        with pytest.raises(DeviceNotFoundError, match="Syringe pump"):
            SyringePump(sn="GONE", syringe_ul=5000, speed_code_limit=10,
                        waste_port=3)

    def test_temperature_controller(self, monkeypatch):
        fake_ports(monkeypatch, [])
        with pytest.raises(DeviceNotFoundError, match="Temperature controller"):
            TCMController(sn="GONE")

    def test_fluid_controller(self, monkeypatch):
        fake_ports(monkeypatch, [])
        controller = FluidController("GONE")
        try:
            with pytest.raises(DeviceNotFoundError, match="Teensy"):
                controller.begin()
        finally:
            controller.close()
