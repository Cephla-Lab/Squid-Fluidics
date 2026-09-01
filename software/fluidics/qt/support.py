"""Shared Qt plumbing for the fluidics widgets: worker-thread event posting, small dialog/format
helpers and the log-pane handler. Moved verbatim from gui.py; imports go through qtpy."""

import logging

from qtpy.QtCore import QCoreApplication, QEvent
from qtpy.QtWidgets import QMessageBox

from fluidics.run_log import LOGGER_NAME


class WorkerEvent(QEvent):
    EVENT_TYPE = QEvent.Type(QEvent.registerEventType())

    def __init__(self, callback_name, *args):
        super().__init__(WorkerEvent.EVENT_TYPE)
        self.callback_name = callback_name
        self.args = args


class PostsToQtThread:
    """For a QObject fed by worker threads: _post_event(name, *args) queues a
    call to one of its own methods on the Qt thread. Mix in before the Qt
    base class, so event() here sees the WorkerEvent first."""

    def _post_event(self, method_name, *args):
        QCoreApplication.postEvent(self, WorkerEvent(method_name, *args))

    def event(self, event):
        if event.type() == WorkerEvent.EVENT_TYPE:
            getattr(self, event.callback_name)(*event.args)
            return True
        return super().event(event)



def _ask_yes_no(parent, title, text, default=QMessageBox.No):
    """One spelling of the Yes/No question the GUI asks three ways --
    start a run, resume one, abort on exit."""
    answer = QMessageBox.question(parent, title, text,
                                  QMessageBox.Yes | QMessageBox.No, default)
    return answer == QMessageBox.Yes


def _hms(seconds):
    """Seconds as hh:mm:ss, for the estimate dialog and the countdown."""
    seconds = int(seconds)
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"



class GuiLogHandler(logging.Handler):
    """Feeds the run tab's log pane from the fluidics logger.

    Records arrive on whatever thread logged them -- the run's, the MCU
    reader's, a report writer's -- so the text crosses to the Qt thread
    the way every other cross-thread paint does. `detach()` takes it off
    the logger: handlers are held globally, so one left attached would
    outlive its tab and post into a destroyed widget.
    """

    def __init__(self, widget):
        super().__init__(level=logging.INFO)
        self.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s", datefmt="%m/%d %H:%M:%S"))
        self._widget = widget

    def emit(self, record):
        widget = self._widget
        if widget is None:
            return
        try:
            widget._post_event("_handle_log_line", self.format(record))
        except RuntimeError:
            # The C++ widget went out from under us: its tab is gone but
            # this handler is still on the logger, so take it off here --
            # nothing else will, and posting again would be worse.
            self.detach()

    def detach(self):
        """Take this handler off the logger and stop feeding the pane.
        Idempotent: the widget's destruction and a failed post both land
        here, in either order."""
        self._widget = None
        logging.getLogger(LOGGER_NAME).removeHandler(self)

    def close(self):
        self._widget = None
        super().close()


def subscribe_until_detached(*pairs):
    """Subscribe each (feed, callback) pair; return a detach() that removes exactly
    these callbacks. Subscribers.unsubscribe deregisters by identity, so an embedded
    widget must retain the bound methods it registered and hand them back on teardown
    (connect the returned detach to the widget's destroyed signal)."""
    for feed, callback in pairs:
        feed.subscribe(callback)

    def detach():
        for feed, callback in pairs:
            feed.unsubscribe(callback)

    return detach
