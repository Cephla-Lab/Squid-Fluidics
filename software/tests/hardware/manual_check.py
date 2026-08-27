"""Bring a rig up from its config and exercise each manual verb once.

The hardware smoke for a newly wired rig, on the same factory and verbs the
GUI's manual tab uses: turn the selector valves to a reagent port, draw a
volume through the syringe pump, empty it to waste, and -- on an Open Chamber
rig -- run the drain for a moment. Watch the liquid; the script only reports
what the pump believes it holds.

Run from software/ as a module, so `fluidics` imports without the package
being installed:

    python -m tests.hardware.manual_check --config ../config.yaml
    python -m tests.hardware.manual_check --config ../config.yaml --reagent-port 3 --volume 200 --flow-rate 500
    python -m tests.hardware.manual_check --config sample_config/flow_cell_config.yaml --simulation
"""

import argparse
import logging
import sys

from fluidics.control.config import load_config
from fluidics.devices import build_devices
from fluidics.manual_operations import ManualOperations


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--config", required=True, help="rig config YAML")
    parser.add_argument("--reagent-port", type=int, default=1,
                        help="selector-valve port to open first (default 1)")
    parser.add_argument("--volume", type=int, default=200,
                        help="whole uL to draw and then empty to waste (default 200)")
    parser.add_argument("--flow-rate", type=float, default=500,
                        help="uL/min for the draw (default 500)")
    parser.add_argument("--aspirate", type=float, default=2,
                        help="seconds of drain on an Open Chamber rig (default 2)")
    parser.add_argument("--simulation", action="store_true",
                        help="simulated hardware, to check the script itself")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log = logging.getLogger("manual_check")

    config = load_config(args.config)
    devices = build_devices(config, args.simulation)
    try:
        manual = ManualOperations(devices)
        log.info("Rig up. The syringe holds %.0f uL.", manual.held_volume_ul())

        manual.open_port(args.reagent_port)
        manual.extract(config.syringe_pump.extract_port, args.volume, args.flow_rate,
                       on_started=lambda s: log.info("Drawing; about %.0f s.", s))
        log.info("Drawn. The syringe holds %.0f uL.", manual.held_volume_ul())

        manual.empty_to_waste(on_started=lambda s: log.info("Emptying; about %.0f s.", s))
        log.info("Emptied. The syringe holds %.0f uL.", manual.held_volume_ul())

        if devices.disc_pump is not None:
            manual.aspirate(args.aspirate)
            log.info("Drain ran for %.1f s.", args.aspirate)
        log.info("Every verb ran.")
    finally:
        errors = devices.close()
        if errors:
            log.error("Closing the rig reported: %s", "; ".join(map(str, errors)))
            sys.exit(1)


if __name__ == "__main__":
    main()
