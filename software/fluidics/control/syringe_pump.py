import logging
import fluidics.control.tecancavro as tecancavro
import threading

from ..errors import RunControl
from .discovery import find_serial_port

_logger = logging.getLogger(__name__)


class Interruptible:
    """Halting a move that is already running, shared by both pump classes.

    Two ways to interrupt, differing only in whether they latch:

      abort()  the operator cancelled. Trips the run's RunControl, so the
               waiting thread wakes and raises AbortRequested out of the
               device call -- the operation unwinds instead of returning as
               if it had finished. Latches until reset_abort().
      stop()   a fault the caller is about to raise on -- a flow fault, say.
               Does not latch and does not cancel the run: the run is being
               failed by whoever called it, and a cancel here would report a
               hardware problem as a user action.

    Shared rather than written twice because the simulation is where these
    semantics get tested -- the real pump needs hardware. Two copies would put
    the tested one and the shipped one out of reach of each other, which is
    exactly how the sleep-through-abort bug survived 230 passing tests.

    Subclasses supply the two hardware-shaped pieces: _terminate() to halt the
    plunger, and _move_finished() to say whether it has stopped.
    """

    def _init_interrupt(self, run_control=None):
        self.is_busy = False
        self.is_aborted = False
        self._interrupt = threading.Event()
        # Shared with every other device of the run when built through
        # build_devices; private when the pump is constructed alone.
        self.run_control = run_control if run_control is not None else RunControl()

    # --- what a real pump does and a simulated one cannot ---

    def _terminate(self):
        """Halt the plunger. Nothing to halt in simulation."""

    def _move_finished(self):
        """Whether the move has ended. A simulated move ends when its
        estimated duration does, so there is nothing further to wait for."""
        return True

    # --- interruption ---

    def abort(self):
        # Halt first, then cancel, then wake: the waiter reads the cause the
        # moment it wakes, and must not resume into a serial round trip while
        # terminateCmd is still in flight on the same port.
        self._terminate()
        self.is_aborted = True
        self.run_control.cancel()
        self._interrupt.set()

    def reset_abort(self):
        self.is_aborted = False
        self.run_control.reset()
        self._interrupt.clear()

    def stop(self):
        self._terminate()
        self._interrupt.set()

    def _arm(self):
        """Clear any stale interrupt; raise if the run is already cancelled.

        Clearing before checking, and abort() cancelling before it sets the
        event, is what makes an abort landing anywhere around here still
        count: either the cause is already set and this raises, or the event
        is set afterwards and wait_for_stop wakes on it and raises. Raising
        here rather than returning is what keeps a cancelled run from pulsing
        the pump once more on its way out.
        """
        self._interrupt.clear()
        self.run_control.check()
        return not self.is_aborted

    def wait_for_stop(self, t=0):
        """Block until the move finishes, or until abort()/stop() interrupts.
        Raises the run's cancellation cause if abort() was what woke it.

        t is the pump's estimate of how long the whole chain will take, which
        for a 2000 uL draw at 500 uL/min is about 240 s. This used to be
        time.sleep(t) -- an uninterruptible sleep for the entire move, so an
        interrupt halted the plunger immediately but the caller stayed asleep
        and the run did not unwind until the estimate elapsed. Waiting on the
        event returns the moment either arrives; _move_finished() stays the
        authoritative end-of-move signal, with the estimate only gating when we
        start asking.
        """
        interrupted = self._interrupt.wait(t)
        while not interrupted and not self._move_finished():
            interrupted = self._interrupt.wait(0.5)
        self.is_busy = False
        self.run_control.check()


class SpeedCodes:
    """The speed-code mapping and the conversions either way.

    Shared by the real pump and the simulation because it is arithmetic over
    self.volume and self.speed_code_limit -- no hardware in it. The simulation
    used to stub flow_rate_to_speed_code as `return 20`, so a simulated run
    ignored the sequence's flow rate entirely and every rate produced the same
    8501 uL/min. Anything that reasons about the actual rate (draw protection
    does) was measuring against a number the simulation invented.
    """

    SPEED_SEC_MAPPING = [1.25, 1.30, 1.39, 1.52, 1.71, 1.97, 2.37, 2.77, 3.03, 3.36, 3.77,
                        4.30, 5.00, 6.00, 7.50, 10.00, 15.00, 30.00, 31.58, 33.33, 35.29,
                        37.50, 40.00, 42.86, 46.15, 50.00, 54.55, 60.00, 66.67, 75.00, 85.71,
                        100.00, 120.00, 150.00, 200.00, 300.00, 333.33, 375.00, 428.57, 500.00, 600.00]
                        # Maps to speed code 0-40

    def effective_speed_code(self, speed_code=None):
        """The code the pump actually runs: never faster than the limit.

        Higher code = slower stroke, so the limit is a floor on the code.
        None means "as fast as allowed" -- the dispense-to-waste default.
        One definition for the real pump and the simulation, because the
        recorded chains are measured against it in tests: a private copy in
        either class could drift and the tests would follow the copy.
        """
        if speed_code is None:
            return self.speed_code_limit
        return max(speed_code, self.speed_code_limit)

    def get_flow_rate(self, speed_code):
        """Flow rate for a speed code, in uL/min.

        uL/min throughout: it is what sequences are written in, what
        flow_rate_to_speed_code takes, and what the flow sensor reports. This
        used to return mL/min, which made it the only function in the pump API
        on a different scale from its own inverse.
        """
        return round(self.volume * 60 / self.SPEED_SEC_MAPPING[speed_code], 2)

    def flow_rate_to_speed_code(self, target_flow_rate):
        """
        Map any flow rate to the closest speed code of the syringe pump

        :param flow_rate: ul/min
        :return: speed code (int)
        """
        # TODO: move this to utils
        target_time = self.volume * 60 / target_flow_rate

        left = 0
        right = len(self.SPEED_SEC_MAPPING) - 1

        # If target is beyond the range, return the closest endpoint
        if target_time <= self.SPEED_SEC_MAPPING[self.speed_code_limit]:
            return self.speed_code_limit
        if target_time >= self.SPEED_SEC_MAPPING[-1]:
            return len(self.SPEED_SEC_MAPPING) - 1

        # Binary search
        while left < right:
            if right - left == 1:
                if abs(self.SPEED_SEC_MAPPING[left] - target_time) <= abs(self.SPEED_SEC_MAPPING[right] - target_time):
                    return left
                return right

            mid = (left + right) // 2
            mid_value = self.SPEED_SEC_MAPPING[mid]

            if mid_value == target_time:
                return mid
            elif mid_value > target_time:
                right = mid
            else:
                left = mid

        return left


