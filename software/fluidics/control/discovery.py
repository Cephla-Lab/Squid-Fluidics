"""Find a device's serial port by USB serial number, failing usefully.

Three drivers grew three private copies of this loop, and each failed its
own way when the device was unplugged: the syringe pump with a bare
AttributeError (self.com_link never assigned), the temperature controller
with a ValueError naming the serial, the microcontroller with
IOError('No Controller Found') naming nothing. The most common field
failure -- a cable, a renumbered rig, a stale config -- deserved better
than a traceback, so the search and its error live here, once.
"""

import errno

import serial
from serial.tools import list_ports

# Re-exported: it was born here, and the drivers and entry points import it
# from here; its family (DeviceError) lives in fluidics.errors now.
from ..errors import DeviceError, DeviceNotFoundError  # noqa: F401


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

    Two programs reading one serial port split the frames between them, so
    each sees fragments and most decodes fail -- a burst of "not enough
    input bytes for length code" in the run log, with pyserial's own guess
    at the cause ("multiple access on port?") mixed in. Refuse instead.

    The lock is advisory: it stops another opener that also asks for it,
    which is every open this package makes. It cannot stop an unrelated
    program, and does not try to.
    """
    try:
        return serial.Serial(port, exclusive=True, **kwargs)
    except serial.SerialException as e:
        # pyserial prefixes every flock failure this way, contention or not:
        # ENOLCK from a filesystem that cannot lock, EINTR from a signal.
        # Only EWOULDBLOCK means someone else holds it, and only that gets
        # the "another program" reading -- the rest keep their own.
        if e.errno != errno.EWOULDBLOCK or "exclusively lock" not in str(e):
            raise
        raise DeviceError(
            f"{device_name} on {port} is already open in another program. "
            f"Close it and try again -- two programs on one port both get "
            f"corrupt data."
        ) from e
