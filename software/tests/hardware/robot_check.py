"""Bench check for the sipper robot: python -m tests.hardware.robot_check --config ../config.yaml

Home, jog each axis, teach A1 and the dip depth on each plate, visit every
corner, agitate in a full well and stay there. Run with the deck clear of
anything but the plates. What to watch at each step -- the order homing
moves the axes, the settle error against the stage's and the driver's
tolerances, what a limit stop looks like (the script never provokes one),
the tip at each corner, Z3 holding unpowered (it has no brake: nm-servo
PR #6) -- is the CHECKLIST printed when the script starts.

Nothing moves until Enter is pressed at this console, and the jogs are
typed here: the script holds the robot's port and the volume tracking
file's lock, so the GUI (and its Robot tab) must be closed while it runs. After
every move it prints each axis's settle error -- where the drive reads
the axis once the move has ended, less where the stage sent it -- and
their worst at the end. A failure or Ctrl+C closes without moving (a
halt, no retract).

Run from software/ as a module, so `fluidics` imports without the package
being installed:

    python -m tests.hardware.robot_check --config ../config.yaml
    python -m tests.hardware.robot_check --config sample_config/plate_sipper_config.yaml --simulation
"""
import argparse
import sys

from fluidics.control.config import gantry, load_config, save_config
from fluidics.control.controller import FluidControllerSimulation
from fluidics.control.syringe_pump import SyringePumpSimulation
from fluidics.errors import DeviceError
from fluidics.robot.labware import well_key
from fluidics.robot.stage import LIMIT_SWITCH_ALARM, TOLERANCE_MM, _label
from fluidics.run_log import configure_console
from fluidics.system import FluidicsSystem
from fluidics.vocabulary import sipper_vocabulary

DRIVE_TOLERANCE_MM = 0.076     # the driver's own end-of-move check: 1000 counts at 13107.2 counts/mm

CHECKLIST = f"""\
Checklist -- watch for these as the script runs:
 1. Homing: Z goes UP first, then X and Y. Each axis then reads 0.5 mm: the
    drives park 0.1 to 0.5 mm past the switch and the stage moves each axis to
    its stroke's start, which is why the travel height is 1 mm or more.
 2. Settle: after each move, every axis's settle error (drive reading less
    target). The stage fails a move that ends more than {TOLERANCE_MM} mm off;
    the driver's own wait (unused here) allows {DRIVE_TOLERANCE_MM} mm. A move that fails with
      "axis <axis>: move to <T> mm ended at <P> mm (a limit switch?)"
    with P within {DRIVE_TOLERANCE_MM} mm of T is that gap, not a switch: note the axis
    and both figures.
 3. A limit stop is never provoked here. If one happens, the drive raises
    alarm 0x{LIMIT_SWITCH_ALARM:04X} and the move fails with
      "axis <axis>: move to <T> mm stopped by a limit switch at <P> mm"
    The stage has already halted every axis of that move and re-read where
    they stopped; it stays homed. A switch inside the stroke means the axis
    table's stroke or the switch is off: note both figures. At a jog prompt,
    jog back away from it (Z up first if Z is low).
 4. Corners: at each, the tip centred over the well and at the V bottom.
    The 12 columns span 99 mm of Y2's 0.5 to 109 mm stroke.
 5. At the end every drive is disabled: Z3 has no brake, so watch that its
    lead screw holds the stage with the sipper and tubing mounted.
"""


def fmt(position):
    if position is None:
        return "unknown"
    return "x {:.3f}, y {:.3f}, z {:.3f}".format(*position)


class Settle:
    """Each axis's settle error after a move, and the worst seen. The stage
    keeps the commanded target of every axis's last leg as its position;
    the drive is read for where the axis actually is (privates: a bench
    script's view of the stage)."""

    def __init__(self, robot):
        self._stage = robot._stage
        self.worst = {}

    def report(self, what):
        stage = self._stage
        commanded = stage.position()
        if commanded is None:
            print(f"  {what}: position unknown, no settle reading")
            return
        parts = []
        for axis, target in zip((stage._x, stage._y, stage._z), commanded):
            label = _label(axis)
            error = stage._service.read(axis).position_mm - target
            self.worst[label] = max(self.worst.get(label, 0.0), abs(error))
            parts.append(f"{label} {error:+.3f}" + (" (over)" if abs(error) > TOLERANCE_MM else ""))
        print(f"  {what}: at {fmt(commanded)}; settle error mm: " + ", ".join(parts))

    def summary(self):
        if not self.worst:
            return "no settle readings"
        return ("worst settle error mm: "
                + ", ".join(f"{label} {mm:.3f}" for label, mm in self.worst.items())
                + f" (the stage accepts {TOLERANCE_MM}, the driver's own wait {DRIVE_TOLERANCE_MM})")