class SyringePump(SpeedCodes, Interruptible):
    def __init__(self, sn, syringe_ul, speed_code_limit, waste_port, num_ports=4, slope=14, debug=False,
                 run_control=None):
        # An unmatched serial number used to fall through the search loop and
        # die one line later with a bare AttributeError on self.com_link --
        # the most common field failure (unplugged pump, stale config)
        # reported as a driver bug.
        self.port = find_serial_port(sn, "Syringe pump")
        self.com_link = tecancavro.TecanAPISerial(tecan_addr=0, ser_port=self.port, ser_baud=9600)
        _logger.info("Syringe pump found on %s.", self.port)
        self.syringe = tecancavro.models.XCaliburD(com_link=self.com_link,
                            num_ports=num_ports,
                            syringe_ul=syringe_ul,
                            microstep=False,
                            waste_port=waste_port,
                            slope=slope,
                            debug=debug,
                            debug_log_path='.')
        self.volume = syringe_ul
        self.speed_code_limit = speed_code_limit
        self.range = 3000  # Property of the syringe pump
        self.chained_volume = 0

        # Every touch of self.syringe -- a wire round trip or the driver's
        # chain-building state -- happens under this lock. The GUI's plunger
        # poll runs on the Qt thread while a worker thread drives moves;
        # unlocked, the poll could consume the reply belonging to the
        # worker's _checkReady, surfacing as a spurious TecanAPITimeout that
        # the manual tab used to mask as "operation complete". Held per
        # driver call, never across a move, so position polls and abort()
        # stay live while the plunger runs. A plain (non-reentrant) Lock on
        # purpose: locked regions never nest, and a future violation should
        # deadlock loudly in testing, not silently interleave.
        self._serial_lock = threading.Lock()

        self.get_plunger_position()
        self._init_interrupt(run_control)

        _logger.info("Syringe pump initialized.")

    def get_plunger_position(self):
        with self._serial_lock:
            position = self.syringe.getPlungerPos()
        self.plunger_pos = position / self.range
        return self.plunger_pos

    def get_current_volume(self):
        return self.volume * self.plunger_pos  # ul

    def get_chained_volume(self):
        return self.chained_volume  # ul

    def set_speed(self, speed_code):
        with self._serial_lock:
            self.syringe.setSpeed(speed_code)

    def set_wait(self, time_s):
        with self._serial_lock:
            self.syringe.delayExec(time_s * 1000)

    def reset_chain(self):
        with self._serial_lock:
            self.syringe.resetChain()
        self.chained_volume = 0

    def execute(self):
        # wait_for_stop is the only waiting path.
        if not self._arm():
            return
        self.is_busy = True
        with self._serial_lock:
            t = self.syringe.executeChain(minimal_reset=True)
        try:
            self.wait_for_stop(t)
        except BaseException:
            # Unwinding on a cancel. The position read keeps the volume
            # bookkeeping honest for the park-to-waste close, but the pump is
            # still decelerating from terminateCmd and the read can fail --
            # and a driver error here would replace the cancellation, so the
            # operator sees a pump fault instead of their own abort.
            try:
                self.get_plunger_position()
            except Exception:
                _logger.warning("Plunger position unreadable after the halt; "
                                "volume bookkeeping may be off until the next move.",
                                exc_info=True)
            raise
        finally:
            self.chained_volume = 0
        self.get_plunger_position()

    def get_time_to_finish(self):
        with self._serial_lock:
            return self.syringe.exec_time

    def dispense(self, port, volume, speed_code):
        if self.is_aborted:
            return
        with self._serial_lock:
            self.syringe.setSpeed(self.effective_speed_code(speed_code))
            self.syringe.dispense(port, volume)
            t = self.syringe.exec_time
        self.chained_volume = self.chained_volume - volume
        return t

    def extract(self, port, volume, speed_code):
        if self.is_aborted:
            return
        with self._serial_lock:
            self.syringe.setSpeed(self.effective_speed_code(speed_code))
            self.syringe.extract(port, volume)
            t = self.syringe.exec_time
        self.chained_volume = self.chained_volume + volume
        return t

    def dispense_to_waste(self, speed_code=None):
        if self.is_aborted:
            return
        with self._serial_lock:
            self.syringe.setSpeed(self.effective_speed_code(speed_code))
            self.syringe.dispenseToWaste(retain_port=False)
            t = self.syringe.exec_time
        self.chained_volume = 0
        return t

    def _terminate(self):
        # Called from other threads (the GUI's abort, the reader thread's
        # flow-fault stop). The lock only makes it queue behind an in-flight
        # round trip -- or, rarely, the driver's own error-recovery sequence
        # -- never behind a move.
        with self._serial_lock:
            self.syringe.terminateCmd()

    def _move_finished(self):
        with self._serial_lock:
            return self.syringe._checkReady()

    def close(self, to_waste=False):
        if to_waste:
            self.dispense_to_waste(self.speed_code_limit)
            self.execute()
        del self.com_link

