// Host-side checks of bus_rules.h: pio test -e native
#include <unity.h>
#include "../../bus_rules.h"

void setUp(void) {}
void tearDown(void) {}

void test_valve_address_valid_accepts_the_idex_range(void) {
  TEST_ASSERT_TRUE(valve_address_valid(0x0E));
  TEST_ASSERT_TRUE(valve_address_valid(0x10));
  TEST_ASSERT_TRUE(valve_address_valid(0x1A));
  TEST_ASSERT_TRUE(valve_address_valid(0xFE));
}

void test_valve_address_valid_rejects_odd_low_and_zero(void) {
  TEST_ASSERT_FALSE(valve_address_valid(0x00));
  TEST_ASSERT_FALSE(valve_address_valid(0x0C));
  TEST_ASSERT_FALSE(valve_address_valid(0x0F));
  TEST_ASSERT_FALSE(valve_address_valid(0xFF));
}

void test_0x10_is_the_flow_sensor_on_the_wire(void) {
  TEST_ASSERT_TRUE(valve_address_conflicts(0x10, 0x08));
  TEST_ASSERT_TRUE(valve_address_conflicts(0x11, 0x08));
  TEST_ASSERT_FALSE(valve_address_conflicts(0x0E, 0x08));
  TEST_ASSERT_FALSE(valve_address_conflicts(0x12, 0x08));
}

void test_flow_slot_for_bus(void) {
  TEST_ASSERT_EQUAL_INT8(0, flow_slot_for_bus(1));
  TEST_ASSERT_EQUAL_INT8(1, flow_slot_for_bus(2));
  TEST_ASSERT_EQUAL_INT8(2, flow_slot_for_bus(0));
  TEST_ASSERT_EQUAL_INT8(-1, flow_slot_for_bus(3));
}

void test_packet_bytes_are_claimed_once_and_never_in_fixed_fields(void) {
  uint8_t claimed[30] = {0};
  for (uint8_t i = 0; i < sizeof(VALVE_POS_OFFSET); i++) {
    claimed[VALVE_POS_OFFSET[i]]++;
  }
  for (uint8_t i = 0; i < FLOW_SLOTS; i++) {
    claimed[FLOW_SLOT_OFFSET[i]]++;
    claimed[FLOW_SLOT_OFFSET[i] + 1]++;
  }
  for (uint8_t b = 0; b < 30; b++) {
    TEST_ASSERT_TRUE_MESSAGE(claimed[b] <= 1, "a packet byte is claimed twice");
    // 0-5 header, 11-12 solenoids, 13-14 pump power, 27-29 time and volume
    if (b <= 5 || (b >= 11 && b <= 14) || b >= 27) {
      TEST_ASSERT_EQUAL_UINT8(0, claimed[b]);
    }
  }
}

int main(int argc, char **argv) {
  UNITY_BEGIN();
  RUN_TEST(test_valve_address_valid_accepts_the_idex_range);
  RUN_TEST(test_valve_address_valid_rejects_odd_low_and_zero);
  RUN_TEST(test_0x10_is_the_flow_sensor_on_the_wire);
  RUN_TEST(test_flow_slot_for_bus);
  RUN_TEST(test_packet_bytes_are_claimed_once_and_never_in_fixed_fields);
  return UNITY_END();
}