def jog_until_enter(manual, robot, settle, prompt, on_enter):
    """Jogs typed at the console until an empty line: `x 1.5` or `z -0.2`
    (mm from where the axis is; X and Y only with Z at travel height),
    `retract`, or `home`. A refused or failed jog is printed and the
    prompt returns. `on_enter` says what the empty line sets moving."""
    print(prompt)
    print("  Type `x 1.5`, `y -0.2`, `z 0.1` (mm, relative), `retract` or `home`.")
    while True:
        line = input(f"  [{fmt(robot.position())}] jog, or Enter to {on_enter}> ").strip().lower()
        if not line:
            return
        try:
            if line == "retract":
                manual.retract()
            elif line == "home":
                manual.home_robot()
            else:
                parts = line.split()
                if len(parts) != 2 or parts[0] not in ("x", "y", "z"):
                    raise ValueError("give an axis and a distance, e.g. `z 0.5`")
                manual.jog(parts[0], float(parts[1]))
            settle.report(line)
        except (DeviceError, RuntimeError, ValueError) as e:
            hint = " -- see item 3 of the checklist" if "limit switch" in str(e) else ""
            print(f"  not done: {e}{hint}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--config", required=True, help="rig config YAML with a nimotion_gantry_system block")
    parser.add_argument("--agitate-well", help="a full well to agitate in (default: the config's priming_well)")
    parser.add_argument("--simulation", action="store_true",
                        help="simulated hardware, to check the script itself")
    args = parser.parse_args()

    configure_console()
    config = load_config(args.config)
    g = gantry(config)
    if g is None:
        sys.exit(f"{args.config} has no nimotion_gantry_system block: there is no robot to check")
    vocabulary = sipper_vocabulary(config)
    agitate_well = args.agitate_well or g.priming_well
    try:
        # Here, before anything moves: a typo found after the teaching
        # would end the script and lose the taught values unsaved.
        ref = vocabulary.resolve(agitate_well)
    except ValueError as e:
        sys.exit(f"--agitate-well {agitate_well}: {e}")
    agitate_well = well_key(ref)
    if args.simulation:
        # A self-check of the script: the valves' one-second commands and the
        # pump's moves need not be waited out. The stage keeps its pacing.
        FluidControllerSimulation.COMMAND_SECONDS = 0
        SyringePumpSimulation.ESTIMATE_SECONDS = 0

    print(CHECKLIST)
    input("Deck clear of anything but the plates? Enter to bring the rig up and home (Z goes UP first)...")
    system = FluidicsSystem.build(config, args.simulation)
    try:
        manual = system.manual
        robot = system.devices.robot
        settle = Settle(robot)
        print("homed; position", fmt(robot.position()), "-- Z should read 0.1 to 0.5 mm at the top")
        input("Enter to jog X +10, Y +10 and Z +5 mm, each and back...")
        for axis, mm in (("x", 10.0), ("y", 10.0), ("z", 5.0)):
            manual.jog(axis, mm)
            settle.report(f"jog {axis} {mm:+g}")
            manual.jog(axis, -mm)
            settle.report(f"jog {axis} {-mm:+g}")
        for name in sorted(vocabulary.reservoirs):
            corners = vocabulary.placement(name).geometry.corners()
            while True:
                jog_until_enter(
                    manual, robot, settle,
                    f"Jog the tip to {name} A1 at the V bottom. An empty line teaches the "
                    f"position there as the A1 and dip depth of {name}, then tours its "
                    f"corners: {', '.join(corners)}, each a retract, a move and a re-dip.",
                    f"teach here and tour the {name} corners")
                position = robot.position()
                if position is None:
                    print("  the position is unknown: type `home`")
                    continue
                x, y, z = position
                try:
                    manual.teach_a1(name, (x, y))
                    manual.teach_dip(name, z)
                except (DeviceError, ValueError) as e:
                    print(f"  not taught: {e}")
                    continue
                break
            for i, well in enumerate(corners):
                manual.go_to_well(f"{name}/{well}")
                settle.report(f"{name}/{well}")
                following = (f"retract and go to {name}/{corners[i + 1]}" if i + 1 < len(corners)
                             else "go on (nothing moves)")
                input(f"At {name}/{well}: is the tip centred and at the V bottom? Enter to {following}...")
        input(f"Enter to go to {agitate_well} (it must be full) and agitate twice...")
        manual.go_to_well(agitate_well)
        robot.agitate(2)
        settle.report(f"agitate in {agitate_well}")
        print(settle.summary())
        for name, r in sorted(vocabulary.reservoirs.items()):
            x, y = r.a1_xy_mm
            print(f"  {name}: a1_xy_mm: [{x:.2f}, {y:.2f}]  dip_z_mm: {r.dip_z_mm:.2f}")
        if args.simulation:
            # The docstring's own example runs the committed sample.
            print("Simulation: the taught values are not saved.")
        elif input(f"Write the taught values into {config.source_path}? [y/N] ").strip().lower() == "y":
            print("written to", save_config(config))
        input(f"Enter to close: no move (the tip stays in {agitate_well}, as it does between "
              "sessions), then every drive is disabled. Then check that Z holds there "
              "unpowered, and that X and Y hold under the tubing...")
    except BaseException as e:
        # A failure or Ctrl+C: the stage halted any leg it was in. Close
        # without a move -- system.close()'s quiescent close would also
        # clear the abort latch and, on a Flow Cell rig, park the syringe
        # to waste, a pump move.
        print("Stopped: closing without moving.")
        system.devices.close(quiescent=False)
        if isinstance(e, KeyboardInterrupt):
            sys.exit(130)
        raise
    errors = system.close()
    print("Every drive is disabled now: confirm Z holds on its lead screw (Z3 has no brake).")
    if errors:
        sys.exit(2)


if __name__ == "__main__":
    main()
