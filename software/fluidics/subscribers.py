"""A callback list with isolated dispatch, shared by everything that publishes
to listeners it does not know: the MCU packet stream, a flow sensor's
readings, the TCM's temperatures, the run session's state."""

import logging
import threading

_logger = logging.getLogger(__name__)


class Subscribers:
    '''A callback list with isolated dispatch.

    Producers here run on a background thread while consumers register and
    deregister from another, and one bad consumer must not take out the rest.
    Used for the controller's packet stream and, one layer up, for a flow
    sensor's scaled readings — the two differ only in payload.
    '''

    def __init__(self, label):
        self._label = label          # names the producer in failure messages
        self._lock = threading.Lock()
        self._callbacks = []

    def subscribe(self, callback):
        '''Register a callback. Runs on the producer's thread, so it must not
        block — a slow subscriber delays every other consumer.
        '''
        with self._lock:
            self._callbacks.append(callback)

    def unsubscribe(self, callback):
        '''Deregister by identity. A no-op if absent, and idempotent.

        Filters rather than calling list.remove(), which matches with __eq__ —
        that would invoke arbitrary subscriber comparison and raise ValueError
        for a callback already gone. Absence has to be tolerated because
        close() is legitimately reachable twice: begin() calls it on its
        failure path, and teardown calls it again.
        '''
        with self._lock:
            self._callbacks = [c for c in self._callbacks if c is not callback]

    def clear(self):
        with self._lock:
            self._callbacks = []

    def __len__(self):
        with self._lock:
            return len(self._callbacks)

    def notify(self, *args):
        '''Dispatch to every subscriber, isolating each from the others.

        The snapshot is taken under the lock but dispatched outside it, so a
        subscriber may unsubscribe itself re-entrantly and a slow consumer
        cannot block registration. Each call is wrapped separately rather than
        the loop as a whole: with one try around the loop, a raising subscriber
        would starve every subscriber after it, forever, at 17 packets/second.
        '''
        with self._lock:
            callbacks = list(self._callbacks)

        for callback in callbacks:
            try:
                callback(*args)
            except Exception as e:
                _logger.warning("%s subscriber failed: %s", self._label, e)
