import argparse
import sys
import threading
from fluidics.sequences import load_sequences, get_included_sequences
from fluidics.control.config import load_config
from fluidics.control.controller import FluidControllerSimulation, FluidController
from fluidics.control.syringe_pump import SyringePumpSimulation, SyringePump
from fluidics.control.selector_valve import SelectorValveSystem
from fluidics.control.disc_pump import DiscPump
from fluidics.control.temperature_controller import TCMControllerSimulation, TCMController
from fluidics.control.flow_sensor import build_flow_sensors
from fluidics.merfish_operations import MERFISHOperations
from fluidics.open_chamber_operations import OpenChamberOperations
from fluidics.experiment_worker import ExperimentWorker
from fluidics.control._def import CMD_SET


def parse_args():
    parser = argparse.ArgumentParser(
        description='Run sequences from a YAML or CSV file'
    )
    parser.add_argument(
        '--path', required=True,
        help='Path to the sequence file (YAML or CSV)'
    )
    parser.add_argument(
        '--config', default='config.yaml',
        help='Path to configuration file (YAML or JSON)'
    )
    parser.add_argument(
        '--simulation',
        action='store_true',
        default=False,
        help='Run in simulation mode without operating hardware'
    )
    return parser.parse_args()

def initialize_hardware(simulation, config):
    temperatureController = None

    if simulation:
        controller = FluidControllerSimulation(config.microcontroller.serial_number)
        syringePump = SyringePumpSimulation(
            sn=config.syringe_pump.serial_number,
            syringe_ul=config.syringe_pump.volume_ul,
            speed_code_limit=config.syringe_pump.speed_code_limit,
            waste_port=config.syringe_pump.waste_port)
        if config.temperature_controller is not None:
            tc_cfg = config.temperature_controller
            temperatureController = TCMControllerSimulation(
                sn=tc_cfg.serial_number,
                channels=tc_cfg.channels,
                tolerance_celsius=tc_cfg.tolerance_celsius,
                stabilization_timeout_seconds=tc_cfg.stabilization_timeout_seconds,
            )
    else:
        controller = FluidController(config.microcontroller.serial_number)
        syringePump = SyringePump(
            sn=config.syringe_pump.serial_number,
            syringe_ul=config.syringe_pump.volume_ul,
            speed_code_limit=config.syringe_pump.speed_code_limit,
            waste_port=config.syringe_pump.waste_port)
        if config.temperature_controller is not None:
            tc_cfg = config.temperature_controller
            temperatureController = TCMController(
                sn=tc_cfg.serial_number,
                channels=tc_cfg.channels,
                tolerance_celsius=tc_cfg.tolerance_celsius,
                stabilization_timeout_seconds=tc_cfg.stabilization_timeout_seconds,
            )

    controller.begin()
    controller.send_command(CMD_SET.CLEAR)

    flow_sensors = build_flow_sensors(controller, config, simulation)
    for sensor in flow_sensors:
        sensor.begin()
        # The simulated sensor's publish loop is built stopped so tests can
        # drive it by hand. A CLI run has to start it, or a --simulation run
        # with monitor: stop reads nothing, never faults, and looks like draw
        # protection working when it has not run at all.
        if hasattr(sensor, "reading_thread"):
            sensor.reading_thread.start()

    return controller, syringePump, temperatureController, flow_sensors

def update_progress(index, sequence_num, status):
    print(f"Sequence {index} ({sequence_num}): {status}")

def on_error(error_msg):
    print(f"Error: {error_msg}")

def on_finished():
    print("Experiment completed")

def on_estimate(time_to_finish, n_sequences):
    print(f"Estimated time: {time_to_finish}s, Sequences: {n_sequences}")

def main():
    args = parse_args()

    controller = None
    syringePump = None
    temperatureController = None
    flowSensors = []
    thread = None

    try:
        # Load sequences
        sequences = load_sequences(args.path)
        included = get_included_sequences(sequences)
        # Load config
        config = load_config(args.config)

        controller, syringePump, temperatureController, flowSensors = initialize_hardware(args.simulation, config)

        selectorValveSystem = SelectorValveSystem(controller, config)
        if config.application == "Open Chamber":
            discPump = DiscPump(controller)

        # Run experiment
        if config.application == "Flow Cell":
            experiment_ops = MERFISHOperations(config, syringePump, selectorValveSystem, temperatureController, flowSensors)
        elif config.application == "Open Chamber":
            experiment_ops = OpenChamberOperations(config, syringePump, selectorValveSystem, discPump, temperatureController)
        else:
            raise ValueError(f"Unsupported application: {config.application!r}")

        callbacks = {
            'update_progress': update_progress,
            'on_error': on_error,
            'on_finished': on_finished,
            'on_estimate': on_estimate
        }

        worker = ExperimentWorker(experiment_ops, included, config, callbacks)
        thread = threading.Thread(target=worker.run)
        thread.start()

        thread.join()

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        if thread is not None:
            thread.join()
        sys.exit(1)
    finally:
        if syringePump is not None:
            syringePump.reset_abort()
            syringePump.close()
        for sensor in flowSensors:
            sensor.close()
        if temperatureController is not None:
            temperatureController.close()
        # Last: the reader thread owns the MCU port for the whole run, so stop
        # it and release the port rather than leaving that to __del__. Sensors
        # detach from the controller above, so nothing is left subscribed.
        if controller is not None:
            controller.close()

if __name__ == '__main__':
    main()
