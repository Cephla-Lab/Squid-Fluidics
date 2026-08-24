"""Logging for the fluidics package: a console you can read, a file that lasts.

Everything first-party logs under the "fluidics" logger (entry points use
fluidics.cli / fluidics.gui), so one configuration covers the package without
adopting third-party noise the way configuring the root logger would.

configure_console() puts INFO and up on stderr, in roughly the terse
timestamped shape the old print_message used. start_log_file() adds a DEBUG
file handler under logs/, one file per run named by its start time -- the
durable record that a warn-mode flow fault or an overnight failure previously
did not leave (the GUI notice is cleared at the next run; stdout does not
exist for a desktop-icon launch). No rotation: a run's file is closed when
the run ends and never written again.
"""

import logging
from datetime import datetime
from pathlib import Path

LOGGER_NAME = "fluidics"

_console_handler = None
_file_handler = None


def configure_console(level=logging.INFO):
    """Attach the stderr handler once; safe to call from every entry point."""
    global _console_handler
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.DEBUG)
    # Whatever the host application does to the root logger must not
    # double-print our records.
    logger.propagate = False
    if _console_handler is None:
        handler = logging.StreamHandler()
        handler.setLevel(level)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s", datefmt="%m/%d %H:%M:%S"))
        logger.addHandler(handler)
        _console_handler = handler


def start_log_file(directory="logs"):
    """Open this run's log file and return its path.

    DEBUG and up: the file also carries what the console suppresses (per-move
    valve traffic, debug chatter), which is exactly what a post-mortem wants.
    Starting a new file closes the previous one.
    """
    global _file_handler
    configure_console()
    stop_log_file()
    log_dir = Path(directory)
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / datetime.now().strftime("run_%Y%m%d_%H%M%S.log")
    handler = logging.FileHandler(path)
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    logging.getLogger(LOGGER_NAME).addHandler(handler)
    _file_handler = handler
    logging.getLogger(LOGGER_NAME).info("Run log: %s", path)
    return path


def stop_log_file():
    """Detach and close the current run's file, if one is open. Idempotent."""
    global _file_handler
    if _file_handler is not None:
        logging.getLogger(LOGGER_NAME).removeHandler(_file_handler)
        _file_handler.close()
        _file_handler = None
