"""The robot subpackage is pump-agnostic and driver-optional (spec D3)."""
from tests.conftest import in_a_fresh_interpreter

CODE = """
import sys
import fluidics.robot.labware, fluidics.robot.simulation, fluidics.robot.sipper, fluidics.robot.stage
bad = [m for m in sys.modules if m.startswith("fluidics.control") or m.startswith("servo_service")
       or m.startswith("PyQt5") or m.startswith("qtpy")]
print(",".join(sorted(bad)))
"""


def test_the_robot_modules_import_nothing_from_control_the_driver_or_qt():
    assert in_a_fresh_interpreter(CODE) == ""
