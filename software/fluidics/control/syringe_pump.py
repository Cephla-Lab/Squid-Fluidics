import logging
import threading
import time

import fluidics.control.tecancavro as tecancavro

from ..errors import Cancelled, RunControl
from .discovery import find_serial_port

_logger = logging.getLogger(__name__)


class Interruptible:
    """Running a chain of moves under the run's RunControl, shared by both
    pump classes.

    Moves are queued by extract() / dispense() / dispense_to_waste() as op
    tuples, carrying the speed the pump will actually run (the shared
    SpeedCodes.effective_speed_code, so the two classes cannot drift):
        ("extract",  port, volume_ul, speed_code)
        ("dispense", port, volume_ul, speed_code)
        ("dispense_to_waste", speed_code)
    and execute() runs them one at a time, each through the run's gate.

    A cancel -- the operator pressed Abort, or draw protection raised a flow
    fault -- wakes the thread inside wait_for_stop, which halts the plunger on
    the thread that owns the port and raises the cause out of execute(), so
    the operation unwinds instead of returning as if it had finished. The
    cause latches until RunControl.reset().

    A pause halts the op in flight the same way, then parks at the gate --
    counted in `holding`, so the GUI's *paused* means what it says -- and
    when the gate opens on a resume re-issues what the op had left. One op
    at a time is what makes that simple: the interrupted op is the one that
    was dispatched, and nothing has to be worked out from where the plunger
    stopped. A cancel while parked raises before anything is re-issued.

    Shared rather than written twice because the simulation is where these
    semantics get tested -- the real pump needs hardware. Subclasses supply
    the hardware-shaped pieces: halt() and _move_finished() for the plunger,
    _estimate(op) for what a queued op will take, and _dispatch() /
    _halted() / _finished() for the three moments of running one.
    """

    def _init_run_control(self, run_control=None):
        self.is_busy = False
        self.run_control = run_control if run_control is not None else RunControl()
        self._chain = []

    # --- queueing ---

    def extract(self, port, volume, speed_code):
        return self._queue(("extract", port, volume,
                            self.effective_speed_code(speed_code)))

    def dispense(self, port, volume, speed_code):
        return self._queue(("dispense", port, volume,
                            self.effective_speed_code(speed_code)))

    def dispense_to_waste(self, speed_code=None):
        return self._queue(("dispense_to_waste",
                            self.effective_speed_code(speed_code)))

    def _queue(self, op):
        """Queue `op`; return the pump's time estimate for it, which the
        manual tab's progress bar reads."""
        self._chain.append(op)
        return self._estimate(op)

    def reset_chain(self):
        self._chain = []

    def get_chained_volume(self):
        """What the queue would add to the held volume: the same fold as the
        plunger's, seeded with zero, so there is no separate count to keep in
        step with it."""
        return self._held_after(self._chain, 0)

    def get_time_to_finish(self):
        return sum(self._estimate(op) for op in self._chain)

    def get_current_volume(self):
        return self.volume * self.plunger_pos  # ul

    @staticmethod
    def _held_after(ops, held_ul):
        """Held volume after running `ops` from `held_ul`."""
        for op in ops:
            if op[0] == "extract":
                held_ul += op[2]
            elif op[0] == "dispense":
                held_ul -= op[2]
            else:  # dispense_to_waste empties whatever is held at that point
                held_ul = 0
        return held_ul

    # --- what a real pump does and a simulated one cannot ---

    def halt(self):
        """Stop the plunger, whatever it is doing. Nothing to stop in
        simulation. Called by the thread inside wait_for_stop when the run
        stops, and by DeviceSet.make_safe once a run has ended early."""

    def _move_finished(self):
        """Whether the move has ended. A simulated move ends when its
        estimated duration does, so there is nothing further to wait for."""
        return True

    # --- running ---

    def execute(self):
        """Run the queued ops in order. Returns when the last has finished;
        later than estimated if the run was paused on the way. Raises the
        run's cause if it is cancelled, before or during."""
        # Consumed either way, as the Tecan's chain is: a cancelled chain
        # must not resurface as chained volume the next call reports.
        chain, self._chain = self._chain, []
        # The gate first, even for an empty chain: an execute() after a
        # cancel raises rather than returning as if it had run.
        self.run_control.checkpoint()
        self.is_busy = True
        try:
            for op in chain:
                self._run_op(op)
        finally:
            self.is_busy = False

    def _run_op(self, op):
        """Run one op to the end, however many pauses that takes."""
        target = None
        while True:
            self.run_control.checkpoint()
            estimate, target = self._dispatch(op, target)
            if not self.wait_for_stop(estimate):
                self._finished(op, target)
                return
            self._halted(op, target)

    def wait_for_stop(self, t=0):
        """Block until the move finishes or the run stops. Returns False when
        the move finished; True when a pause cut it short -- the plunger has
        been halted, here, on the thread that owns the port, and the caller
        finishes the move on resume. On a cancel, halt likewise, then raise
        the cause.

        t is the pump's estimate of how long the move will take -- about 240 s
        for a 2000 uL draw at 500 uL/min -- so it only gates when we start
        asking: _move_finished() is the authoritative end-of-move signal, and
        waiting on the run's signal returns the moment it stops.
        """
        stopped = self.run_control.wait_interrupted(t)
        while not stopped and not self._move_finished():
            stopped = self.run_control.wait_interrupted(0.5)
        if not stopped:
            return False
        # This also closes the window between the gate in execute() and the
        # dispatch: a cancel landing there returns from the wait at once and
        # halts the move it could not prevent.
        try:
            self.halt()
        except Exception as e:
            # The stop still has to reach the caller -- the worker's safety
            # cleanup depends on a cancel getting through -- so the halt
            # failure is reported here, loudly, rather than replacing it.
            _logger.error("Halting the plunger failed; the pump may still be "
                          "moving: %s", e, exc_info=True)
        self.run_control.check()
        return True


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

        # Every touch of self.syringe -- a wire round trip or the driver's
        # chain-building state -- happens under this lock. The GUI's plunger
        # poll runs on the Qt thread while a worker thread drives moves;
        # unlocked, the poll could consume the reply belonging to the
        # worker's _checkReady, surfacing as a spurious TecanAPITimeout that
        # the manual tab used to mask as "operation complete". Held per
        # driver call, never across a move, so position polls and a cancel's halt
        # stay live while the plunger runs. A plain (non-reentrant) Lock on
        # purpose: locked regions never nest, and a future violation should
        # deadlock loudly in testing, not silently interleave.
        self._serial_lock = threading.Lock()

        self.get_plunger_position()
        self._init_run_control(run_control)

        _logger.info("Syringe pump initialized.")

    def get_plunger_position(self):
        with self._serial_lock:
            position = self.syringe.getPlungerPos()
        self.plunger_pos = position / self.range
        return self.plunger_pos

    def reset_chain(self):
        with self._serial_lock:
            self.syringe.resetChain()
        super().reset_chain()

    # --- the ops, in the driver's terms ---

    def _estimate(self, op):
        """The driver's own time estimate for `op`: built on its chain for the
        arithmetic and discarded, since ops are dispatched one at a time from
        execute() rather than as the chain the driver was building."""
        with self._serial_lock:
            self._build(op)
            t = self.syringe.exec_time
            self.syringe.resetChain()
        return t

    def _build(self, op):
        """Queue `op` on the driver's chain as the relative move it has always
        been: the same bytes an unpaused run has always sent."""
        kind, code = op[0], op[-1]
        self.syringe.setSpeed(code)
        if kind == "extract":
            self.syringe.extract(op[1], op[2])
        elif kind == "dispense":
            self.syringe.dispense(op[1], op[2])
        else:
            self.syringe.dispenseToWaste(retain_port=False)

    def _target(self, op, start):
        """Where the plunger will be when `op` is done, in the driver's steps,
        from a real reading of where it is now."""
        kind = op[0]
        if kind == "dispense_to_waste":
            return 0
        steps = self.syringe._ulToSteps(op[2])
        return start + steps if kind == "extract" else start - steps

    def _port(self, op):
        return self.syringe.waste_port if op[0] == "dispense_to_waste" else op[1]

    def _dispatch(self, op, target):
        """Send `op` -- or, with `target` set, what is left of it after a
        pause -- and return (estimate, target).

        The remainder is an absolute move to the op's target rather than a
        relative move worked out from a position read after the halt: the
        target was fixed from a reading taken before the op started, so no
        read on the decelerating pump is on the path that decides how much
        liquid moves.
        """
        with self._serial_lock:
            start = self.syringe.getPlungerPos()
            # The driver's own bookkeeping starts from the truth too, so its
            # time estimate for an absolute move does.
            self.syringe.updateSimState()
            if target is None:
                target = self._target(op, start)
                self._build(op)
            else:
                _logger.info("Resuming the move: %d steps to go.", abs(target - start))
                self.syringe.setSpeed(op[-1])
                self.syringe.changePort(self._port(op))
                self.syringe.movePlungerAbs(target)
            t = self.syringe.executeChain(minimal_reset=True)
        self.plunger_pos = start / self.range
        return t, target

    def _halted(self, op, target):
        """The plunger has been told to stop for a pause: let it, then record
        where it stopped. The reading is bookkeeping and log -- the resume
        does not depend on it -- so a failed read is reported, not raised."""
        deadline = time.monotonic() + 5
        while not self._move_finished():
            if time.monotonic() > deadline:
                raise RuntimeError("the plunger did not stop within 5 s of "
                                   "being halted for the pause")
            time.sleep(0.05)
        try:
            self.get_plunger_position()
        except Exception as e:
            _logger.warning("Plunger position unreadable after the halt (%s); "
                            "the remainder still runs on resume.", e)
            return
        steps = round(self.plunger_pos * self.range)
        _logger.info("Paused mid-move at %d steps, %d to go.", steps, abs(target - steps))

    def _finished(self, op, target):
        pass

    def execute(self):
        try:
            super().execute()
        except Cancelled:
            # The position read keeps the volume bookkeeping honest for the
            # park-to-waste close, but the pump is still decelerating from
            # terminateCmd and the read can fail -- and a driver error here
            # would replace the cancellation, so the operator would see a pump
            # fault instead of their own abort.
            try:
                self.get_plunger_position()
            except Exception as e:
                _logger.warning("Plunger position unreadable after the halt "
                                "(%s); volume bookkeeping may be off until the "
                                "next move.", e)
            raise
        self.get_plunger_position()

    def halt(self):
        # The lock only makes this queue behind an in-flight round trip -- or,
        # rarely, the driver's own error-recovery sequence -- never behind a
        # move. Called from the thread inside wait_for_stop, and from
        # make_safe on the worker thread once a run has ended.
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

    execute() replays the queued ops into `executed` -- one list per executed
    chain -- while moving the held volume, so get_current_volume() and
    get_chained_volume() do real accounting: the operations layer's overflow
    and tubing arithmetic reads them, and a simulation that returned constants
    (as this one used to) exempted all of that arithmetic from every test.

    A pause splits the op in flight where it lands -- the fraction of the
    estimate that had elapsed -- and records both pieces in the same chain,
    so a test can pin that a paused-and-resumed operation moves exactly the
    liquid an uninterrupted one does. A dump is recorded once, on completion.
    Under the suite's fake clock every wait "takes" its whole estimate, so a
    pause there splits at the end; the split tests run on the real clock.

    The plunger starts mid-stroke, as the old constant simulation reported,
    so existing tests keep the same headroom before an emptying dump.
    """

    ESTIMATE_SECONDS = 5

    def __init__(self, sn, syringe_ul, speed_code_limit, waste_port, num_ports=4, slope=14,
                 run_control=None):
        self.syringe = None
        self.volume = syringe_ul
        self.speed_code_limit = speed_code_limit
        self.range = 3000
        self._held_ul = 0.5 * syringe_ul
        self.executed = []
        self._current = []
        self._init_run_control(run_control)
        self.get_plunger_position()
        _logger.info("Simulated syringe pump.")

    @property
    def executed_ops(self):
        """Every executed op in order, chain boundaries flattened away."""
        return [op for chain in self.executed for op in chain]

    def get_plunger_position(self):
        self.plunger_pos = self._held_ul / self.volume
        return self.plunger_pos

    def get_current_volume(self):
        # The held volume itself, not volume * (held / volume): the round trip
        # turns 2800 into 2800.0000000000005 and a test's == into approx.
        return self._held_ul

    def _estimate(self, op):
        return self.ESTIMATE_SECONDS

    def execute(self):
        self._current = []
        super().execute()
        self.get_plunger_position()

    def _record(self, piece):
        if not self._current:
            self.executed.append(self._current)
        self._current.append(piece)

    def _dispatch(self, op, target):
        if target is None:
            target = self._held_after([op], self._held_ul)
            self._split = False
            # Signed, and the op's own figure rather than target - held, so an
            # uninterrupted op is recorded with exactly the volume queued.
            if op[0] == "extract":
                self._to_move = op[2]
            elif op[0] == "dispense":
                self._to_move = -op[2]
            else:
                self._to_move = -self._held_ul
        self._dispatched_at = time.monotonic()
        return self.ESTIMATE_SECONDS, target

    def _halted(self, op, target):
        fraction = min(1.0, (time.monotonic() - self._dispatched_at)
                       / self.ESTIMATE_SECONDS)
        moved = fraction * self._to_move
        if moved and op[0] != "dispense_to_waste":
            self._record((op[0], op[1], abs(moved), op[3]))
            self._split = True
        self._held_ul += moved
        self._to_move -= moved
        self.get_plunger_position()

    def _finished(self, op, target):
        if op[0] == "dispense_to_waste":
            self._record(op)
        elif not (self._split and self._to_move == 0):
            # A zero-volume op queued is recorded as such; a split op whose
            # remainder was nothing (the pause landed as it ended) is not.
            self._record((op[0], op[1], abs(self._to_move), op[3]))
        self._held_ul = target
        self.get_plunger_position()

    def close(self, to_waste=False):
        pass
