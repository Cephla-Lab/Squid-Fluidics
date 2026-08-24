# tests/unit/test_run_log.py
"""The run log's lifecycle: console once, one file per run, clean detach.

Every test restores the "fluidics" logger's state on the way out --
configure_console sets propagate=False for the process, which would silently
break any later caplog-based test (caplog listens on the root logger).
"""

import logging

import pytest

from fluidics import run_log


@pytest.fixture(autouse=True)
def pristine_logger():
    logger = logging.getLogger(run_log.LOGGER_NAME)
    saved = (logger.handlers[:], logger.propagate, logger.level)
    saved_console, saved_file = run_log._console_handler, run_log._file_handler
    run_log._console_handler = None
    run_log._file_handler = None
    yield
    run_log.stop_log_file()
    logger.handlers[:], logger.propagate, logger.level = saved
    run_log._console_handler, run_log._file_handler = saved_console, saved_file


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


def test_each_start_opens_a_fresh_file_and_closes_the_old_one(tmp_path):
    first = run_log.start_log_file(tmp_path)
    second = run_log.start_log_file(tmp_path / "second")
    logging.getLogger("fluidics.x").info("after switch")
    run_log.stop_log_file()
    assert "after switch" not in first.read_text()
    assert "after switch" in second.read_text()


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
