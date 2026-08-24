# tests/unit/test_run_log.py
"""The run log's lifecycle: console once, one file per run, clean detach.

Every test restores the "fluidics" logger's state on the way out --
configure_console sets propagate=False for the process, which would silently
break any later caplog-based test (caplog listens on the root logger).
"""

import logging
import sys
import threading

import pytest

from fluidics import run_log


@pytest.fixture(autouse=True)
def pristine_logger():
    logger = logging.getLogger(run_log.LOGGER_NAME)
    saved = (logger.handlers[:], logger.propagate, logger.level)
    saved_console, saved_file = run_log._console_handler, run_log._file_handler
    saved_hooks = (sys.excepthook, threading.excepthook, sys.unraisablehook,
                   run_log._hooks_installed)
    run_log._console_handler = None
    run_log._file_handler = None
    run_log._hooks_installed = False
    yield
    run_log.stop_log_file()
    logger.handlers[:], logger.propagate, logger.level = saved
    run_log._console_handler, run_log._file_handler = saved_console, saved_file
    (sys.excepthook, threading.excepthook, sys.unraisablehook,
     run_log._hooks_installed) = saved_hooks


def test_records_land_in_the_run_file(tmp_path):
    path = run_log.start_log_file(tmp_path)
    logging.getLogger("fluidics.control.something").info("pump found")
    run_log.stop_log_file()
    content = path.read_text()
    assert "pump found" in content
    assert "fluidics.control.something" in content


def test_the_file_carries_debug_detail_the_console_suppresses(tmp_path):
    path = run_log.start_log_file(tmp_path)
    logging.getLogger("fluidics.control.selector_valve").debug("open port 3")
    run_log.stop_log_file()
    assert "open port 3" in path.read_text()
    assert run_log._console_handler.level == logging.INFO


def test_each_start_rolls_the_previous_run_aside(tmp_path):
    """The Squid convention: one live filename, prior runs pruned to .1..25."""
    path = run_log.start_log_file(tmp_path)
    logging.getLogger("fluidics.x").info("first run")
    run_log.stop_log_file()
    path = run_log.start_log_file(tmp_path)
    logging.getLogger("fluidics.x").info("second run")
    run_log.stop_log_file()
    assert "second run" in path.read_text()
    assert "first run" not in path.read_text()
    assert "first run" in (tmp_path / "fluidics.log.1").read_text()


def test_console_configuration_is_idempotent(tmp_path):
    run_log.configure_console()
    handler = run_log._console_handler
    run_log.configure_console()
    logger = logging.getLogger(run_log.LOGGER_NAME)
    assert run_log._console_handler is handler
    assert logger.handlers.count(handler) == 1


def test_stop_without_start_is_a_no_op():
    run_log.stop_log_file()


def test_the_run_file_names_itself_in_its_first_line(tmp_path):
    """The operator finding a log later should see which file the run
    announced at the time."""
    path = run_log.start_log_file(tmp_path)
    run_log.stop_log_file()
    assert str(path) in path.read_text()


def test_the_default_directory_is_the_platform_log_home():
    """Never cwd-relative: a desktop-launcher GUI and a terminal run must
    log to the same place."""
    assert run_log.get_default_log_directory().is_absolute()


class TestUncaughtExceptionLogging:
    def test_a_main_thread_crash_reaches_the_log(self, tmp_path):
        path = run_log.start_log_file(tmp_path)
        # Our hook chains to whatever was installed before it -- which under
        # pytest-qt is a capture hook that would fail this test. Park a no-op
        # there first; the autouse fixture restores the real hooks after.
        sys.excepthook = lambda *args: None
        run_log.setup_uncaught_exception_logging()
        try:
            raise ValueError("slot blew up")
        except ValueError:
            sys.excepthook(*sys.exc_info())
        run_log.stop_log_file()
        content = path.read_text()
        assert "Uncaught exception" in content
        assert "slot blew up" in content

    def test_installation_is_idempotent(self):
        run_log.setup_uncaught_exception_logging()
        hook = sys.excepthook
        run_log.setup_uncaught_exception_logging()
        # A second call must not chain the hooks twice.
        assert sys.excepthook is hook
