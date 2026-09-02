# tests/unit/control/test_discovery.py
"""One port search, one failure shape, for all three serial devices.

Before this, each driver had its own copy of the loop and its own failure:
the syringe pump a bare AttributeError, the temperature controller a
ValueError, the microcontroller IOError('No Controller Found') naming
nothing. These tests pin the shared helper and that every driver actually
routes through it -- an unplugged device must read as an operator problem
(which device, which serial, what is present), never a driver bug.
"""

import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from fluidics.control.controller import FluidController
from fluidics.control.discovery import (DeviceInUseError, DeviceNotFoundError,
                                        find_serial_port, open_serial_port,
                                        port_holders)
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


class TestExclusiveOpen:
    """Two processes on one port is the failure this prevents. Both read the
    same MCU stream, each gets fragments, and most frames fail to decode --
    a burst of "not enough input bytes for length code" in the run log, and
    a valve command that never sees its completion packet. The port is
    claimed so the second instance is refused instead.
    """

    def _serial_that_cannot_lock(self, monkeypatch):
        import serial

        def refuse(port, exclusive=None, **kwargs):
            raise serial.SerialException(
                11, f"Could not exclusively lock port {port}: [Errno 11] "
                    "Resource temporarily unavailable")

        monkeypatch.setattr("fluidics.control.discovery.serial.Serial", refuse)

    def test_the_port_is_claimed_for_this_process(self, monkeypatch):
        seen = {}

        def record(port, **kwargs):
            seen.update(kwargs, port=port)
            return object()

        monkeypatch.setattr("fluidics.control.discovery.serial.Serial", record)
        open_serial_port("/dev/ttyFAKE", "Widget", baudrate=9600)
        assert seen["exclusive"] is True

    def test_a_port_someone_else_holds_names_the_remedy(self, monkeypatch):
        self._serial_that_cannot_lock(monkeypatch)
        with pytest.raises(DeviceInUseError, match="already open in another program"):
            open_serial_port("/dev/ttyFAKE", "Widget", baudrate=9600)

    def test_other_serial_failures_are_not_relabelled(self, monkeypatch):
        """A dead port is not a busy port -- only the lock failure gets the
        'close the other instance' advice."""
        import serial

        def refuse(port, **kwargs):
            raise serial.SerialException("could not open port: no such device")

        monkeypatch.setattr("fluidics.control.discovery.serial.Serial", refuse)
        with pytest.raises(serial.SerialException, match="no such device"):
            open_serial_port("/dev/ttyFAKE", "Widget", baudrate=9600)

    def test_the_mcu_port_goes_through_it(self, monkeypatch):
        fake_ports(monkeypatch, [("/dev/ttyACM9", "AAA")])
        self._serial_that_cannot_lock(monkeypatch)
        controller = FluidController("AAA")
        try:
            with pytest.raises(DeviceInUseError, match="Fluid controller"):
                controller.begin()
        finally:
            controller.close()

    def test_the_temperature_port_goes_through_it(self, monkeypatch):
        fake_ports(monkeypatch, [("/dev/ttyUSB9", "BBB")])
        self._serial_that_cannot_lock(monkeypatch)
        with pytest.raises(DeviceInUseError, match="Temperature controller"):
            TCMController(sn="BBB")

    def test_the_syringe_pump_is_claimed_too(self, monkeypatch):
        """The port this package does not open itself, and the one where a
        stolen reply costs most: a timeout partway through a plunger move,
        not 60 ms of telemetry."""
        fake_ports(monkeypatch, [("/dev/ttyUSB9", "PUMP")])
        monkeypatch.setattr("fluidics.control.discovery.port_holders",
                            lambda device: [(4242, "python3")])
        with pytest.raises(DeviceInUseError, match="Syringe pump"):
            SyringePump(sn="PUMP", syringe_ul=5000, speed_code_limit=10,
                        waste_port=3)

    def test_a_busy_port_is_a_device_error(self):
        """Same bring-up dialog as a missing device: the entry points catch
        DeviceError and get this with it."""
        from fluidics.errors import DeviceError
        assert issubclass(DeviceInUseError, DeviceError)


class TestNamingWhoHoldsThePort:
    """The advisory lock only stops an opener that asks for it, and the port's
    other user is often a program that does not -- the microscope software's
    own copy of this driver, or a bench script. Then the operator needs a name,
    not a corrupt stream.
    """

    def test_a_held_port_is_refused_by_name(self, monkeypatch):
        monkeypatch.setattr("fluidics.control.discovery.port_holders",
                            lambda device: [(4242, "napari")])
        monkeypatch.setattr("fluidics.control.discovery.serial.Serial",
                            lambda *a, **k: pytest.fail("opened a held port"))
        with pytest.raises(DeviceInUseError, match=r"napari \(pid 4242\)"):
            open_serial_port("/dev/ttyFAKE", "Widget", baudrate=9600)

    def test_an_unheld_port_opens(self, monkeypatch):
        monkeypatch.setattr("fluidics.control.discovery.port_holders",
                            lambda device: [])
        opened = object()
        monkeypatch.setattr("fluidics.control.discovery.serial.Serial",
                            lambda *a, **k: opened)
        assert open_serial_port("/dev/ttyFAKE", "Widget", baudrate=9600) is opened

    @pytest.mark.skipif(not os.path.isdir("/proc"), reason="needs /proc")
    def test_it_finds_a_real_holder(self, tmp_path):
        """Against a live process holding a real file -- the /proc walk is the
        part a mock cannot check."""
        held = tmp_path / "held"
        held.write_text("x")
        proc = subprocess.Popen(
            [sys.executable, "-c",
             f"f = open({str(held)!r}); import time; print('ok', flush=True); time.sleep(30)"],
            stdout=subprocess.PIPE, text=True)
        try:
            proc.stdout.readline()          # it has the file open
            holders = dict(port_holders(str(held)))
            assert proc.pid in holders, holders
            # The name is whatever the interpreter is called here -- "python3"
            # on the rig, "python" on CI -- so pin that we report one, not
            # which one.
            assert holders[proc.pid]
        finally:
            proc.kill()
            proc.wait()

    @pytest.mark.skipif(not os.path.isdir("/proc"), reason="needs /proc")
    def test_our_own_handle_is_not_a_holder(self, tmp_path):
        """Otherwise every open would refuse itself on the second device."""
        held = tmp_path / "mine"
        held.write_text("x")
        with open(held):
            assert port_holders(str(held)) == []
