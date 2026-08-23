"""Find a device's serial port by USB serial number, failing usefully.

Three drivers grew three private copies of this loop, and each failed its
own way when the device was unplugged: the syringe pump with a bare
AttributeError (self.com_link never assigned), the temperature controller
with a ValueError naming the serial, the microcontroller with
IOError('No Controller Found') naming nothing. The most common field
failure -- a cable, a renumbered rig, a stale config -- deserved better
than a traceback, so the search and its error live here, once.
"""

from serial.tools import list_ports


class DeviceNotFoundError(Exception):
    """A configured device is not on the bus.

    The message carries what the operator needs: which device, which serial
    number the config names, and what is actually plugged in. (Re-parent
    under fluidics/errors.py when the cancellation redesign lands it.)
    """


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
