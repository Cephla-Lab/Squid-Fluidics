import argparse
import sys
import threading
from fluidics.sequences import (
    check_ports_against_config, get_included_sequences, load_sequences,
)
from fluidics.control.config import load_config
from fluidics.devices import build_devices, build_operations
from fluidics.experiment_worker import ExperimentWorker


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

    devices = None
    worker = None
    thread = None
    close_errors = []

    try:
        # Load sequences
        sequences = load_sequences(args.path)
        included = get_included_sequences(sequences)
        # Load config
        config = load_config(args.config)
        # Fail on a mistyped port before any hardware is touched.
        check_ports_against_config(included, config)

        devices = build_devices(config, args.simulation)
        experiment_ops = build_operations(config, devices)

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

    except KeyboardInterrupt:
        # `except Exception` would not catch this, so without it Ctrl+C fell
        # straight into finally, tearing down devices -- including the Flow
        # Cell park-to-waste move -- underneath a worker thread still driving
        # the pump on the same serial port. Quiesce the run first: abort wakes
        # the worker out of any wait (incubation, wait_for_stop), the join
        # lets it unwind through its own error path, and only then does the
        # finally block touch the hardware, single-threaded.
        print("Interrupted; stopping the run before closing devices...",
              file=sys.stderr)
        if worker is not None:
            worker.abort()
        if devices is not None:
            devices.abort()
        if thread is not None:
            thread.join()
        sys.exit(130)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        if thread is not None:
            thread.join()
        sys.exit(1)
    finally:
        # DeviceSet.close owns the teardown ordering: sensors detach before the
        # controller stops the reader thread that owns the MCU port.
        if devices is not None:
            close_errors = devices.close()

    # Reached only when the run itself succeeded (the error paths above exit
    # through sys.exit, skipping this). A run whose teardown failed must not
    # report clean: the syringe may not be parked and a port may still be held.
    if close_errors:
        sys.exit(2)

if __name__ == '__main__':
    main()
