"""Move one selector valve to a new I2C address (bench use).

Power-cycle the controller and connect exactly ONE valve first: every device
on the valve bus hears a command sent to --from (a J20 flow sensor answers
0x10 as well, so unplug it). Typical use, freeing 0x10 for a flow sensor
on J20:

    python readdress_valve.py --from 0x10 --to 0x1A

then give the valve that address under reagent_selection.selector_valves.
i2c_addresses in the rig config. Addresses are the 8-bit form IDEX uses
(factory default 0x0E).
"""

import argparse
import sys

from fluidics.control.config import default_config_path, load_config
from fluidics.control.controller import FluidController
from fluidics.control.valve_address import check_readdress_request, readdress_valve
from fluidics.errors import DeviceError


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Move one selector valve to a new I2C address (bench use).")
    parser.add_argument("--from", dest="from_addr", type=lambda s: int(s, 0),
                        required=True,
                        help="the valve's current address, e.g. 0x10 (factory default 0x0E)")
    parser.add_argument("--to", dest="to_addr", type=lambda s: int(s, 0), required=True,
                        help="the new address: even, 0x0E-0xFE, not 0x10")
    parser.add_argument("--ports", type=int, default=10,
                        help="the valve's number of ports, to check its position (default 10)")
    parser.add_argument("--config", default=None,
                        help="rig config, for the controller's serial number; defaults "
                             "to ./config.yaml or ./config.json")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        check_readdress_request(args.from_addr, args.to_addr)
    except ValueError as e:
        print(f"Not re-addressed: {e}", file=sys.stderr)
        return 2
    config_path = args.config or default_config_path()
    if config_path is None:
        print("No --config given and no ./config.yaml or ./config.json here.",
              file=sys.stderr)
        return 2
    serial_number = load_config(config_path).microcontroller.serial_number

    input("Power-cycle the controller, connect exactly ONE selector valve and "
          "nothing else on the valve bus (unplug a J20 flow sensor), then press "
          "Enter.")
    fc = FluidController(serial_number)
    fc.begin()
    try:
        position = readdress_valve(fc, args.from_addr, args.to_addr, args.ports,
                                   power_cycle=input)
    except DeviceError as e:
        print(f"Not re-addressed: {e}", file=sys.stderr)
        return 1
    finally:
        fc.close()

    print(f"Done: the valve answers at 0x{args.to_addr:02X} (position {position}).")
    print("In the rig config, under reagent_selection.selector_valves, set:")
    print(f"  i2c_addresses: {{<valve id>: 0x{args.to_addr:02X}}}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
