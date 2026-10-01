
#define FROM_MCU_MSG_LENGTH 30
#define TX_INTERVAL_MS 60
#define SENSOR_INTERVAL_MS 20

#define DEBOUNCE_TIME_MS 150
#define FLOWSENSOR_DB_TIME_MS 100

#define VOLUME_UL_MAX 5000

#define KP_MAX   8
#define KI_MAX   1
#define KD_MAX   8
#define ILIM_MAX UINT16_MAX

#define VALVES_RST  2
#define VALVES_CS  10
#define VALVES_PWM  3
#define VALVES_SPI     SPI

#define FLUIDSENSORFRONT_A 29
#define FLUIDSENSORFRONT_B 28
#define FLUIDSENSORFRONT_C 30

#define FLUIDSENSORBACK_A  27
#define FLUIDSENSORBACK_B  26
#define FLUIDSENSORBACK_C  31

#define SELECTORVALVE_WIRE Wire
// Six slots on every build -- the v4s board has six valve ports, and an
// unused slot costs one object: a slot talks to its valve only once
// INITIALIZE_ROTARY got an ACK. Addresses are the 8-bit write form (see
// bus_rules.h); INITIALIZE_ROTARY may give a slot another one.
#define SELECTORVALVE_MAX  6
#define SELECTORVALVE_QTY  SELECTORVALVE_MAX
const uint8_t SELECTORVALVE_ADDRS[SELECTORVALVE_MAX] = {0x0E, 0x10, 0x12, 0x14, 0x16, 0x18};

#define SLF3X_WIRE0   Wire
#define SLF3X_WIRE1   Wire1
#define SLF3X_WIRE2   Wire2
#define PERFORM_CRC  true

// Flow-sensor slots. The host names a sensor by its I2C bus (config `index`)
// and bus_rules.h fixes the rest: FLOW_SLOT_BUS maps slot -> bus (1, 2, 0)
// and FLOW_SLOT_OFFSET maps slot -> status bytes (23, 25, 15).
//
// Slot 0 (bus 1, J15) is the process sensor: it alone feeds
// global_flowrate_reading, volume integration and the CLEAR_LINES guard.
// Slots 1 (bus 2, J17) and 2 (bus 0, J20) are telemetry. Bus 0 is the
// selector valves' bus: a sensor there is refused while a valve sits at the
// sensor's address on the wire (0x10 >> 1 == SLF3X_ADDRESS), and the other
// way round. IDEX valves do not implement general call, so the SLF3X reset
// at 0x00 leaves them alone.
#define SLF3X_MAX          3
// Slot -> Wire object, in FLOW_SLOT_BUS order.
TwoWire* const SLF3X_BUS_BY_SLOT[SLF3X_MAX] = {&SLF3X_WIRE1, &SLF3X_WIRE2, &SLF3X_WIRE0};

#define SSCX_SPI     SPI
#define SSCX_QTY      0 // 0 are installed here
#define SSCX_MAX      4 // Support up to 4 pressure sensors
const uint8_t PRESSURE_CS[] = {37, 36, 35, 34}; // Only index 0 and 1 are populated on most boards

#define TTP_UART             Serial3
#define TTP_SELECTED_MODE    TTP_MODE_MANUAL
#define TTP_SELECTED_STREAM  TTP_STREAM_DISABLE
#define TTP_SELECTED_SRC     TTP_SRC_SETVAL
#define TTP_PWR_LIM_mW       TTP_MAX_PWR
#define DISCPUMP_PWR_mW_GO    100


enum InternalState_t {
  INTERNAL_STATE_IDLE = 0,
  INTERNAL_STATE_INITIALIZING_MEDIUM = 1,
  INTERNAL_STATE_LOADING_MEDIUM      = 2,
  INTERNAL_STATE_VENT_VB0            = 3,
  INTERNAL_STATE_UNLOADING           = 4,
  INTERNAL_STATE_CLEARING            = 5,
  INTERNAL_STATE_MOVING_ROTARY       = 6,
  INTERNAL_STATE_CALIB_FLUID         = 7,
  INTERNAL_STATE_REMOVING            = 8,
  INTERNAL_STATE_DELAYING            = 9,
  INTERNAL_STATE_EJECTING            = 10
};

enum CommandExecution_t {
  COMPLETED_WITHOUT_ERRORS  = 0,
  IN_PROGRESS               = 1,
  CMD_INVALID               = 2,
  CMD_EXECUTION_ERROR       = 3
};

enum ClosedLoopType_t {
  FLUID_OUT_BANG_BANG = 0,
  FLUID_IN_BANG_BANG  = 1,
  FLUID_OUT_PID       = 2,
  PRESSURE_PID        = 3,
  VACUUM_PID          = 4,
  OPEN_LOOP_CTRL      = 5
};

enum ValvesStates_t {
  FLUID_TO_CHAMBER   = 0b0000000000000000,
  FLUID_CLEAR_LINES  = 0b0000000000010111,  
  FLUID_TO_RESERVOIR = 0b0000000000010111,
  FLUID_STOP_FLOW    = 0b0000000000010101,
  VALVES_VENT_VB0    = 0b0000000000110101,
  FLUID_TO_VB1       = 0b0000000000011000,
};

enum SerialCommands_t {
  CLEAR                        = 0,
  INITIALIZE_DISC_PUMP         = 1,
  INITIALIZE_PRESSURE_SENSOR   = 2,
  INITIALIZE_FLOW_SENSOR       = 3,
  INITIALIZE_BUBBLE_SENSORS    = 4, 
  INITIALIZE_VALVES            = 5,
  INITIALIZE_ROTARY            = 6,
  INITIALIZE_BANG_BANG_PARAMS  = 7,
  INITIALIZE_PID_PARAMS        = 8,
  SET_SOLENOID_VALVES          = 9,
  SET_SOLENOID_VALVE           = 10,
  SET_ROTARY_VALVE             = 11, 
  SET_PUMP_PWR_OPEN_LOOP       = 12,
  BEGIN_CLOSED_LOOP            = 13,
  STOP_CLOSED_LOOP             = 14,
  CLEAR_LINES                  = 15, 
  LOAD_FLUID_TO_SENSOR         = 16,
  LOAD_FLUID_VOLUME            = 17,
  UNLOAD_FLUID_VOLUME          = 18,
  VENT_VB0                     = 19,
  VOL_INTEGRATE_SETTING        = 20,
  REMOVE_ALL_MEDIUM            = 21,
  DELAY_MS                     = 22,
  EJECT_MEDIUM                 = 23,
  SET_ROTARY_ADDRESS           = 24
};
