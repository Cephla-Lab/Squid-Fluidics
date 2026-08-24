"""Logging for the fluidics package: a console you can read, a file that lasts.

The conventions follow the Squid software's squid/logging.py so the two
instruments feel the same to operate:

- Files live in the platform's user log directory (Linux:
  ~/.local/state/squid-fluidics/log), never relative to the working
  directory -- a desktop-launcher GUI and a terminal run log to the same
  place.
- One fixed filename with rotation: every start_log_file() rolls the
  previous file to fluidics.log.1 (.2, ...), keeping the last 25 runs and
  pruning itself. The announced "Run log:" line names the live file.
- The file carries DEBUG and up with thread ids and source locations; the
  console stays terse at INFO.
- Uncaught exceptions -- main thread, worker threads, and unraisables --
  are routed into the log before the interpreter's own handling runs.

Everything first-party logs under the "fluidics" logger (entry points use
fluidics.cli / fluidics.gui), so one configuration covers the package
without adopting third-party noise the way configuring the root logger
would.
"""

import logging
import logging.handlers
import sys
import threading
from pathlib import Path

import platformdirs

LOGGER_NAME = "fluidics"

# Squid's _baseline_log_format, verbatim: a grep habit or parsing script
# built on one instrument's logs works on the other's.
_FILE_FORMAT = ("%(asctime)s.%(msecs)03d - %(thread_id)d - %(name)s - "
                "%(levelname)s - %(message)s (%(filename)s:%(lineno)d)")
_FILE_DATEFMT = "%Y-%m-%d %H:%M:%S"

_console_handler = None
_file_handler = None
_hooks_installed = False


def _thread_id_filter(record):
    """The reader thread, the Qt thread, and workers all share this log;
    which one spoke matters in every post-mortem."""
    record.thread_id = threading.get_native_id()
    return True


def get_default_log_directory():
    return platformdirs.user_log_path("squid-fluidics", "cephla")


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


def start_log_file(directory=None):
    """Open this run's log file and return its path.

    DEBUG and up: the file also carries what the console suppresses (per-move
    valve traffic, debug chatter), which is exactly what a post-mortem wants.
    An existing file from the previous run is rolled to fluidics.log.1 first,
    so each run starts a fresh file and the directory prunes itself.
    """
    global _file_handler
    configure_console()
    stop_log_file()
    log_dir = Path(directory) if directory is not None else get_default_log_directory()
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / "fluidics.log"
    rollover_needed = path.exists()
    handler = logging.handlers.RotatingFileHandler(
        path, maxBytes=0, backupCount=25, encoding="utf-8", errors="replace")
    if rollover_needed:
        handler.doRollover()
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter(_FILE_FORMAT, datefmt=_FILE_DATEFMT))
    handler.addFilter(_thread_id_filter)
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


def setup_uncaught_exception_logging():
    """Route uncaught exceptions into the log, then let the previous hooks run.

    The failure mode this exists for: an exception in a Qt slot or a
    background thread kills or wounds the process with nothing in the run
    log saying why. Covers the three escape paths (sys.excepthook,
    threading.excepthook, sys.unraisablehook), same as the Squid software.
    Idempotent -- a second call must not chain the hooks twice.
    """
    global _hooks_installed
    if _hooks_installed:
        return
    _hooks_installed = True
    logger = logging.getLogger(LOGGER_NAME)
    previous_excepthook = sys.excepthook
    previous_thread_hook = threading.excepthook
    previous_unraisable_hook = sys.unraisablehook

    def excepthook(exc_type, value, tb):
        logger.error("Uncaught exception", exc_info=(exc_type, value, tb))
        previous_excepthook(exc_type, value, tb)

    def thread_excepthook(args):
        name = args.thread.name if args.thread is not None else "?"
        logger.error("Uncaught exception in thread %s", name,
                     exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
        previous_thread_hook(args)

    def unraisable_hook(info):
        logger.error("Unraisable exception: %s", info.err_msg or "",
                     exc_info=(info.exc_type, info.exc_value, info.exc_traceback))
        previous_unraisable_hook(info)

    sys.excepthook = excepthook
    threading.excepthook = thread_excepthook
    sys.unraisablehook = unraisable_hook
