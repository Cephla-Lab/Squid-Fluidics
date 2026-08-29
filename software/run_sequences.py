import argparse
import logging
import sys
from fluidics.sequences import (
    get_included_sequences, load_sequences, validate_sequences,
)
from fluidics.control.config import default_config_path, load_config
from fluidics.events import RunEnded
from fluidics.system import FluidicsSystem
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
        '--config', default=None,
        help='Path to configuration file (YAML or JSON); defaults to the '
             "rig's own ./config.yaml or ./config.json"
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

    system = None
    run_errors = []
    close_errors = []

    try:
        # Load sequences
        sequences = load_sequences(args.path)
        included = get_included_sequences(sequences)
        # Load config
        config_path = args.config or default_config_path()
        if config_path is None:
            _logger.error("No --config given and no ./config.yaml or "
                          "./config.json here.")
            sys.exit(2)
        config = load_config(config_path)
        # Fail on a mistyped port or a wrong-application sequence type
        # before any hardware is touched.
        validate_sequences(included, config)

        system = FluidicsSystem.build(config, args.simulation)

        # The worker narrates its own run through the fluidics logger, so
        # the CLI renders nothing -- but a failed run must not exit 0, and
        # the run reports its outcome only through the events channel.
        def note_bad_ending(event):
            if isinstance(event, RunEnded) and event.outcome != "finished":
                run_errors.append(event.message or event.outcome)

        system.session.events.subscribe(note_bad_ending)
        system.run(included)
        system.wait()

    except KeyboardInterrupt:
        # Caught so Ctrl+C does not fall straight into finally with the exit
        # code of a crash; system.close() below stops the run first.
        _logger.warning("Interrupted.")
        sys.exit(130)
    except Exception as e:
        # Nothing after the thread starts raises through here, so there is
        # no run to wait on: straight to teardown.
        _logger.exception("%s", e)
        sys.exit(1)
    finally:
        if system is not None:
            # No time limit: an attended terminal waits as long as the
            # operator will, and a second Ctrl+C forces its way through the
            # wait -- the devices are still released (close() shields that).
            close_errors = system.close(timeout=None)
        stop_log_file()

    # Reached whenever main's own try completed. The worker never raises out
    # of run() -- the run's ending arrives as a RunEnded event -- so a failed
    # run lands here too and must not exit 0; nor may a failed teardown
    # report clean (the syringe may not be parked, a port may still be held).
    if run_errors:
        sys.exit(1)
    if close_errors:
        sys.exit(2)

if __name__ == '__main__':
    main()
