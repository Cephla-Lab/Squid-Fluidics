import argparse
import logging
import sys
from fluidics.sequences import (
    check_ports_against_config, get_included_sequences, load_sequences,
)
from fluidics.control.config import load_config
from fluidics.devices import build_devices, build_operations
from fluidics.run_session import RunSession
from fluidics.run_log import (
    setup_uncaught_exception_logging, start_log_file, stop_log_file,
)

_logger = logging.getLogger("fluidics.cli")


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

def main():
    args = parse_args()
    setup_uncaught_exception_logging()
    start_log_file()

    devices = None
    session = None
    run_errors = []
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

        # The worker narrates its own run through the fluidics logger, so
        # the CLI needs no rendering callbacks -- but a failed run must not
        # exit 0, and the worker reports failure only through on_error.
        session = RunSession(devices)
        session.start(included, experiment_ops,
                      callbacks={"on_error": run_errors.append})
        session.wait()

    except KeyboardInterrupt:
        # `except Exception` would not catch this, so without it Ctrl+C fell
        # straight into finally, tearing down devices -- including the Flow
        # Cell park-to-waste move -- underneath a worker thread still driving
        # the pump on the same serial port. Quiesce the run first: the one
        # cancel signal wakes the worker out of any wait (incubation,
        # wait_for_stop), the join lets it unwind through its own error
        # path, and only then does the finally block touch the hardware,
        # single-threaded.
        _logger.warning("Interrupted; stopping the run before closing devices...")
        if session is not None and session.busy:
            session.abort()
            session.wait()
        sys.exit(130)
    except Exception as e:
        # Nothing after the thread starts raises through here, so there is
        # no run to wait on: straight to teardown.
        _logger.exception("%s", e)
        sys.exit(1)
    finally:
        # DeviceSet.close owns the teardown ordering: sensors detach before the
        # controller stops the reader thread that owns the MCU port.
        if devices is not None:
            close_errors = devices.close()
        stop_log_file()

    # Reached whenever main's own try completed. The worker never raises out
    # of run() -- it reports through on_error -- so a failed run lands here
    # too and must not exit 0; nor may a failed teardown report clean (the
    # syringe may not be parked, a port may still be held).
    if run_errors:
        sys.exit(1)
    if close_errors:
        sys.exit(2)

if __name__ == '__main__':
    main()
