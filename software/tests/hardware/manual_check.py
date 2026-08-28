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
from fluidics.control.controller import FluidControllerSimulation
from fluidics.control.syringe_pump import SyringePumpSimulation
from fluidics.run_log import configure_console
from fluidics.system import FluidicsSystem

log = logging.getLogger("fluidics.manual_check")


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

    configure_console()
    if args.simulation:
        # A self-check of the script, not of the simulation's pacing: its
        # one-second valve commands and five-second moves would otherwise
        # make this a fifteen-second wait.
        FluidControllerSimulation.COMMAND_SECONDS = 0
        SyringePumpSimulation.ESTIMATE_SECONDS = 0

    config = load_config(args.config)
    system = FluidicsSystem.build(config, args.simulation)
    try:
        manual = system.manual
        log.info("Rig up. The syringe holds %.0f uL.", manual.held_volume_ul())

        manual.open_port(args.reagent_port)
        manual.extract(config.syringe_pump.extract_port, args.volume, args.flow_rate,
                       on_started=lambda s: log.info("Drawing; about %.0f s.", s))
        log.info("Drawn. The syringe holds %.0f uL.", manual.held_volume_ul())

        manual.empty_to_waste(on_started=lambda s: log.info("Emptying; about %.0f s.", s))
        log.info("Emptied. The syringe holds %.0f uL.", manual.held_volume_ul())

        if system.devices.disc_pump is not None:
            manual.aspirate(args.aspirate)
            log.info("Drain ran for %.1f s.", args.aspirate)
        log.info("Every verb ran.")
    except KeyboardInterrupt:
        # The verbs run on this thread, so Ctrl+C lands inside a wait with
        # nothing cancelled: halt the pump before the close below sends it
        # anything more.
        log.warning("Interrupted; halting the pump before closing.")
        system.make_safe()
        sys.exit(130)
    finally:
        # close() logs each failure itself; only the exit code is ours, and
        # it is run_sequences.py's for the same condition.
        if system.close():
            sys.exit(2)


if __name__ == "__main__":
    main()
