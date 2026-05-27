"""
Hardware test: peristaltic pumps and Z-axis servo motor.

Usage:
    python tests/hardware/test_pumps_and_servo.py --pumps       # Test pumps only
    python tests/hardware/test_pumps_and_servo.py --servo       # Test servo only
    python tests/hardware/test_pumps_and_servo.py --all         # Test both

Run from software/ directory.
"""

import argparse
import logging
import time

import serial.tools.list_ports

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from fluidics.control.modbus_rtu import ModbusRTUClient
from fluidics.control.peristaltic_pump import Direction, PeristalticPump
from fluidics.control.servo_motor import ServoMotor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# --- Device serial numbers ---
SERVO_SERIAL = "A10PGADK"
PUMP_SERIAL = "BG01NAV6"

# --- Pump config ---
PUMP_BAUDRATE = 115200
PUMP_DISPENSE_SLAVE_ID = 1
PUMP_ASPIRATE_SLAVE_ID = 2
PUMP_SPEED_RPM = 100.0
PUMP_DURATION_S = 3.0

# --- Servo config ---
SERVO_BAUDRATE = 115200
LOWER_POSITION_MM = 30.0
RAISE_POSITION_MM = 0.0
SERVO_VELOCITY_MM_S = 50.0


def find_port_by_serial(serial_number: str) -> str:
    for p in serial.tools.list_ports.comports():
        if p.serial_number == serial_number:
            return p.device
    raise RuntimeError(f"Device with serial number {serial_number} not found")


def test_pumps():
    log.info("=== Peristaltic Pump Test ===")

    port = find_port_by_serial(PUMP_SERIAL)
    log.info("Pump bus found at %s", port)

    client = ModbusRTUClient(port, PUMP_BAUDRATE)
    client.connect()
    log.info("Modbus client connected")

    pump_dispense = PeristalticPump(client, PUMP_DISPENSE_SLAVE_ID, Direction.CLOCKWISE)
    pump_aspirate = PeristalticPump(client, PUMP_ASPIRATE_SLAVE_ID, Direction.COUNTER_CLOCKWISE)

    try:
        # Step 1: Test dispense pump — start/stop
        log.info("--- Step 1: Dispense pump start/stop ---")
        pump_dispense.set_speed(PUMP_SPEED_RPM)
        pump_dispense.start()
        log.info("Dispense pump running at %.1f RPM", PUMP_SPEED_RPM)
        time.sleep(2.0)
        pump_dispense.stop()
        log.info("Dispense pump stopped")
        time.sleep(0.5)

        # Step 2: Test aspirate pump — start/stop
        log.info("--- Step 2: Aspirate pump start/stop ---")
        pump_aspirate.set_speed(PUMP_SPEED_RPM)
        pump_aspirate.start()
        log.info("Aspirate pump running at %.1f RPM", PUMP_SPEED_RPM)
        time.sleep(2.0)
        pump_aspirate.stop()
        log.info("Aspirate pump stopped")
        time.sleep(0.5)

        # Step 3: Test run_for_duration
        log.info("--- Step 3: Dispense pump run_for_duration (%.1fs) ---", PUMP_DURATION_S)
        pump_dispense.run_for_duration(PUMP_SPEED_RPM, PUMP_DURATION_S)
        log.info("run_for_duration complete")

        log.info("=== Pump test PASSED ===")

    except Exception:
        log.exception("Pump test failed")
        # Try to stop both pumps
        for pump in (pump_dispense, pump_aspirate):
            try:
                pump.stop()
            except Exception:
                pass
        raise
    finally:
        client.disconnect()
        log.info("Modbus client disconnected")


def test_servo():
    log.info("=== Z-Axis Servo Test ===")

    port = find_port_by_serial(SERVO_SERIAL)
    log.info("Servo found at %s", port)

    servo = ServoMotor(port, SERVO_BAUDRATE)
    servo.connect()
    log.info("Servo connected")

    try:
        # Step 1: Initialize and enable
        log.info("--- Step 1: Initialize and enable ---")
        servo.initialize_axis("Z")
        servo.enable("Z")
        log.info("Servo enabled")

        # Step 2: Home
        log.info("--- Step 2: Homing ---")
        servo.home("Z", wait=True)
        log.info("Homing complete, position: %.2f mm", servo.get_position("Z"))

        # Step 3: Move down
        log.info("--- Step 3: Move to %.1f mm ---", LOWER_POSITION_MM)
        servo.move_to(LOWER_POSITION_MM, axis="Z", velocity_mm_s=SERVO_VELOCITY_MM_S, wait=True)
        log.info("Position: %.2f mm", servo.get_position("Z"))

        # Step 4: Move back up
        log.info("--- Step 4: Move to %.1f mm ---", RAISE_POSITION_MM)
        servo.move_to(RAISE_POSITION_MM, axis="Z", velocity_mm_s=SERVO_VELOCITY_MM_S, wait=True)
        log.info("Position: %.2f mm", servo.get_position("Z"))

        # Step 5: Disable
        servo.disable("Z")
        log.info("Servo disabled")

        log.info("=== Servo test PASSED ===")

    except Exception:
        log.exception("Servo test failed")
        try:
            servo.disable("Z")
        except Exception:
            pass
        raise
    finally:
        servo.disconnect()
        log.info("Servo disconnected")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Hardware test for pumps and servo")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--pumps", action="store_true", help="Test pumps only")
    group.add_argument("--servo", action="store_true", help="Test servo only")
    group.add_argument("--all", action="store_true", help="Test both")
    args = parser.parse_args()

    if args.pumps or args.all:
        test_pumps()

    if args.servo or args.all:
        test_servo()
