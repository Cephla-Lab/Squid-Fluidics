"""Poll a TCM temperature controller for diagnostic state.

Bypasses TCMController so error replies (CMD:REPLY=N) print instead of raise.
Run from software/ with the controller's USB serial number.

Examples:
    python tests/hardware/diagnose_temperature_controller.py --sn ABC123
    python tests/hardware/diagnose_temperature_controller.py --port COM5 --channel 1
    python tests/hardware/diagnose_temperature_controller.py --sn ABC123 --enable-output
"""

import argparse
import sys
import time

import serial
from serial.tools import list_ports


# Parameters worth polling for "enabled but no drive" diagnosis.
# Names follow the protocol manual; some firmwares use TCADJUSTTEMP instead of
# TCADJTEMP — both are queried so the working one shows up.
QUERY_PARAMS = [
    "TCACTTEMP",      # actual temperature
    "TCADJTEMP",      # target (short alias used by current code)
    "TCADJUSTTEMP",   # target (canonical name in manual)
    "TCSETTEMP",      # target (modbus param list name)
    "TCSW",           # output switch
    "TCOE",           # output enable (modbus)
    "TCACTVOL",       # actual TEC voltage
    "TCACTCUR",       # actual TEC current
    "TCSETVOL",       # output voltage limit
    "TCCURLIMIT",     # output current limit
    "TCERRORSTATUS",  # error status word
    "TCRTOCPSTATUS",  # real-time over-current status
    "TCOTPSTATUS",    # over-temperature protection status
    "TCOCPSTATUS",    # over-current protection status
    "TCEXTSTATUS",    # external status
]


def find_port(sn):
    for d in list_ports.comports():
        if d.serial_number == sn:
            return d.device
    return None


def send(ser, frame):
    """Send `frame` (already includes trailing \\r), return decoded response."""
    ser.reset_input_buffer()
    ser.write(frame.encode())
    # Spec §4.6 says >50 ms between commands; readline waits for \r or timeout.
    raw = ser.readline()
    return raw.decode(errors="replace").strip()


def query(ser, channel, param):
    cmd = f"TC{channel}:{param}?\r"
    resp = send(ser, cmd)
    print(f"  {param:<16}  ->  {resp!r}")
    # 50 ms inter-command gap per §4.6.
    time.sleep(0.06)
    return resp


def set_param(ser, channel, param, value):
    cmd = f"TC{channel}:{param}={value}\r"
    resp = send(ser, cmd)
    print(f"  SET {param}={value}  ->  {resp!r}")
    time.sleep(0.06)
    return resp


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--sn", help="USB serial number of the controller")
    src.add_argument("--port", help="Direct COM/tty port (skips enumeration)")
    ap.add_argument("--channel", type=int, choices=[1, 2], default=1,
                    help="TC channel")
    ap.add_argument("--baud", type=int, default=57600)
    ap.add_argument("--timeout", type=float, default=0.5)
    out = ap.add_mutually_exclusive_group()
    out.add_argument("--enable-output", action="store_true",
                     help="Send TCSW=1 and TCOE=1 before polling")
    out.add_argument("--disable-output", action="store_true",
                     help="Send TCSW=0 after polling (use to leave the unit idle)")
    ap.add_argument("--set-target", type=float, default=None,
                    help="Set target to this °C before polling")
    args = ap.parse_args()

    if args.port:
        port = args.port
    else:
        port = find_port(args.sn)
        if port is None:
            print(f"No serial device with serial_number={args.sn!r}")
            print("Available ports:")
            for d in list_ports.comports():
                print(f"  {d.device}  sn={d.serial_number!r}  desc={d.description!r}")
            sys.exit(1)

    print(f"Opening {port} @ {args.baud} baud, channel TC{args.channel}\n")
    ser = serial.Serial(port, baudrate=args.baud, timeout=args.timeout)
    try:
        if args.set_target is not None:
            print("--- setting target ---")
            set_param(ser, args.channel, "TCADJTEMP", args.set_target)
            set_param(ser, args.channel, "TCADJUSTTEMP", args.set_target)
            print()

        if args.enable_output:
            print("--- enabling output ---")
            set_param(ser, args.channel, "TCSW", 1)
            set_param(ser, args.channel, "TCOE", 1)
            time.sleep(0.3)  # give the loop a moment to ramp
            print()

        print("--- polling state ---")
        for p in QUERY_PARAMS:
            query(ser, args.channel, p)

        if args.disable_output:
            print("\n--- disabling output ---")
            set_param(ser, args.channel, "TCSW", 0)
    finally:
        ser.close()


if __name__ == "__main__":
    main()
