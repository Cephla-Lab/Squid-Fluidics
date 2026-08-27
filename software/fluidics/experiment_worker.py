import logging

from .errors import AbortRequested, Cancelled, RunControl
# Re-exported: defined here before fluidics.errors existed, and scripts
# outside this package may still import it from here.
from .errors import OperationError  # noqa: F401

_logger = logging.getLogger(__name__)


class ExperimentWorker:
    def __init__(self, experiment_ops, sequences, config, callbacks=None,
                 run_control=None):
        """
        Initialize ExperimentWorker with callbacks instead of signals.

        Args:
            experiment_ops: The experiment operations object
            sequences: list[dict] of validated sequence dicts, each with a 'type' key
            config: Configuration object
            callbacks: Dictionary of callback functions with keys:
                - 'update_progress': fn(index, sequence_num, status)
                - 'on_error': fn(error_message)
                - 'on_finished': fn()
                - 'on_estimate': fn(time_to_finish, n_sequences)
                - 'make_safe': fn() -> [exceptions it could not act on] --
                  called on the worker thread once a run has ended early,
                  aborted or failed, before on_error; its failures are
                  appended to the report (devices.build_worker passes
                  DeviceSet.make_safe)
            run_control: the run's cancellation signal, shared with the
                devices (DeviceSet.run_control) so one abort reaches the
                operation, the incubation wait, and the check between
                sequences alike. Private when omitted.
        """

        self.experiment_ops = experiment_ops
        self.sequences = sequences
        self.config = config
        self.callbacks = callbacks or {}
        self.run_control = run_control if run_control is not None else RunControl()

        self.time_to_finish, self.n_sequences = self.get_time_to_finish()
        # The worker narrates its own run: one source feeds the console, the
        # run log, and (via callbacks) whatever UI is attached, so the record
        # exists even when nothing is watching.
        _logger.info("Run of %d sequence(s), estimated %.0f s.",
                     self.n_sequences, self.time_to_finish)
        self._call_callback('on_estimate', self.time_to_finish, self.n_sequences)

    def _call_callback(self, name, *args):
        """Call the callback if one is registered; return what it returns."""
        callback = self.callbacks.get(name)
        if callback:
            return callback(*args)
        return None

    def get_time_to_finish(self):
        total_time = 0
        total_sequences = 0
        for seq in self.sequences:
            if seq['type'] == "set_temperature":
                t = seq.get('incubation_time', 0) * 60 + 60
            else:
                t = seq.get('volume', 0) / max(seq.get('flow_rate', 1), 1) * 60
                if seq.get('fill_tubing_with'):
                    t += self.config.reagent_selection.common_tubing_fluid_amount_ul / max(seq.get('flow_rate', 1), 1) * 60 + 1
                if seq.get('incubation_time', 0) > 0:
                    t += seq['incubation_time'] * 60
                t += 2
            repeat = seq.get('repeat', 1)
            t = t * repeat
            total_time += t
            total_sequences += repeat
        return total_time, total_sequences

    def wait_for_incubation(self, time_minutes):
        # Running time: a pause stops the incubation clock and the remainder
        # resumes with it, which is most of what pause is for.
        self.run_control.delay(time_minutes * 60)

    def _hold_if_paused(self, index, sequence_number, tag):
        """Park between sequences while the run is paused, saying so.

        Reporting is best-effort: a pause arriving just after the check still
        holds the run at the gate, it is simply not narrated. Whoever asked
        for the pause already knows they asked.
        """
        if self.run_control.paused:
            _logger.info("%s: paused", tag)
            self._call_callback('update_progress', index, sequence_number, "Paused")
        # Unconditional: a pause arriving after that read still holds here,
        # at the boundary, rather than inside the sequence about to start.
        self.run_control.checkpoint()

    def _end_early(self, message):
        """Quiet the rig, then report why it stopped.

        Act, then report: the console and run log already carry the message
        (the handlers log first); what trails make_safe's round trips is the
        GUI's dialog and the manual tab's return, which is the point. What
        make_safe could not switch off is appended to the report -- after an
        abort, "the rig could not be made safe" is the line that matters.
        """
        self.run_control.release()      # a pending pause must not hold the unwinding
        failures = self._call_callback('make_safe') or []
        if failures:
            message += (" Making the rig safe failed: "
                        + "; ".join(str(e) for e in failures))
        self._call_callback('on_error', message)

    def run(self):
        current_sequence = 0
        # Both name the sequence in hand, the way the operator saw it; the
        # handlers read them only under `if tag`, and they are bound together.
        tag = None
        try:
            for index, seq in enumerate(self.sequences):
                label = seq.get('name') or seq['type']
                for repeat in range(1, seq.get('repeat', 1) + 1):
                    current_sequence += 1
                    tag = f"Sequence {current_sequence}/{self.n_sequences} ({label})"
                    # A cancel that landed between sequences must not start
                    # the next one, and a pause holds here -- the sequence
                    # before it finished, this one has not begun. Inside a
                    # sequence, the device that was waiting answers for both.
                    self._hold_if_paused(index, current_sequence, tag)
                    _logger.info("%s: started", tag)
                    self._call_callback('update_progress', index, current_sequence, "Started")
                    self.experiment_ops.process_sequence(seq)
                    # Every wait inside an operation raises on a cancel, so
                    # this covers only the tail: a cancel landing after the
                    # operation's last wait and before it returns. A sequence
                    # the operator cancelled must not be reported Completed.
                    self.run_control.check()

                    incubation_time = seq.get('incubation_time', 0)
                    if incubation_time > 0:
                        _logger.info("%s: incubating %.1f min", tag, incubation_time)
                        self._call_callback('update_progress', index, current_sequence, "Incubating")
                        self.wait_for_incubation(incubation_time)
                    _logger.info("%s: completed", tag)
                    self._call_callback('update_progress', index, current_sequence, "Completed")

        except AbortRequested:
            _logger.warning("Run aborted by user.")
            self._end_early("Operation aborted by user")
        except Cancelled as fault:
            # The instrument stopped itself -- a flow fault, say. Reported
            # with its diagnosis, never as an abort.
            message = f"{tag}: {fault}" if tag else str(fault)
            _logger.error(message)
            self._end_early(message)
        except Exception as e:
            # Named the way the operator just saw it when a sequence was in
            # hand; outside any sequence -- the sequence list itself failing
            # -- it is a programming error, where the traceback matters most.
            message = (f"{tag}: failed on repeat {repeat}: {e}" if tag else str(e))
            _logger.error(message, exc_info=True)
            self._end_early(message)
        finally:
            _logger.info("Run finished.")
            self._call_callback('on_finished')
