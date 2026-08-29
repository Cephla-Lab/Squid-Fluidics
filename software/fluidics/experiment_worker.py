import logging

from .errors import AbortRequested, Cancelled, RunControl
from .events import Incubating, RunStarted, SequenceCompleted, SequenceStarted
from .subscribers import Subscribers
# Re-exported: defined here before fluidics.errors existed, and scripts
# outside this package may still import it from here.
from .errors import OperationError  # noqa: F401

_logger = logging.getLogger(__name__)


class ExperimentWorker:
    def __init__(self, experiment_ops, plan, config, run_id="run",
                 events=None, make_safe=None, run_control=None):
        """One run of `plan` (fluidics.events.PlanEntry per sequence repeat,
        in run order -- RunSession.start builds it via time_estimate).

        The worker publishes the run's boundary facts on `events`
        (RunStarted from here, SequenceStarted/Incubating/SequenceCompleted
        from the loop) and records how it ended in `outcome`
        ("finished" | "stopped" | "failed"), `message`, `ended_position`
        and `elapsed_seconds` -- the session reads those to publish
        RunEnded once the rig is free.

        make_safe: fn() -> [exceptions it could not act on], called on this
        thread once a run has ended early, aborted or failed
        (devices.build_worker passes DeviceSet.make_safe). run_control: the
        run's cancellation signal, shared with the devices so one abort
        reaches the operation, the incubation wait, and the check between
        sequences alike; private when omitted.
        """
        self.experiment_ops = experiment_ops
        self.plan = tuple(plan)
        self.config = config
        self.run_id = run_id
        self.events = events if events is not None else Subscribers("run events")
        self.make_safe = make_safe
        self.run_control = run_control if run_control is not None else RunControl()

        self.outcome = None          # set by run(): finished | stopped | failed
        self.message = None
        self.ended_position = None   # the plan entry in flight at an early end
        self.elapsed_seconds = 0.0

        # The worker narrates its own run: one source feeds the console and
        # the run log, so the record exists even when nothing is watching.
        _logger.info("Run %s: %d sequence(s), estimated %.0f s.",
                     self.run_id, len(self.plan),
                     sum(entry.duration_seconds for entry in self.plan))
        self.events.notify(RunStarted(self.run_id, self.plan))

    def wait_for_incubation(self, time_minutes):
        # Running time: a pause stops the incubation clock and the remainder
        # resumes with it, which is most of what pause is for.
        self.run_control.delay(time_minutes * 60)

    def _hold_if_paused(self, tag):
        """Park between sequences while the run is paused, saying so.

        Narration is best-effort: a pause arriving just after the read still
        holds the run at the gate, it is simply not narrated. Whoever asked
        for the pause already knows they asked.
        """
        if self.run_control.paused:
            _logger.info("%s: paused", tag)
        # Unconditional: a pause arriving after that read still holds here,
        # at the boundary, rather than inside the sequence about to start.
        self.run_control.checkpoint()

    def _end_early(self, position, message=None):
        """Quiet the rig, then record why it stopped: "stopped" for an abort
        (no message), "failed" otherwise.

        Act, then record: the console and run log already carry the message
        (the handlers log first). What make_safe could not switch off is a
        failure even after an abort -- "the rig could not be made safe" is
        the line that matters.
        """
        self.run_control.release()      # a pending pause must not hold the unwinding
        failures = self.make_safe() if self.make_safe is not None else []
        if failures:
            unsafe = "Making the rig safe failed: " + "; ".join(str(e) for e in failures)
            message = f"{message} {unsafe}" if message else unsafe
        self.outcome = "stopped" if message is None else "failed"
        self.message = message
        self.ended_position = position

    def run(self):
        position = None
        tag = None                       # names the entry in hand for the log
        try:
            for position, entry in enumerate(self.plan):
                tag = (f"Sequence {position + 1}/{len(self.plan)} "
                       f"({entry.label})")
                # A cancel that landed between sequences must not start the
                # next one, and a pause holds here -- the sequence before it
                # finished, this one has not begun. Inside a sequence, the
                # device that was waiting answers for both.
                self._hold_if_paused(tag)
                _logger.info("%s: started", tag)
                self.events.notify(SequenceStarted(self.run_id, position))
                self.experiment_ops.process_sequence(entry.sequence)
                # Every wait inside an operation raises on a cancel, so this
                # covers only the tail: a cancel landing after the
                # operation's last wait and before it returns. A sequence
                # the operator cancelled must not be reported completed.
                self.run_control.check()

                incubation_time = entry.sequence.get('incubation_time', 0)
                if incubation_time > 0:
                    _logger.info("%s: incubating %.1f min", tag, incubation_time)
                    self.events.notify(Incubating(self.run_id, position,
                                                  incubation_time))
                    self.wait_for_incubation(incubation_time)
                _logger.info("%s: completed", tag)
                self.events.notify(SequenceCompleted(self.run_id, position))
            self.outcome = "finished"

        except AbortRequested:
            _logger.warning("Run aborted by user.")
            self._end_early(position)  # an abort is a stop, not an error
        except Cancelled as fault:
            # The instrument stopped itself -- a flow fault, say. Recorded
            # with its diagnosis, never as an abort.
            message = f"{tag}: {fault}" if tag else str(fault)
            _logger.error(message)
            self._end_early(position, message)
        except Exception as e:
            # Named the way the operator just saw it when a sequence was in
            # hand; outside any sequence -- the plan itself failing -- it is
            # a programming error, where the traceback matters most.
            message = f"{tag}: {e}" if tag else str(e)
            _logger.error(message, exc_info=True)
            self._end_early(position, message)
        finally:
            self.elapsed_seconds = self.run_control.running_seconds()
            _logger.info("Run finished.")
