import numpy as np

MCU_CMD_LENGTH = 15
MCU_MSG_LENGTH = 30

# MCU - COMPUTER
T_DIFF_COMPUTER_MCU_MISMATCH_FAULT_THRESHOLD_SECONDS = 3

class MCU_CONSTANTS:
  # pressure sensor SSCMRRV015PD2A3
  _output_min = 0 #1638; # 10% of 2^14
  _output_max = (1<<14) - 1 #14745; # 90% of 2^14
  _p_min = -15 # psi
  _p_max = 15 # psi
  # SLF3X params
  VOLUME_UL_MAX = 5000
  # Mirrors SLF3X_SCALE_FACTOR_FLOW in the firmware. Used only to convert
  # uL/min thresholds into the raw counts the MCU compares against
  # (INITIALIZE_BANG_BANG_PARAMS), so it must match the firmware, NOT
  # necessarily the installed sensor. The sensor's own scale factor lives in
  # flow_sensor.py, which is what turns a reading into uL/min.
  MCU_ASSUMED_SCALE_FACTOR_FLOW = 10
  # Same category: mirrors firmware SLF3X.h, so it describes what the MCU
  # assumes rather than the part that is fitted.
  SLF3X_MAX_VAL_uL_MIN = 3520
  SLF3X_WATER = 0x08
  SLF3X_IPA = 0x15
  MEDIUM_WATER = 0x08
  MEDIUM_IPA = 0x15
  MEDIA = [MEDIUM_IPA, MEDIUM_WATER]
  # PID params
  KP_MAX =  8
  KI_MAX =  1
  KD_MAX =  8
  ILIM_MAX = np.iinfo(np.uint16).max
  # Disc pump params
  TTP_MAX_PW = 1000
  # Control loop params
  FLUID_OUT_BANG_BANG = 0
  FLUID_IN_BANG_BANG  = 1
  FLUID_OUT_PID       = 2
  PRESSURE_PID        = 3
  VACUUM_PID          = 4
  OPEN_LOOP_CTRL      = 5
  LOOP_TYPES          = [FLUID_OUT_BANG_BANG, FLUID_IN_BANG_BANG, FLUID_OUT_PID, PRESSURE_PID, VACUUM_PID, OPEN_LOOP_CTRL]
  BB_LOOP_TYPES       = [FLUID_OUT_BANG_BANG, FLUID_IN_BANG_BANG]
  PID_LOOP_TYPES      = [FLUID_OUT_PID, PRESSURE_PID, VACUUM_PID]

class CMD_SET:
  CLEAR                        = 0
  INITIALIZE_DISC_PUMP         = 1
  INITIALIZE_PRESSURE_SENSOR   = 2
  INITIALIZE_FLOW_SENSOR       = 3
  INITIALIZE_BUBBLE_SENSORS    = 4
  INITIALIZE_VALVES            = 5
  INITIALIZE_ROTARY            = 6
  INITIALIZE_BANG_BANG_PARAMS  = 7
  INITIALIZE_PID_PARAMS        = 8
  SET_SOLENOID_VALVES          = 9
  SET_SOLENOID_VALVE           = 10
  SET_ROTARY_VALVE             = 11
  SET_PUMP_PWR_OPEN_LOOP       = 12
  BEGIN_CLOSED_LOOP            = 13 
  STOP_CLOSED_LOOP             = 14
  CLEAR_LINES                  = 15
  LOAD_FLUID_TO_SENSOR         = 16
  LOAD_FLUID_VOLUME            = 17
  UNLOAD_FLUID_VOLUME          = 18
  VENT_VB0                     = 19
  VOL_INTEGRATE_SETTING        = 20
  REMOVE_ALL_MEDIUM            = 21
  DELAY_MS                     = 22
  EJECT_MEDIUM                 = 23

class COMMAND_STATUS:
  COMPLETED_WITHOUT_ERRORS  = 0
  IN_PROGRESS               = 1
  CMD_INVALID               = 2
  CMD_EXECUTION_ERROR       = 3

class VALVE_POSITIONS:
  FLUID_TO_CHAMBER   = 0b0000000000000000
  FLUID_CLEAR_LINES  = 0b0000000000010111  
  FLUID_TO_RESERVOIR = 0b0000000000010111
  FLUID_STOP_FLOW    = 0b0000000000010101
  VALVES_VENT_VB0    = 0b0000000000110101
  FLUID_TO_VB1       = 0b0000000000011000
  # Python-only named masks -- SET_SOLENOID_VALVES accepts any uint16, and the
  # firmware's ValvesStates_t does not name these two. Pinned as such in
  # test_firmware_mirror.py; a new Python-only name must be added there too.
  TEST_PRESSURE      = 0b0000000000001010
  TEST_VACUUM        = 0b0000000000010001