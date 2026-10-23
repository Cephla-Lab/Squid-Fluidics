"""The one place that knows where the servo driver lives.

The driver is nm-servo's `servo_service`, vendored as `fluidics.control.nimotion`
(see its VENDORED.md for the source commit and the maintenance rule; open
question 4 of the design, decided 2026-10-12). Nothing else is tried: the
driver a rig runs is the one in the repository, never an installed package.
The stage above sees the service protocol in stage.py. A copy that is
missing, or that raises ImportError (a dependency it needs is absent), fails
at bring-up with the line below, which names the import error; a copy that
is present but broken otherwise (a SyntaxError, say) fails at import.
DriverService is the adapter: it turns every driver or port exception into
DeviceError naming the axis, so the stage's early-end path (halt, re-read,
raise) runs for all of them.

Its poll (DriverService.read) asks each drive for three registers, not the
six the driver's get_axis_status reads: the status word, the error code and
the position, decoded as get_axis_status decodes them. The driver's own
simulator agrees with get_axis_status read for read
(tests/unit/robot/test_driver.py), but
this read path wants a bench check on the real drives (the X1/Y2/Z3 stage)
before it is trusted on a rig.
"""

from ..errors import DeviceError
from .stage import AxisReading, HomingReading, _label

try:
    from ..control.nimotion import simulation      # the driver's own sim:// drives, for tests
    from ..control.nimotion.high_level_api import AxisName, ServoService
    from ..control.nimotion.high_level_api.axis_config import DEFAULT_AXIS_CONFIGS
    from ..control.nimotion.motor_control import (DriveState, decode_status_word, is_fault,
                                                  is_target_reached)
    DRIVER_AVAILABLE = True
    _IMPORT_ERROR = None
except ImportError as e:
    AxisName = ServoService = DEFAULT_AXIS_CONFIGS = simulation = None
    DriveState = decode_status_word = is_fault = is_target_reached = None
    DRIVER_AVAILABLE = False
    _IMPORT_ERROR = e                               # kept for the bring-up error to name

INSTALL_LINE = ("the robot driver did not import: fluidics/control/nimotion is the vendored "
                "copy of nm-servo's servo_service (see its VENDORED.md); restore it from the "
                "recorded commit")


def _install_line():
    """INSTALL_LINE, naming the import error behind it when there is one."""
    if _IMPORT_ERROR is None:
        return INSTALL_LINE
    return f"{INSTALL_LINE} (import failed: {_IMPORT_ERROR})"


# CiA402 statusword bits and the homing mode number (NiMotion manual B06;
# nm-servo's StatusWordBits and OperationMode carry the same values).
_FAULT = 0x0008
_HOMING_ATTAINED = 0x1000
_HOMING_ERROR = 0x2000
_HOMING_MODE = 6


class DriverService:
    """ServoService, in the shape XYZStage speaks (stage.py's service
    protocol): non-blocking moves and homing, a waited controlled stop,
    the homing bits and mode read from the drive, the limit inputs once a
    target reads reached, strokes from the axis table, every failure a
    DeviceError naming the axis."""

    def __init__(self, port, baudrate, names):
        self._names = names                       # "x"/"y"/"z" -> AxisName
        self._configs = {name: DEFAULT_AXIS_CONFIGS[name] for name in names.values()}
        self._service = ServoService(port=port, baudrate=baudrate, axis_configs=self._configs)

    def _call(self, axis, what, fn):
        try:
            return fn()
        except Exception as e:
            raise DeviceError(f"axis {_label(axis)}: {what} failed: {e}") from e

    def connect(self):
        # The driver verifies and initializes the *current* axis only, and
        # its default is Z4, an axis this rig lacks: select ours first,
        # then initialize each mapped axis fail-fast (initialize_all_motors
        # logs and carries on, which is not what a bring-up wants).
        self._service.current_axis = self._names["z"]
        self._call(self._names["z"], "connect", self._service.connect)
        for axis in self._configs:
            self._call(axis, "parameter initialization",
                       lambda axis=axis: self._service.initialize_motor_parameters(axis, verbose=False))

    def disconnect(self):
        self._call(None, "disconnect", self._service.disconnect)

    def enable(self, axis):
        self._call(axis, "enable", lambda: self._service.enable(axis))

    def disable(self, axis):
        self._call(axis, "disable", lambda: self._service.disable(axis))

    def engage_brake(self, axis):
        if self._configs[axis].has_brake:
            self._call(axis, "brake", lambda: self._service.engage_brake(axis))

    def stroke(self, axis):
        config = self._configs[axis]
        return config.stroke_min, config.stroke_max

    def homing_timeout_s(self, axis):
        return self._configs[axis].homing_timeout / 1000.0

    def move_to(self, position_mm, velocity_mm_s, axis):
        self._call(axis, "move", lambda: self._service.move_to(
            position_mm, velocity_mm_s=velocity_mm_s, axis=axis, wait=False))

    def home(self, axis):
        self._call(axis, "homing", lambda: self._service.home(axis, wait=False))

    def homing_state(self, axis):
        def read():
            motor = self._service.get_motor(axis)
            word = motor.read_status_word()
            mode = motor.read_operation_mode()
            return HomingReading(bool(word & _HOMING_ATTAINED), bool(word & _HOMING_ERROR),
                                 bool(word & _FAULT), mode == _HOMING_MODE)
        return self._call(axis, "status read", read)

    def read(self, axis):
        def read():
            # What the stage polls on, and only that: get_axis_status would
            # also read velocity, torque and the operation mode, three
            # requests on the shared RS-485 line per axis per poll that
            # nothing here reads. Decoded as get_axis_status decodes them --
            # enabled is the driver's state machine reading the word as
            # Operation Enabled, not one bit. Disconnected, get_motor raises
            # (where get_axis_status would read a zeroed, disabled axis): a
            # status read failed, which the stage handles as any other.
            motor = self._service.get_motor(axis)
            word = motor.read_status_word()
            error_code = motor.read_error_code()
            position_mm = self._configs[axis].pulses_to_mm(motor.read_position())
            reached = is_target_reached(word)
            # The limit inputs (60FDh bits 0-1) only once the target reads
            # reached, as the driver's own wait asks them: a move that ends
            # on an active switch is not a completed move. One read more at
            # the end of a leg, none per poll.
            on_limit = reached and bool(motor.read_digital_inputs() & 0b11)
            return AxisReading(position_mm, reached,
                               decode_status_word(word) == DriveState.OPERATION_ENABLED,
                               is_fault(word), error_code, on_limit)
        return self._call(axis, "status read", read)

    def stop(self, axis):
        self._call(axis, "stop", lambda: self._service.stop(axis, wait=True))

    def reset_fault(self, axis):
        self._call(axis, "fault reset", lambda: self._service.fault_reset(axis))


def open_service(port, baudrate, axis_names):
    """A DriverService for the three axes `axis_names` maps ("x"/"y"/"z" ->
    driver axis names such as "X1") and the mapping as the driver's own
    names, which the stage must be built with. DeviceError with the
    install line when no driver is present, or naming an axis the driver
    does not know."""
    if not DRIVER_AVAILABLE:
        raise DeviceError(_install_line()) from _IMPORT_ERROR
    names = {}
    for key, value in axis_names.items():
        try:
            names[key] = AxisName(value)
        except ValueError:
            raise DeviceError(f"the robot driver has no axis named {value!r} for {key}; "
                              f"it knows {[axis.value for axis in AxisName]}") from None
    return DriverService(port, baudrate, names), names
