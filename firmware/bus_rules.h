/* bus_rules.h
   The I2C-bus and status-packet rules the firmware and the host must agree
   on, kept free of Arduino headers so `pio test -e native` can check them on
   the development machine. The host mirrors every constant here
   (software/fluidics/control/_def.py, valve_address.py, flow_sensor.py) and
   software/tests/unit/control/test_firmware_mirror.py compares the two.
*/
#ifndef BUS_RULES_H_
#define BUS_RULES_H_

#include <stdint.h>

// Selector-valve I2C addresses are the 8-bit write form IDEX documents and
// SELECTORVALVE_ADDRS stores; RheoLink::begin shifts right by one for Wire.
// IDEX RheoLink protocol 2321383F: factory address 0x0E, and the 'N'
// command accepts 0x0E-0xFE, even numbers only.
#define VALVE_ADDR_MIN 0x0E
#define VALVE_ADDR_MAX 0xFE

inline bool valve_address_valid(uint8_t addr) {
  return (addr % 2 == 0) && addr >= VALVE_ADDR_MIN && addr <= VALVE_ADDR_MAX;
}

// True when a valve at 8-bit address `valve_addr` puts the 7-bit address
// `addr7` on the wire. 0x10 >> 1 == 0x08, the SLF3X flow sensor's fixed
// address -- the reason a sensor on J20 and a valve at 0x10 cannot share
// the valves' bus.
inline bool valve_address_conflicts(uint8_t valve_addr, uint8_t addr7) {
  return (valve_addr >> 1) == addr7;
}

// Flow-sensor slot -> I2C bus. The host names a sensor by its bus (config
// `index`), the bus fixes the slot, and the slot fixes the packet bytes.
// Slot 0 (bus 1, Wire1, J15) alone feeds the control loop; slot 1 (bus 2,
// Wire2, J17) and slot 2 (bus 0, Wire, J20 -- the valves' bus) are telemetry.
const uint8_t FLOW_SLOT_BUS[] = {1, 2, 0};
// Flow-sensor slot -> first of its two status-packet bytes (big-endian int16).
// Slot 2 took bytes 15-16, which carried an SSCX pressure reading no build
// ever populated.
const uint8_t FLOW_SLOT_OFFSET[] = {23, 25, 15};
#define FLOW_SLOTS (sizeof(FLOW_SLOT_BUS) / sizeof(FLOW_SLOT_BUS[0]))

// Selector-valve slot -> its status-packet byte. Valve 6 took byte 17, the
// high byte of the same dead pressure field.
const uint8_t VALVE_POS_OFFSET[] = {6, 7, 8, 9, 10, 17};

// The flow slot serving `bus`, or -1 if no slot does.
inline int8_t flow_slot_for_bus(uint8_t bus) {
  for (uint8_t slot = 0; slot < FLOW_SLOTS; slot++) {
    if (FLOW_SLOT_BUS[slot] == bus) {
      return slot;
    }
  }
  return -1;
}

#endif /* BUS_RULES_H_ */
