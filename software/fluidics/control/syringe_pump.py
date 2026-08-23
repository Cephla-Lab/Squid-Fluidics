import fluidics.control.tecancavro as tecancavro
import threading
from serial.tools import list_ports


class Interruptible:
    """Halting a move that is already running, shared by both pump classes.

    Two ways to interrupt, differing only in whether they latch:

      abort()  the operator cancelled. Latches until reset_abort(), so every
               later operation returns early.
      stop()   a fault the caller is about to raise on -- a flow fault, say.
               Does not latch: the run is being failed by whoever called it,
               and callers read is_aborted to mean "the operator cancelled",
               so latching here would report a hardware problem as a user
               action and silently disable the rest of the run.

    Shared rather than written twice because the simulation is where these
    semantics get tested -- the real pump needs hardware. Two copies would put
    the tested one and the shipped one out of reach of each other, which is
    exactly how the sleep-through-abort bug survived 230 passing tests.

    Subclasses supply the two hardware-shaped pieces: _terminate() to halt the
    plunger, and _move_finished() to say whether it has stopped.
    """

    def _init_interrupt(self):
        self.is_busy = False
        self.is_aborted = False
        self._interrupt = threading.Event()

    # --- what a real pump does and a simulated one cannot ---

    def _terminate(self):
        """Halt the plunger. Nothing to halt in simulation."""

    def _move_finished(self):
        """Whether the move has ended. A simulated move ends when its
        estimated duration does, so there is nothing further to wait for."""
        return True

    # --- interruption ---

    def abort(self):
        self._terminate()
        self.is_aborted = True
        self._interrupt.set()

    def reset_abort(self):
        self.is_aborted = False
        self._interrupt.clear()

    def stop(self):
        self._terminate()
        self._interrupt.set()

    def _arm(self):
        """Clear any stale interrupt and report whether a chain may run.

        Clearing before reading is_aborted, and abort() setting the flag before
        the event, is what makes an abort landing anywhere around here still
        count: either the flag is already true and the caller returns, or the
        event is set afterwards and wait_for_stop wakes on it.
        """
        self._interrupt.clear()
        return not self.is_aborted

    def wait_for_stop(self, t=0):
        """Block until the move finishes, or until abort()/stop() interrupts.

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
    def __init__(self, sn, syringe_ul, speed_code_limit, waste_port, num_ports=4, slope=14, debug=False):
        if sn is not None:
            for d in list_ports.comports():
                if d.serial_number == sn:
                    self.port = d.device
                    self.com_link = tecancavro.TecanAPISerial(tecan_addr=0, ser_port=self.port, ser_baud=9600)
                    print("Syringe pump found.")
                    break
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

        self.get_plunger_position()
        self._init_interrupt()

        print("Syringe pump initialized.")

    def get_plunger_position(self):
        position = self.syringe.getPlungerPos()
        self.plunger_pos = position / self.range
        return self.plunger_pos

    def get_current_volume(self):
        return self.volume * self.plunger_pos  # ul

    def get_chained_volume(self):
        return self.chained_volume  # ul

    def set_speed(self, speed_code):
        self.syringe.setSpeed(speed_code)

    def set_wait(self, time_s):
        self.syringe.delayExec(time_s * 1000)

    def reset_chain(self):
        self.syringe.resetChain()
        self.chained_volume = 0

    def execute(self, block_pump=False):
        if not self._arm():
            return
        self.is_busy = True
        t = self.syringe.executeChain(minimal_reset=True)
        if block_pump:
            self.syringe.waitReady()
            self.is_busy = False
        else:
            self.wait_for_stop(t)
        self.get_plunger_position()
        self.chained_volume = 0

    def get_time_to_finish(self):
        return self.syringe.exec_time

    def dispense(self, port, volume, speed_code):
        if self.is_aborted:
            return
        self.set_speed(self.effective_speed_code(speed_code))
        self.syringe.dispense(port, volume)
        self.chained_volume = self.chained_volume - volume
        return self.get_time_to_finish()

    def extract(self, port, volume, speed_code):
        if self.is_aborted:
            return
        self.set_speed(self.effective_speed_code(speed_code))
        self.syringe.extract(port, volume)
        self.chained_volume = self.chained_volume + volume
        return self.get_time_to_finish()

    def dispense_to_waste(self, speed_code=None):
        if self.is_aborted:
            return
        self.set_speed(self.effective_speed_code(speed_code))
        self.syringe.dispenseToWaste(retain_port=False)
        self.chained_volume = 0
        return self.get_time_to_finish()

    def _terminate(self):
        self.syringe.terminateCmd()

    def _move_finished(self):
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

    def __init__(self, sn, syringe_ul, speed_code_limit, waste_port, num_ports=4, slope=14):
        self.syringe = None
        self.volume = syringe_ul
        self.speed_code_limit = speed_code_limit
        self.range = 3000
        self._held_ul = 0.5 * syringe_ul
        self._chain = []
        self.executed = []
        self._init_interrupt()
        self.get_plunger_position()
        print("Simulated syringe pump.")

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

    def execute(self, block_pump=False):
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
