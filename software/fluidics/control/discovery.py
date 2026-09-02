"""Find a device's serial port by USB serial number, failing usefully.

Three drivers grew three private copies of this loop, and each failed its
own way when the device was unplugged: the syringe pump with a bare
AttributeError (self.com_link never assigned), the temperature controller
with a ValueError naming the serial, the microcontroller with
IOError('No Controller Found') naming nothing. The most common field
failure -- a cable, a renumbered rig, a stale config -- deserved better
than a traceback, so the search and its error live here, once.
"""

import glob
import os

import serial
from serial.tools import list_ports

# Re-exported: it was born here, and the drivers and entry points import it
# from here; its family (DeviceError) lives in fluidics.errors now.
from ..errors import DeviceInUseError, DeviceNotFoundError  # noqa: F401


def find_serial_port(serial_number, device_name):
    """The port path for the device with `serial_number`, else raise.

    serial_number must be a real value: ports with no USB serial report
    None, so matching a None from a half-filled config against them would
    silently pick an arbitrary device.
    """
    ports = list_ports.comports()
    if serial_number is not None:
        for p in ports:
            if p.serial_number == serial_number:
                return p.device
    present = ", ".join(
        f"{p.device} (sn {p.serial_number})" for p in ports) or "none"
    raise DeviceNotFoundError(
        f"{device_name} with serial number {serial_number!r} not found. "
        f"Serial devices present: {present}. Check the serial number in "
        f"the config file and the USB connection."
    )


def open_serial_port(port, device_name, **kwargs):
    """Open `port` for this process alone.

    Two processes on one port is not a hypothetical: a second instance of
    this software reading the MCU stream splits the frames between them,
    so each sees fragments and most decodes fail. That looked like a burst
    of "not enough input bytes for length code" in the run log, mixed with
    pyserial's own guess at the cause -- "device disconnected or multiple
    access on port?" -- and it cost a valve command its completion packet.

    The lock is advisory (flock), so it stops another opener that also
    asks for it -- every open this package makes. It cannot stop an
    unrelated program, and does not try to.
    """
    holders = port_holders(port)
    if holders:
        who = ", ".join(f"{name} (pid {pid})" for pid, name in holders)
        raise DeviceInUseError(
            f"{device_name} on {port} is already open in {who}. Close it and "
            f"try again -- two programs reading one port split the frames "
            f"between them, and both get corrupt data."
        )
    try:
        return serial.Serial(port, exclusive=True, **kwargs)
    except serial.SerialException as e:
        if "exclusively lock" not in str(e):
            raise
        raise DeviceInUseError(
            f"{device_name} on {port} is already open in another program. "
            f"Close the other instance of the fluidics software (or whatever "
            f"else holds the port) and try again."
        ) from e


def port_holders(device):
    """Other processes with `device` open, as sorted (pid, name) pairs.

    The advisory lock only stops an opener that asks for it, and the port's
    other user is often a program that does not -- the microscope software's
    own copy of this driver, or a bench script someone left running. /proc
    knows who it is, which turns "the frames are corrupt" into a name and a
    pid the operator can act on.

    Linux only, by construction: elsewhere the glob finds nothing and the
    open proceeds, which is the behaviour we had before.
    """
    me = os.getpid()
    found = set()
    for entry in glob.glob("/proc/[0-9]*/fd/*"):
        try:
            if os.readlink(entry) != device:
                continue
            pid = int(entry.split("/", 3)[2])
        except (OSError, ValueError):
            continue        # the process exited, or is not ours to read
        if pid == me:
            continue
        try:
            with open(f"/proc/{pid}/comm") as f:
                name = f.read().strip()
        except OSError:
            name = "unknown"
        found.add((pid, name))
    return sorted(found)
