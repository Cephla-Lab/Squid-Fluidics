"""Live dump of MCU status packets: raw flow int16s next to the scaled values.

Read-only: connects to the Teensy, initializes the flow sensor, and prints -
it never touches the syringe pump or valves. Run it, then start a draw from
your usual GUI is NOT possible (the serial port is exclusive), so instead run
this alone and drive the pump by hand / from a second terminal via the Tecan's
own USB serial - or just watch the idle stream and hand-draw liquid through
the sensor to see the numbers move.

Usage (from software/):  python3 tools/dump_packets.py [--hz 5] [--seconds 60]
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml

from fluidics.control._def import CMD_SET, MCU_CONSTANTS
from fluidics.control.controller import FluidController


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hz", type=float, default=5.0, help="print rate (packets arrive at ~16 Hz)")
    parser.add_argument("--seconds", type=float, default=120.0, help="how long to dump")
    parser.add_argument("--config", default="_config.yaml")
    args = parser.parse_args()

    config = yaml.safe_load(open(Path(__file__).resolve().parents[1] / args.config))
    sensors = config.get("flow_sensors") or [{"index": 1, "name": "sensor1"}]

    fc = FluidController(config["microcontroller"]["serial_number"])
    fc.begin()
    fc.send_command(CMD_SET.CLEAR)
    time.sleep(0.2)

    for sensor in sensors:
        status = fc.send_command_blocking(
            CMD_SET.INITIALIZE_FLOW_SENSOR, sensor["index"], MCU_CONSTANTS.MEDIUM_WATER, True
        )
        print(f"init sensor index {sensor['index']} ({sensor.get('name', '?')}): MCU status {status}")

    state = {"last_print": 0.0, "n": 0}

    def on_packet(parsed):
        state["n"] += 1
        now = time.time()
        if now - state["last_print"] < 1.0 / args.hz:
            return
        state["last_print"] = now
        raw = parsed["flowrates_raw"]
        scaled = parsed["flowrates"]
        print(
            f"[{time.strftime('%H:%M:%S')}] pkt#{state['n']:6d} "
            f"flow_raw={raw[0]:6d},{raw[1]:6d}  flow={scaled[0]:8.1f},{scaled[1]:8.1f} uL/min  "
            f"pressures={['%.2f' % p for p in parsed['pressures']]}  "
            f"cmd_status={parsed['MCU_command_execution_status']} state={parsed['MCU_interal_program']}"
        )

    fc.subscribe_packets(on_packet)
    print(f"dumping for {args.seconds:.0f}s at {args.hz:g} lines/s (raw 32767 = sensor not reporting) ...")
    try:
        time.sleep(args.seconds)
    except KeyboardInterrupt:
        pass
    print(f"done: {state['n']} packets seen.")


if __name__ == "__main__":
    main()
