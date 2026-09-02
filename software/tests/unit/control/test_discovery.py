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

import errno

import pytest
import serial

from fluidics.control.controller import FluidController
from fluidics.errors import DeviceError
from fluidics.control.discovery import (DeviceNotFoundError, find_serial_port,
                                        open_serial_port)
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


def test_a_missing_device_is_a_device_error():
    """One family, one bring-up dialog: the entry points catch DeviceError
    and get the missing-device case with it."""
    from fluidics.control.discovery import DeviceNotFoundError
    from fluidics.errors import DeviceError
    assert issubclass(DeviceNotFoundError, DeviceError)


class TestThePortIsClaimed:
    """Two programs reading one port split the frames between them and both
    get corrupt data. That is what the MCU's "not enough input bytes for
    length code" bursts were: a 64-minute session logged 106 of them, all
    inside two 7-second windows, each ending when the other process died.
    """

    def _serial_that_cannot_lock(self, monkeypatch):
        def refuse(port, exclusive=None, **kwargs):
            raise serial.SerialException(
                11, f"Could not exclusively lock port {port}: [Errno 11] "
                    "Resource temporarily unavailable")
        monkeypatch.setattr("fluidics.control.discovery.serial.Serial", refuse)

    def test_the_port_is_opened_for_this_process_alone(self, monkeypatch):
        seen = {}
        monkeypatch.setattr("fluidics.control.discovery.serial.Serial",
                            lambda port, **kwargs: seen.update(kwargs) or object())
        open_serial_port("/dev/ttyFAKE", "Widget", baudrate=9600)
        assert seen["exclusive"] is True

    def test_a_port_someone_else_holds_names_the_remedy(self, monkeypatch):
        self._serial_that_cannot_lock(monkeypatch)
        with pytest.raises(DeviceError, match="already open in another program"):
            open_serial_port("/dev/ttyFAKE", "Widget", baudrate=9600)

    def test_a_lock_failure_that_is_not_contention_keeps_its_own_error(
            self, monkeypatch):
        """pyserial prefixes every flock failure with "Could not exclusively
        lock" -- ENOLCK on a filesystem that cannot lock, EINTR from a
        signal. Reading those as "another program has it" would send the
        operator hunting for a process that does not exist."""
        def refuse(port, exclusive=None, **kwargs):
            raise serial.SerialException(
                errno.ENOLCK, f"Could not exclusively lock port {port}: "
                              "[Errno 37] No locks available")
        monkeypatch.setattr("fluidics.control.discovery.serial.Serial", refuse)
        with pytest.raises(serial.SerialException, match="No locks available"):
            open_serial_port("/dev/ttyFAKE", "Widget", baudrate=9600)

    def test_an_ordinary_port_failure_is_not_relabelled(self, monkeypatch):
        """A dead port is not a busy one."""
        def refuse(port, **kwargs):
            raise serial.SerialException("could not open port: no such device")
        monkeypatch.setattr("fluidics.control.discovery.serial.Serial", refuse)
        with pytest.raises(serial.SerialException, match="no such device"):
            open_serial_port("/dev/ttyFAKE", "Widget", baudrate=9600)

    def test_the_mcu_goes_through_it(self, monkeypatch):
        fake_ports(monkeypatch, [("/dev/ttyACM9", "AAA")])
        self._serial_that_cannot_lock(monkeypatch)
        controller = FluidController("AAA")
        try:
            with pytest.raises(DeviceError, match="Fluid controller"):
                controller.begin()
        finally:
            controller.close()

    def test_the_temperature_controller_goes_through_it(self, monkeypatch):
        fake_ports(monkeypatch, [("/dev/ttyUSB9", "BBB")])
        self._serial_that_cannot_lock(monkeypatch)
        with pytest.raises(DeviceError, match="Temperature controller"):
            TCMController(sn="BBB")