class SyringePumpSimulation(SpeedCodes, Interruptible):
    """Simulation counterpart that remembers what it was asked to do.

    Chains are recorded the way the real pump runs them: extract / dispense /
    dispense_to_waste queue op tuples, and execute() replays the queue into
    `executed` (one list of ops per executed chain) while updating the held
    volume. get_current_volume() and get_chained_volume() therefore do real
    accounting -- the operations layer's overflow and tubing arithmetic reads
    them, and a simulation that returned constants (as this one used to)
    exempted all of that arithmetic from every test.

    Op tuples, carrying the speed the real pump would set (the shared
    SpeedCodes.effective_speed_code, so the two cannot drift):
        ("extract",  port, volume_ul, effective_speed_code)
        ("dispense", port, volume_ul, effective_speed_code)
        ("dispense_to_waste", effective_speed_code)

    The plunger starts mid-stroke, as the old constant simulation reported,
    so existing tests keep the same headroom before an emptying dump.

    Interrupted moves are deliberately not modeled: an execute() that stop()
    or abort() wakes early still folds the whole chain, where a real pump
    reads back a partial plunger position. What an interrupted chain leaves
    behind is a semantic the cancellation redesign owns (its first step is
    making this simulation honour abort, with tests) -- a guess here would be
    baked into every test written against it in the meantime.
    """

    def __init__(self, sn, syringe_ul, speed_code_limit, waste_port, num_ports=4, slope=14,
                 run_control=None):
        self.syringe = None
        self.volume = syringe_ul
        self.speed_code_limit = speed_code_limit
        self.range = 3000
        self._held_ul = 0.5 * syringe_ul
        self._chain = []
        self.executed = []
        self._init_interrupt(run_control)
        self.get_plunger_position()
        _logger.info("Simulated syringe pump.")

    @property
    def executed_ops(self):
        """Every executed op in order, chain boundaries flattened away."""
        return [op for chain in self.executed for op in chain]

    @staticmethod
    def _run(ops, held_ul):
        """Held volume after running `ops` from `held_ul` -- the plunger's
        arithmetic, written once: the queued-volume view is the same fold
        seeded with zero, so there is no separate chained_volume to keep in
        step with it."""
        for op in ops:
            if op[0] == "extract":
                held_ul += op[2]
            elif op[0] == "dispense":
                held_ul -= op[2]
            else:  # dispense_to_waste empties whatever is held at that point
                held_ul = 0
        return held_ul

    def get_plunger_position(self):
        self.plunger_pos = self._held_ul / self.volume
        return self.plunger_pos

    def get_current_volume(self):
        return self.volume * self.plunger_pos

    def get_chained_volume(self):
        return self._run(self._chain, 0)

    def set_speed(self, speed_code):
        pass

    def set_wait(self, time_s):
        pass

    def reset_chain(self):
        self._chain = []

    def execute(self):
        if not self._arm():
            return
        self.is_busy = True
        self.wait_for_stop(5)
        self._held_ul = self._run(self._chain, self._held_ul)
        self.executed.append(self._chain)
        self._chain = []
        self.get_plunger_position()

    def get_time_to_finish(self):
        return 5

    def dispense(self, port, volume, speed_code):
        if self.is_aborted:
            return
        self._chain.append(("dispense", port, volume,
                            self.effective_speed_code(speed_code)))
        return 5

    def extract(self, port, volume, speed_code):
        if self.is_aborted:
            return
        self._chain.append(("extract", port, volume,
                            self.effective_speed_code(speed_code)))
        return 5

    def dispense_to_waste(self, speed_code=None):
        if self.is_aborted:
            return
        self._chain.append(("dispense_to_waste",
                            self.effective_speed_code(speed_code)))
        return 5

    def close(self, to_waste=False):
        pass
