/*
  This file implements Serial control over the fluidics controller and maintains the control loop.

*/

#include "AutoPID.h"
#include "NXP33996.h"
#include "OPX350.h"
#include "RheoLink.h"
#include "SLF3X.h"
#include "TTP.h"
#include "SSCX.h"
#include "_defs.h"
#include <PacketSerial.h>

// Class constructors and supporting global variables/definitions for the different modules we want
void disableControlLoops();

// Closed loop control: Use bang-bang to get fluid into the reservior (positive) and bang-bang / PID to get fluid out (negative)
// Need different params for input vs output due to fluidics topology
double global_flowrate_reading = 0;
double global_power_out = 0;
double global_pid_setpoint = 0;
double global_pressure_reading = 0;
double global_vacuum_reading = 0;
AutoBangBang fluid_in_bb(&global_flowrate_reading, &global_power_out, -1);
AutoBangBang fluid_out_bb(&global_flowrate_reading, &global_power_out, +1);
AutoPID fluid_out_pid(&global_flowrate_reading, &global_pid_setpoint, &global_power_out, +1);
AutoPID pressure_press_pid(&global_pressure_reading, &global_pid_setpoint, &global_power_out, 1);
AutoPID pressure_vacuum_pid(&global_vacuum_reading, &global_pid_setpoint, &global_power_out, -1);

// NXP33996 - valve controller
NXP33996 valves;

// OPX350 - bubble sensors
OPX350 fluidsensor_front, fluidsensor_back;
bool fs_front_debounce = false;
bool fs_back_debounce = false;
bool fs_back_debounce_neg = false;
uint32_t fs_front_time, fs_back_time, fs_back_neg_time;

// Selector valves
RheoLink selectorvalves[SELECTORVALVE_QTY];

// SLF3X flowrate sensor
// Slot 0 is the process sensor (bus 1 / Wire1): it alone drives
// global_flowrate_reading, volume integration and the CLEAR_LINES guard.
// Slot 1 (bus 2 / Wire2) is telemetry -- transmitted, but not fed back.
// Both start with init false, so an unpopulated slot reports INT16_MAX.
SLF3X flowsensors[SLF3X_MAX];
bool flowsensor_debounce = false;
uint32_t flow_time;
bool flowsensor_debounce_neg = false;
uint32_t flow_time_neg;

// SSCX pressure sensors
SSCX pressuresensors[SSCX_QTY];

// TTP Disc Pump
TTP discpump;

// PacketSerial
PacketSerial pSerial;

// Timekeeping
uint32_t tx_interval_ms = TX_INTERVAL_MS;
uint32_t sensor_interval_ms = SENSOR_INTERVAL_MS;
elapsedMillis time_since_last_tx = 0;
elapsedMillis time_since_cmd_started = 0;
elapsedMillis time_since_last_sensor = 0;
elapsedMillis time_cmd_operation     = 0;

// Track state - modified by an interrupt
volatile InternalState_t    state;
volatile CommandExecution_t execution_status;
volatile uint16_t           cmd_uid;
volatile SerialCommands_t   cmd_rxed;
volatile double             integrated_volume_uL;
volatile uint32_t           timeout_duration;
volatile uint32_t           debounce_time;
volatile uint32_t           cmd_data;
volatile bool               integrate_flowrate;
// Track peak pressure - for determining when the line is empty
float peak_pressure = 0;
// Track pressure scale form serial command
volatile uint8_t pressure_scale = UINT8_MAX;

void setup() {
  // Initialize serial communications with the host computer
  pSerial.begin(2000000);
  pSerial.setPacketHandler(&onPacketReceived);

  state = INTERNAL_STATE_IDLE;
  execution_status = COMPLETED_WITHOUT_ERRORS;
}

void loop() {
  // Update control loops, pSerial
  pSerial.update();
  fluid_in_bb.run();
  fluid_out_bb.run();
  fluid_out_pid.run();
  pressure_press_pid.run();
  pressure_vacuum_pid.run();

  // If sufficient time elapsed, send status packet back
  if (time_since_last_tx >= tx_interval_ms) {
    time_since_last_tx -= tx_interval_ms;
    // It takes 2 ms max to read all the sensors - time taken up mostly by send operation
    sendStatusPacket();
  }

  // If sufficient time elapsed, read the sensors and manage the state
  if (time_since_last_sensor >= sensor_interval_ms) {
    uint32_t dt = time_since_last_sensor;
    time_since_last_sensor -= sensor_interval_ms;

    // Read all the sensors
    // FLUID SENSORS
    uint8_t fs1 = fluidsensor_front.read();
    uint8_t fs2 = fluidsensor_back.read();
    // SELECTOR VALVE STATUS
    uint8_t selectorvalve_status[SELECTORVALVE_QTY];
    for (uint8_t i = 0; i < SELECTORVALVE_QTY; i++) {
      selectorvalve_status[i] = byte(selectorvalves[i].read_register(RheoLink_STATUS));
    }
    // PRESSURE
    // Sized SSCX_MAX, not SSCX_QTY: indices 0 and 1 are read unconditionally
    // below and further down in the state machine, so with SSCX_QTY 0 this was
    // a zero-length array and those reads went out of bounds into whatever the
    // stack held. Zero-initialized so an unpopulated sensor reads as 0 rather
    // than garbage.
    uint16_t press_readings[2];
    uint16_t pressure_results[SSCX_MAX] = {0};
    for (uint8_t i = 0; i < SSCX_QTY; i++) {
      pressuresensors[i].read(press_readings);
      pressure_results[i] = press_readings[SSCX_PRESS_IDX];
    }
    // FLOWRATE
    int16_t flow_readings[3];
    flowsensors[0].read(flow_readings);   // slot 0 only: this feeds the control loop
    bool flowsensor_fluid_present = !(flow_readings[SLF3X_FLAG_IDX] & SLF3X_AIR_IN_LINE);
    double flowrate = flowsensor_fluid_present ? SLF3X_to_uLmin(flow_readings[SLF3X_FLOW_IDX]) : 0;

    // DEBOUNCING
    // If fluid sensor 1 detects fluid, latch high for debounce_time
    if (fs1 == OPX350_LOW) {
      fs_front_time = millis();
      fs_front_debounce = true;
    }
    else if ((millis() - fs_front_time) > debounce_time) {
      fs_front_debounce = false;
    }
    // If fluid sensor 2 detects fluid, latch high for debounce_time
    if (fs2 == OPX350_LOW) {
      fs_back_time = millis();
      fs_back_debounce = true;
    }
    else if ((millis() - fs_back_time) > debounce_time) {
      fs_back_debounce = false;
    }
    // If fluid sensor 2 detects air, latch low for debounce_time
    if (fs2 != OPX350_LOW) {
      fs_back_neg_time = millis();
      fs_back_debounce_neg = false;
    }
    else if ((millis() - fs_back_neg_time) > debounce_time) {
      fs_back_debounce_neg = true;
    }
    // If the flow sensor detects fluid, latch high for debounce_time
    if (flowsensor_fluid_present) {
      flow_time = millis();
      flowsensor_debounce = true;
    }
    else if ((millis() - flow_time) > debounce_time) {
      flowsensor_debounce = false;
    }
    // If the flow sensor detects air, latch low for debounce_time
    if (!flowsensor_fluid_present) {
      flow_time_neg = millis();
      flowsensor_debounce_neg = false;
    }
    else if ((millis() - flow_time_neg) > debounce_time) {
      flowsensor_debounce_neg = true;
    }

    // INTEGRATION:
    if (integrate_flowrate) {
      integrated_volume_uL += dt * flowrate / (60.0 * 1000.0);
    }

    // SET GLOBAL VARIABLES
    global_flowrate_reading = flowrate;
    global_pressure_reading = SSCX_to_psi(pressure_results[1]);
    global_vacuum_reading = SSCX_to_psi(pressure_results[0]);

    // State machine for performing multi-step operations
    switch (state) {
      case INTERNAL_STATE_DELAYING: {
          if (time_since_cmd_started > timeout_duration) {
            state = INTERNAL_STATE_IDLE;
            execution_status = COMPLETED_WITHOUT_ERRORS;
          }
        }
        break;
      case INTERNAL_STATE_IDLE: {
          // debug: get rid of error checking
          // Check if we hit the back fluid sensor
          //          if (fs2 == OPX350_LOW) {
          //            // Turn off all control loops and disable the pump
          //            disableControlLoops();
          //            // Set the valves to minimize additional fluid flow
          //            valves.transfer(FLUID_TO_CHAMBER);
          //            // Indicate an error has occured
          //            execution_status = CMD_EXECUTION_ERROR;
          //          }
        }
        break;
      /*
      case INTERNAL_STATE_MOVING_ROTARY: {
          // Check if we timed out
          // If any of the valves are reporting a value greater than their pos_max, value_moving becomes true
          bool valve_moving = false;
          for (uint8_t i = 0; i < SELECTORVALVE_QTY; i++) {
            valve_moving |= (selectorvalve_status[i] > selectorvalves[i].pos_max);
          }
          // If we aren't moving, we are done here
          if (!valve_moving) {
            state = INTERNAL_STATE_IDLE;
            execution_status = COMPLETED_WITHOUT_ERRORS;
          }
          // If we aren't, check if we timed out
          else if (time_since_cmd_started > timeout_duration) {
            state = INTERNAL_STATE_IDLE;
            execution_status = CMD_EXECUTION_ERROR;
          }
        }
        break;
      */
      case INTERNAL_STATE_CALIB_FLUID: {
          // Wait for timeout
          if (time_since_cmd_started > timeout_duration) {
            state = INTERNAL_STATE_IDLE;
            execution_status = COMPLETED_WITHOUT_ERRORS;
          }
        }
        break;
      case INTERNAL_STATE_CLEARING: {
          bool fluids_present = fs_front_debounce || fs_back_debounce || flowsensor_debounce;
          // Check if we have timed out.
          if (time_since_cmd_started > timeout_duration) {
            state = INTERNAL_STATE_IDLE;
            execution_status = CMD_EXECUTION_ERROR;
          }
          // Otherwise, if there are fluids, reset the count
          else if (fluids_present) {
            time_cmd_operation = 0;
          }
          // Otherwise, if cmd_data ms have elapsed since the start of the operation, we are done
          else if (time_cmd_operation > cmd_data) {
            state = INTERNAL_STATE_IDLE;
            execution_status = COMPLETED_WITHOUT_ERRORS;
            // Turn off all control loops and disable the pump
            disableControlLoops();
          }
        }
        break;
      case INTERNAL_STATE_INITIALIZING_MEDIUM: {
          // Want both fluid in the flow sensor and fluid sensor. This will cause overshoot!
          bool fluids_present = flowsensor_debounce_neg && (fs1 == OPX350_LOW);
          // Check if we have timed out.
          if (time_since_cmd_started > timeout_duration) {
            state = INTERNAL_STATE_IDLE;
            execution_status = CMD_EXECUTION_ERROR;
          }
          // Error check - don't let fluid get past the second fluid sensor
          // debug: get rid of error checking
          //          else if (fs_back_debounce) {
          //            state = INTERNAL_STATE_IDLE;
          //            execution_status = CMD_EXECUTION_ERROR;
          //            // Prevent fluid flow
          //            discpump.set_target(0);
          //            discpump.enable(false);
          //            valves.transfer(FLUID_STOP_FLOW);
          //          }
          // Otherwise, if there are fluids, we are done!
          else if (fluids_present) {
            state = INTERNAL_STATE_IDLE;
            execution_status = COMPLETED_WITHOUT_ERRORS;
            // Prevent fluid flow
            discpump.set_target(0);
            discpump.enable(false);
            valves.transfer(FLUID_STOP_FLOW);
          }
        }
        break;
      case INTERNAL_STATE_UNLOADING:
      case INTERNAL_STATE_LOADING_MEDIUM: {
          // Check for error conditions:
          //    timeout
          bool timed_out = (time_since_cmd_started > timeout_duration);
          //    flow sensor reading too high or too low
          bool flow_sensor_saturated = (constrain(flow_readings[SLF3X_FLOW_IDX], SSCX_OUT_MIN, SSCX_OUT_MAX) != flow_readings[SLF3X_FLOW_IDX]);
          //    fluid hit the second fluid sensor (only important when loading medium, don't care if unloading)
          bool overfilled_reservoir = (state == INTERNAL_STATE_LOADING_MEDIUM) ? flowsensor_debounce_neg : false;
          //    integrated flowrate more than max
          bool over_max = (abs(integrated_volume_uL) > VOLUME_UL_MAX);

          // Check if we withdrew the correct volume
          // Due to flow sensor orientation, it is negative volume when loading and positive when unloading
          double intermediate =  UINT16_MAX * integrated_volume_uL / VOLUME_UL_MAX;
          intermediate = (state == INTERNAL_STATE_LOADING_MEDIUM) ? -intermediate : intermediate;
          bool volume_threshold_reached = (intermediate >= cmd_data);
          // debug: get rid of error checking
          if (timed_out || over_max) { // || overfilled_reservoir || flow_sensor_saturated
            state = INTERNAL_STATE_IDLE;
            execution_status = CMD_EXECUTION_ERROR;
            // Prevent fluid flow
            disableControlLoops();
            discpump.set_target(0);
            discpump.enable(false);
            valves.transfer(FLUID_STOP_FLOW);
          }
          else if (volume_threshold_reached) {
            state = INTERNAL_STATE_IDLE;
            execution_status = COMPLETED_WITHOUT_ERRORS;
            // Prevent fluid flow
            disableControlLoops();
            discpump.set_target(0);
            discpump.enable(false);
            valves.transfer(FLUID_STOP_FLOW);
          }
          else {
            // Update the setpoint if using a control loop, otherwise keep the target the same
            if (!fluid_in_bb.isStopped() || !fluid_out_bb.isStopped() || !fluid_out_pid.isStopped() || !pressure_press_pid.isStopped() || !pressure_vacuum_pid.isStopped()) {
              discpump.set_target(global_power_out);
            }
          }
        }
        break;

      case INTERNAL_STATE_VENT_VB0: {
          // Check timeout
          if (time_since_cmd_started > timeout_duration) {
            state = INTERNAL_STATE_IDLE;
            execution_status = CMD_EXECUTION_ERROR;
            // Prevent fluid flow
            valves.transfer(FLUID_STOP_FLOW);
          }

          // Check if we hit the threshold
          else if (pressure_results[0] >= cmd_data) {
            state = INTERNAL_STATE_IDLE;
            execution_status = COMPLETED_WITHOUT_ERRORS;
            // Prevent fluid flow
            valves.transfer(FLUID_STOP_FLOW);
          }
        }
        break;
      case INTERNAL_STATE_EJECTING:
      case INTERNAL_STATE_REMOVING: {
          // Keep pumping until we stop seeing spikes in pressure
          float target_pressure = 0;
          if (state == INTERNAL_STATE_REMOVING) {
            // negate vacuum for positive readings
            target_pressure = -SSCX_to_psi(pressure_results[0]);
          }
          else if (state == INTERNAL_STATE_EJECTING) {
            // read pressure sensor
            target_pressure = SSCX_to_psi(pressure_results[1]);
          }
          // track peak after time delay
          if (time_since_cmd_started >= min(3.0, timeout_duration / 15.0)) {
            peak_pressure = max(peak_pressure, target_pressure);
          }
          // wait until the pressure falls below (pressure_scale/UINT8_MAX) of the peak
          float thresh_pressure = pressure_scale * peak_pressure;
          thresh_pressure /= UINT8_MAX;
          // Check timeout
          if (time_since_cmd_started > timeout_duration) {
            state = INTERNAL_STATE_IDLE;
            execution_status = CMD_EXECUTION_ERROR;
            // Prevent fluid flow
            discpump.set_target(0);
            discpump.enable(false);
            valves.transfer(FLUID_STOP_FLOW);
          }
          // Check for spikes in pressure, reset count if we aren't below the threshold
          else if (target_pressure >= thresh_pressure) {
            time_cmd_operation = 0;
          }
          // Check if sufficient time has passed since falling below the threshold
          else if (time_cmd_operation > cmd_data) {
            state = INTERNAL_STATE_IDLE;
            execution_status = COMPLETED_WITHOUT_ERRORS;
            // Prevent fluid flow
            discpump.set_target(0);
            discpump.enable(false);
            valves.transfer(FLUID_STOP_FLOW);
          }
        }
        break;
      default:
        break;
    }
  }
}

void sendStatusPacket() {
  /*
        #########################################################
        #########   MCU -> Computer message structure   #########
        #########################################################
        byte 0-1    : computer -> MCU CMD counter (UID)
        byte 2      : cmd from host computer (error checking through check sum => no need to transmit back the parameters associated with the command)
        byte 3      : status of the command (see _def.py)
        byte 4      : MCU internal program being executed (see _def.py)
        byte 5      : Bubble sensor state (high and low nibble)
        byte 6-10   : Selector valves status (1,2,3,4,5)
        byte 11-12  : state of valve D1-D16
        byte 13-14  : pump power
        byte 15-16  : pressure sensor 1 reading
        byte 17-18  : pressure sensor 2 reading
        byte 19-20  : pressure sensor 3 reading
        byte 21-22  : pressure sensor 4 reading
        byte 23-24  : flow sensor slot 0 reading (bus 1 / Wire1)
        byte 25-26  : flow sensor slot 1 reading (bus 2 / Wire2)
                      INT16_MAX (32767) means no sensor or a failed read, and
                      is distinct from 0, which means measured zero flow.
        byte 27     : elapsed time since the start of the last internal program (in seconds)
        byte 28-29  : total volume (ul), range: 0 - 5000
  */
  double intermediate;
  byte buffer_tx[FROM_MCU_MSG_LENGTH];
  buffer_tx[0] = byte(cmd_uid >> 8);
  buffer_tx[1] = byte(cmd_uid & 0xFF);

  buffer_tx[2] = cmd_rxed;
  buffer_tx[3] = execution_status;
  buffer_tx[4] = state;

  uint8_t fs1 = fluidsensor_front.read();
  uint8_t fs2 = fluidsensor_back.read();
  buffer_tx[5] = byte((fs1 << 4) | fs2);

  // Read from selector valves
  for (uint8_t i = 0; i < SELECTORVALVE_QTY; i++) {
    buffer_tx[6 + i] =  byte(selectorvalves[i].read_register(RheoLink_STATUS));
  }
  // Fill remaining entries of buffer with 0
  for (uint8_t i = SELECTORVALVE_QTY; i < SELECTORVALVE_MAX; i++) {
    buffer_tx[6 + i] = 0;
  }

  buffer_tx[11] =  byte(valves.local_state >> 8);
  buffer_tx[12] =  byte(valves.local_state & 0xFF);

  intermediate = (discpump.pwr / TTP_MAX_PWR) * UINT16_MAX;
  uint16_t normed_power = static_cast<uint16_t>(intermediate);
  buffer_tx[13] = byte(normed_power >> 8);
  buffer_tx[14] = byte(normed_power & 0xFF);

  // Get pressure readings
  uint16_t press_readings[2];
  for (uint8_t i = 0; i < SSCX_QTY; i++) {
    pressuresensors[i].read(press_readings);
    buffer_tx[15 + (2 * i)] = byte(press_readings[SSCX_PRESS_IDX] >> 8);
    buffer_tx[16 + (2 * i)] = byte(press_readings[SSCX_PRESS_IDX] & 0xFF);
  }
  for (uint8_t i = SSCX_QTY; i < SSCX_MAX; i++) {
    buffer_tx[15 + (2 * i)] = 0;
    buffer_tx[16 + (2 * i)] = 0;
  }

  // debug: include peak pressure and target pressure
  if (false) {
    uint16_t peak = psi_to_SSCX(peak_pressure);
    float temp = peak_pressure * pressure_scale / UINT8_MAX;
    uint16_t target = psi_to_SSCX(temp);
    buffer_tx[19] = peak >> 8;
    buffer_tx[20] = peak & 0xFF;
    buffer_tx[21] = target >> 8;
    buffer_tx[22] = target & 0xFF;
  }
  // debug: include flowrate PID error and integral error
  if (false){
    uint16_t err = psi_to_SSCX(SSCX_PSI_MAX * constrain(fluid_out_pid._previousError, -TTP_MAX_PWR, TTP_MAX_PWR)/(2*TTP_MAX_PWR)); 
    uint16_t ierr = psi_to_SSCX(SSCX_PSI_MAX * constrain(fluid_out_pid._integral, -UINT16_MAX, UINT16_MAX)/(2*UINT16_MAX));

    buffer_tx[19] = err >> 8;
    buffer_tx[20] = err & 0xFF;
    buffer_tx[21] = ierr >> 8;
    buffer_tx[22] = ierr & 0xFF;
  }

  // One pair of bytes per slot. An uninitialized or failed sensor needs no
  // special case: SLF3X::read() pre-fills INT16_MAX and returns early when
  // !init, so an empty slot transmits the sentinel the host already reads as
  // "no reading". It must not transmit 0 -- that means "measured zero flow".
  int16_t flow_readings[3];
  for (uint8_t i = 0; i < SLF3X_MAX; i++) {
    flowsensors[i].read(flow_readings);
    buffer_tx[23 + (2 * i)] = byte(flow_readings[SLF3X_FLOW_IDX] >> 8);
    buffer_tx[24 + (2 * i)] = byte(flow_readings[SLF3X_FLOW_IDX] & 0xFF);
  }

  buffer_tx[27] = byte(time_since_cmd_started / 1000); // byte(time_cmd_operation  / 1000);

  intermediate = (integrated_volume_uL / VOLUME_UL_MAX) * INT16_MAX;
  int16_t volume_ul_int16 = static_cast<int16_t>(intermediate);
  buffer_tx[28] = byte(volume_ul_int16 >> 8);
  buffer_tx[29] = byte(volume_ul_int16 & 0xFF);

  pSerial.send(buffer_tx, FROM_MCU_MSG_LENGTH);

  return;
}

void disableControlLoops() {
  fluid_in_bb.stop();
  fluid_out_bb.stop();
  fluid_out_pid.stop();
  pressure_press_pid.stop();
  pressure_vacuum_pid.stop();
  discpump.enable(true);
  discpump.set_target(0);

  return;
}


// We process the incoming command here
void onPacketReceived(const uint8_t* buffer, size_t size) {
  // If we don't have enough bytes, return out before having a buffer overflow
  if (size < 3) {
    return;
  }
  // Get the received command
  cmd_rxed = static_cast<SerialCommands_t>(buffer[2]);
  // Get the cmd uid
  cmd_uid = (buffer[0] << 8) + buffer[1];
  // we got a new command!
  time_since_cmd_started = 0;
  switch (cmd_rxed) {
    case CLEAR: {
        // Stop all operations
        disableControlLoops();
        valves.clear_all();
        // Accumulate rather than assign: plain assignment kept only the last
        // valve's result, so a failure on any earlier valve was reported as
        // success. Initialized because the loop body may not run at all.
        uint8_t err = 0;
        for (uint8_t i = 0; i < SELECTORVALVE_QTY; i++) {
          err |= selectorvalves[i].set_position(1, true, RheoLink_TIMEOUT);
        }

        time_since_last_tx = 0;

        time_since_last_sensor = 0;

        state = INTERNAL_STATE_IDLE;
        if (err != 0)
          execution_status = CMD_EXECUTION_ERROR;
        else
          execution_status = COMPLETED_WITHOUT_ERRORS;
        cmd_uid = 0;
        integrate_flowrate = false;
        integrated_volume_uL = 0;
        peak_pressure = 0;
        pressure_scale = UINT8_MAX;
      }
      break;

    case INITIALIZE_DISC_PUMP: {
        // Ensure the correct amount of data was sent
        // If we don't see exactly 8 bytes, don't do anything
        // 3 bytes for cmd and UID, 2 for initializing the pump
        if (size !=  5) {
          state = INTERNAL_STATE_IDLE;
          execution_status = CMD_INVALID;
          return;
        }
        // Ensure the control loops are stopped
        disableControlLoops();
        // Initialize variables
        int16_t pwr_lim = int16_t((buffer[3] << 8) + buffer[4]);
        // Begin initialization
        discpump.begin(TTP_UART);
        bool result = discpump.init(pwr_lim, TTP_SELECTED_SRC, TTP_SELECTED_MODE, TTP_SELECTED_STREAM);
        // Ensure disc pump is not enabled
        discpump.set_target(0);
        discpump.enable(false);

        if (result) {
          execution_status = COMPLETED_WITHOUT_ERRORS;
        }
        else {
          execution_status = CMD_EXECUTION_ERROR;
        }

        state = INTERNAL_STATE_IDLE;
      }
      break;

    case INITIALIZE_PRESSURE_SENSOR: {
        // Ensure the correct amount of data was sent
        // If we don't see exactly 4 bytes, don't do anything
        // 3 bytes for cmd and UID, 1 for initializing the pump
        if (size !=  4) {
          state = INTERNAL_STATE_IDLE;
          execution_status = CMD_INVALID;
          return;
        }
        uint8_t idx = buffer[3];
        SSCXStatus_t result;
        if (idx < SSCX_QTY) {
          result = pressuresensors[idx].begin(SPI, PRESSURE_CS[idx]);
        }
        else {
          state = INTERNAL_STATE_IDLE;
          execution_status = CMD_INVALID;
          return;
        }

        if (result == SSCX_STATUS_NORMAL) {
          execution_status = COMPLETED_WITHOUT_ERRORS;
        }
        else {
          execution_status = CMD_EXECUTION_ERROR;
        }

        state = INTERNAL_STATE_IDLE;
      }
      break;

    case INITIALIZE_FLOW_SENSOR: {
        // Ensure the correct amount of data was sent
        // If we don't see exactly 6 bytes, don't do anything
        // 3 bytes for cmd and UID, 3 for initializing the sensor
        if (size !=  6) {
          state = INTERNAL_STATE_IDLE;
          execution_status = CMD_INVALID;
          return;
        }
        uint8_t bus = buffer[3];
        uint8_t medium = buffer[4];
        bool do_crc = buffer[5];
        bool result;

        // The bus fixes the slot: bus 1 -> slot 0 -> packet bytes 23-24,
        // bus 2 -> slot 1 -> bytes 25-26. Bus 0 (Wire) is rejected; it carries
        // the selector valves, whose transactions would stall flow reads.
        if (bus < SLF3X_FIRST_BUS || bus >= SLF3X_FIRST_BUS + SLF3X_MAX) {
          state = INTERNAL_STATE_IDLE;
          execution_status = CMD_INVALID;
          return;
        }
        uint8_t slot = bus - SLF3X_FIRST_BUS;

        if (bus == 1) {
          result = flowsensors[slot].begin(SLF3X_WIRE1, medium, do_crc);
        }
        else {
          result = flowsensors[slot].begin(SLF3X_WIRE2, medium, do_crc);
        }

        if (result) {
          execution_status = COMPLETED_WITHOUT_ERRORS;
        }
        else {
          execution_status = CMD_EXECUTION_ERROR;
        }

        state = INTERNAL_STATE_IDLE;
      }

      break;

    case INITIALIZE_BUBBLE_SENSORS: {
        // We no additional data - all hard-coded
        if (size != 3) {
          state = INTERNAL_STATE_IDLE;
          execution_status = CMD_INVALID;
          return;
        }
        fluidsensor_front.begin(FLUIDSENSORFRONT_A, FLUIDSENSORFRONT_B, FLUIDSENSORFRONT_C);
        fluidsensor_back.begin(FLUIDSENSORBACK_A, FLUIDSENSORBACK_B, FLUIDSENSORBACK_C);

        fluidsensor_front.calibrate();
        fluidsensor_back.calibrate();


        state = INTERNAL_STATE_CALIB_FLUID;
        execution_status = IN_PROGRESS;
        timeout_duration = OPX35_CALIB_TIMEOUT_MS;
      }
      break;

    case INITIALIZE_VALVES: {
        // We no additional data - all hard-coded
        if (size != 3) {
          state = INTERNAL_STATE_IDLE;
          execution_status = CMD_INVALID;
          return;
        }
        valves.begin(VALVES_CS, VALVES_PWM, VALVES_RST, VALVES_SPI);
        execution_status = COMPLETED_WITHOUT_ERRORS;
        state = INTERNAL_STATE_IDLE;
      }
      break;

    case INITIALIZE_ROTARY: {
        // Ensure the correct amount of data was sent
        // If we don't see exactly 5 bytes, don't do anything
        // 3 bytes for cmd and UID, 2 for I2C address and max
        state = INTERNAL_STATE_IDLE;
        if (size != 5) {
          execution_status = CMD_INVALID;
          return;
        }
        // Ensure we are trying to initialize a pump that exists
        uint8_t idx = buffer[3];
        if (idx >= SELECTORVALVE_QTY) {
          execution_status = CMD_INVALID;
          return;
        }
        // Begin initialization
        uint8_t addr = SELECTORVALVE_ADDRS[idx];
        uint8_t max_pos = buffer[4];
        uint8_t result = selectorvalves[idx].begin(SELECTORVALVE_WIRE, addr, 1, max_pos);
        if (result == 0) {
          execution_status = COMPLETED_WITHOUT_ERRORS;
        }
        else {
          execution_status = CMD_EXECUTION_ERROR;
        }

      }
      break;

    case INITIALIZE_BANG_BANG_PARAMS: {
        // Ensure the correct amount of data was sent
        // If we don't see exactly 16 bytes, don't do anything
        // 3 bytes for cmd and UID, 13 for initializing the controller
        if (size != 16) {
          state = INTERNAL_STATE_IDLE;
          execution_status = CMD_INVALID;
          return;
        }
        double t_low, t_high, o_min, o_max;
        uint32_t tstep;

        ClosedLoopType_t type = static_cast<ClosedLoopType_t>(buffer[3]);
        double temp;
        temp = ((double)(buffer[4] << 8) + buffer[5]) / SLF3X_SCALE_FACTOR_FLOW;
        t_low  = temp;
        temp = ((double)(buffer[6] << 8) + buffer[7]) / SLF3X_SCALE_FACTOR_FLOW;
        t_high = temp;
        o_min = ((double)(buffer[8] << 8) + buffer[9]);
        o_max = ((double)(buffer[10] << 8) + buffer[11]);
        tstep = (buffer[12] << (8 * 3)) + (buffer[13] << (8 * 2)) + (buffer[14] << 8) + buffer[15];

        switch (type) {
          case FLUID_IN_BANG_BANG:
            fluid_in_bb.setThresholds(t_low, t_high);
            fluid_in_bb.setOutputRange(o_min, o_max);
            fluid_in_bb.setTimeStep(tstep);

            execution_status = COMPLETED_WITHOUT_ERRORS;
            break;
          case FLUID_OUT_BANG_BANG:
            fluid_out_bb.setThresholds(t_low, t_high);
            fluid_out_bb.setOutputRange(o_min, o_max);
            fluid_out_bb.setTimeStep(tstep);

            execution_status = COMPLETED_WITHOUT_ERRORS;
            break;
          default:
            execution_status = CMD_INVALID;
        }

        state = INTERNAL_STATE_IDLE;

      }
      break;

    case INITIALIZE_PID_PARAMS: {
        // Ensure the correct amount of data was sent
        // If we don't see exactly 20 bytes, don't do anything
        // 3 bytes for cmd and UID, 17 for initializing the controller
        if (size != 20) {
          state = INTERNAL_STATE_IDLE;
          execution_status = CMD_INVALID;
          return;
        }
        double kp, ki, kd, ilim, temp;
        double o_min, o_max;
        uint32_t tstep;

        ClosedLoopType_t type = static_cast<ClosedLoopType_t>(buffer[3]);
        temp = ((buffer[4] << 8) + buffer[5]) * KP_MAX;
        kp   = temp / UINT16_MAX;
        temp = ((buffer[6] << 8) + buffer[7]) * KI_MAX;
        ki   = temp / UINT16_MAX;
        temp = ((buffer[8] << 8) + buffer[9]) * KD_MAX;
        kd   = temp / UINT16_MAX;

        temp  = ((buffer[10] << 8) + buffer[11]) * ILIM_MAX;
        ilim  = temp / UINT16_MAX;
        o_min = ((buffer[12] << 8) + buffer[13]);
        o_max = ((buffer[14] << 8) + buffer[15]);

        tstep = (buffer[16] << (8 * 3)) + (buffer[17] << (8 * 2)) + (buffer[18] << 8) + buffer[19];

        switch (type) {
          case FLUID_OUT_PID:
            fluid_out_pid.setGains(kp, ki, kd, ilim);
            fluid_out_pid.setOutputRange(o_min, o_max);
            fluid_out_pid.setTimeStep(tstep);

            execution_status = COMPLETED_WITHOUT_ERRORS;
            break;
          case PRESSURE_PID:
            pressure_press_pid.setGains(kp, ki, kd, ilim);
            pressure_press_pid.setOutputRange(o_min, o_max);
            pressure_press_pid.setTimeStep(tstep);

            execution_status = COMPLETED_WITHOUT_ERRORS;
            break;
          case VACUUM_PID:
            pressure_vacuum_pid.setGains(kp, ki, kd, ilim);
            pressure_vacuum_pid.setOutputRange(o_min, o_max);
            pressure_vacuum_pid.setTimeStep(tstep);

            execution_status = COMPLETED_WITHOUT_ERRORS;
            break;
          default:
            execution_status = CMD_INVALID;
        }

        state = INTERNAL_STATE_IDLE;

      }
      break;

    case SET_SOLENOID_VALVES: {
        // Ensure the correct amount of data was sent
        // If we don't see exactly 5 bytes, don't do anything
        // 3 bytes for cmd and UID, 2 for setting all 16 positions
        if (size != 5) {
          state = INTERNAL_STATE_IDLE;
          execution_status = CMD_INVALID;
          return;
        }
        // Unpack the setting
        uint16_t setting = (buffer[3] << 8) + buffer[4];
        valves.transfer(setting);
        execution_status = COMPLETED_WITHOUT_ERRORS;
        state = INTERNAL_STATE_IDLE;
      }
      break;

    case SET_SOLENOID_VALVE: {
        // Ensure the correct amount of data was sent
        // If we don't see exactly 5 bytes, don't do anything
        // 3 bytes for cmd and UID, 2 for picking which index to set/clear
        if (size != 5) {
          state = INTERNAL_STATE_IDLE;
          execution_status = CMD_INVALID;
          return;
        }
        bool op = buffer[3];
        uint8_t idx = buffer[4];

        if (op) {
          valves.turn_on(idx);
        }
        else {
          valves.turn_off(idx);
        }

        execution_status = COMPLETED_WITHOUT_ERRORS;
        state = INTERNAL_STATE_IDLE;

      }
      break;

    case SET_ROTARY_VALVE: {
        // Ensure the correct amount of data was sent
        // If we don't see exactly 5 bytes, don't do anything
        // 3 bytes for cmd and UID, 2 for the index and position command
        if (size != 5) {
          state = INTERNAL_STATE_IDLE;
          execution_status = CMD_INVALID;
          return;
        }
        // Ensure we are setting a valid valve
        uint8_t idx = buffer[3];
        if (idx >= SELECTORVALVE_QTY) {
          state = INTERNAL_STATE_IDLE;
          execution_status = CMD_INVALID;
          return;
        }
        // Ensure we have a valid position
        uint8_t pos = buffer[4];
        if ((pos < selectorvalves[idx].pos_min) || (pos > selectorvalves[idx].pos_max)) {
          state = INTERNAL_STATE_IDLE;
          execution_status = CMD_INVALID;
          return;
        }
        // Set position
        uint8_t err = selectorvalves[idx].set_position(pos, true, RheoLink_TIMEOUT);
        if (err != 0) {
          execution_status = CMD_EXECUTION_ERROR;
        }
        else {
          execution_status = COMPLETED_WITHOUT_ERRORS;
        }
        state = INTERNAL_STATE_IDLE;
      }
      break;

    case SET_PUMP_PWR_OPEN_LOOP: {
        // Ensure the correct amount of data was sent
        // If we don't see exactly 5 bytes, don't do anything
        // 3 bytes for cmd and UID, 2 for power
        if (size != 5) {
          state = INTERNAL_STATE_IDLE;
          execution_status = CMD_INVALID;
          return;
        }
        // Disable all control loops
        disableControlLoops();
        // Initialize variables
        float pwr_setting = uint16_t((buffer[3] << 8) + buffer[4]);
        // Set power
        if (pwr_setting > 0) {
          discpump.enable(true);
        }
        else {
          discpump.enable(false);
        }
        bool result = discpump.set_target(pwr_setting);
        if (result) {
          execution_status = COMPLETED_WITHOUT_ERRORS;
        }
        else {
          execution_status = CMD_EXECUTION_ERROR;
        }
        state = INTERNAL_STATE_IDLE;

      }
      break;

    case BEGIN_CLOSED_LOOP: {
        // Ensure the correct amount of data was sent
        // If we don't see exactly 4 bytes, don't do anything
        // 3 bytes for cmd and UID, 1 for which loop to enable
        if (size != 4) {
          state = INTERNAL_STATE_IDLE;
          execution_status = CMD_INVALID;
          return;
        }
        ClosedLoopType_t type = static_cast<ClosedLoopType_t>(buffer[3]);

        switch (type) {
          case FLUID_OUT_BANG_BANG:
            fluid_out_bb.begin();
            execution_status = COMPLETED_WITHOUT_ERRORS;
            break;
          case FLUID_IN_BANG_BANG:
            fluid_in_bb.begin();
            execution_status = COMPLETED_WITHOUT_ERRORS;
            break;
          case FLUID_OUT_PID:
            fluid_out_pid.begin();
            execution_status = COMPLETED_WITHOUT_ERRORS;
            break;
          case PRESSURE_PID:
            pressure_press_pid.begin();
            execution_status = COMPLETED_WITHOUT_ERRORS;
            break;
          case VACUUM_PID:
            pressure_vacuum_pid.begin();
            execution_status = COMPLETED_WITHOUT_ERRORS;
            break;
          default:
            execution_status = CMD_INVALID;
        }
        state = INTERNAL_STATE_IDLE;

      }
      break;

    case STOP_CLOSED_LOOP: {
        // Ensure the correct amount of data was sent
        // If we don't see exactly 3 bytes, don't do anything
        // 3 bytes for cmd and UID
        if (size != 3) {
          state = INTERNAL_STATE_IDLE;
          execution_status = CMD_INVALID;
          return;
        }
        // Disable all control loops
        disableControlLoops();
        execution_status = COMPLETED_WITHOUT_ERRORS;
        state = INTERNAL_STATE_IDLE;

      }
      break;
    case REMOVE_ALL_MEDIUM:
    case EJECT_MEDIUM: {
        // Ensure the correct amount of data was sent
        // If we don't see exactly 10 bytes, don't do anything
        // 3 bytes for cmd and UID, 2 for power, 2 for timeout time, 2 for debounce interval, 1 for pressure scale
        if (size != 10) {
          state = INTERNAL_STATE_IDLE;
          execution_status = CMD_INVALID;
          return;
        }
        // Reset max pressure
        peak_pressure = 0;
        // Unpack data
        float pwr_setting = uint16_t((buffer[3] << 8) + buffer[4]);
        timeout_duration = uint16_t((buffer[5] << 8) + buffer[6]);
        debounce_time =  uint16_t((buffer[7] << 8) + buffer[8]);
        pressure_scale = buffer[9];


        if (cmd_rxed == REMOVE_ALL_MEDIUM) {
          // Set valves
          valves.transfer(FLUID_TO_VB1);
          state = INTERNAL_STATE_REMOVING;
        }
        else if (cmd_rxed == EJECT_MEDIUM) {
          // Set valves
          valves.transfer(FLUID_TO_CHAMBER);
          state = INTERNAL_STATE_EJECTING;
        }

        // Set open-loop disc pump power
        disableControlLoops();
        if (pwr_setting > 0) {
          discpump.enable(true);
        }
        else {
          discpump.enable(false);
        }
        bool result = discpump.set_target(pwr_setting);

        // If we failed to get the disc pump working, turn it off and return with error
        if (!result) {
          state = INTERNAL_STATE_IDLE;
          execution_status = CMD_EXECUTION_ERROR;
          discpump.enable(false);
          return;
        }

        execution_status = IN_PROGRESS;
        time_cmd_operation = 0;
      }
      break;
    case CLEAR_LINES: {
        // Ensure the correct amount of data was sent
        // If we don't see exactly 9 bytes, don't do anything
        // 3 bytes for cmd and UID, 2 for power, 2 for timeout time, 2 for debounce interval
        if (size != 9) {
          state = INTERNAL_STATE_IDLE;
          execution_status = CMD_INVALID;
          return;
        }
        // Check if bubble and flow sensors are initialized
        // Slot 0 only. Requiring slot 1 would break every single-sensor rig;
        // OR-ing across slots would let this run with a dead slot 0, whose
        // INT16_MAX has bit 0 set -> reads as AIR_IN_LINE -> the debounce latch
        // never sets -> the state machine calls the lines clear immediately.
        if (!fluidsensor_front.init || !fluidsensor_back.init || !flowsensors[0].init) {
          state = INTERNAL_STATE_IDLE;
          execution_status = CMD_EXECUTION_ERROR;
          return;
        }
        // Unpack data
        float pwr_setting = uint16_t((buffer[3] << 8) + buffer[4]);
        timeout_duration = uint16_t((buffer[5] << 8) + buffer[6]);
        cmd_data =  uint16_t((buffer[7] << 8) + buffer[8]);
        debounce_time = DEBOUNCE_TIME_MS;
        // Set valves
        valves.transfer(FLUID_CLEAR_LINES);
        // Set open-loop disc pump power
        disableControlLoops();
        if (pwr_setting > 0) {
          discpump.enable(true);
        }
        else {
          discpump.enable(false);
        }
        bool result = discpump.set_target(pwr_setting);

        // If we failed to get the disc pump working, turn it off and return with error
        if (!result) {
          state = INTERNAL_STATE_IDLE;
          execution_status = CMD_EXECUTION_ERROR;
          discpump.enable(false);
          return;
        }
        // Otherwise, go to state for monitoring fluid sensors
        execution_status = IN_PROGRESS;
        time_cmd_operation = 0;
        state = INTERNAL_STATE_CLEARING;
      }
      break;
    case LOAD_FLUID_TO_SENSOR: {
        // Ensure the correct amount of data was sent
        // If we don't see exactly 7 bytes, don't do anything
        // 3 bytes for cmd and UID, 2 for power, 2 for timeout time
        if (size != 7) {
          state = INTERNAL_STATE_IDLE;
          execution_status = CMD_INVALID;
          return;
        }
        // Unpack data
        float pwr_setting = uint16_t((buffer[3] << 8) + buffer[4]);
        timeout_duration = uint16_t((buffer[5] << 8) + buffer[6]);
        debounce_time = FLOWSENSOR_DB_TIME_MS;
        // Set valves
        valves.transfer(FLUID_TO_RESERVOIR);
        // Set open-loop disc pump power
        disableControlLoops();
        if (pwr_setting > 0) {
          discpump.enable(true);
        }
        else {
          discpump.enable(false);
        }
        bool result = discpump.set_target(pwr_setting);

        // If we failed to get the disc pump working, turn it off and return with error
        if (!result) {
          state = INTERNAL_STATE_IDLE;
          execution_status = CMD_EXECUTION_ERROR;
          discpump.enable(false);
          return;
        }

        // Enable flowrate integration in case of overshoot
        integrated_volume_uL = 0;
        integrate_flowrate = true;

        // Otherwise, go to state for monitoring pressure sensors
        execution_status = IN_PROGRESS;
        state = INTERNAL_STATE_INITIALIZING_MEDIUM;
        time_cmd_operation = 0;
      }
      break;
    case VOL_INTEGRATE_SETTING: {
        // Ensure the correct amount of data was sent
        // If we don't see exactly 5 bytes, don't do anything
        // 3 bytes for cmd and UID, 1 for resetting the integrated flowrate, 1 for setting whether to perform integration
        if (size != 5) {
          state = INTERNAL_STATE_IDLE;
          execution_status = CMD_INVALID;
          return;
        }
        bool reset_volume = buffer[4];

        integrate_flowrate = buffer[3];
        if (reset_volume) {
          integrated_volume_uL = 0;
        }

        execution_status = COMPLETED_WITHOUT_ERRORS;
        state = INTERNAL_STATE_IDLE;
      }
      break;
    case LOAD_FLUID_VOLUME: {
        // Ensure the correct amount of data was sent
        // If we don't see exactly 10 bytes, don't do anything
        // 3 bytes for cmd and UID, 1 for setting the control type, 2 for setpoint (if applicable), 2 for timeout, 2 for volume setpoint
        if (size != 10) {
          state = INTERNAL_STATE_IDLE;
          execution_status = CMD_INVALID;
          return;
        }

        ClosedLoopType_t type = static_cast<ClosedLoopType_t>(buffer[3]);
        timeout_duration = uint16_t((buffer[6] << 8) + buffer[7]);
        cmd_data = uint16_t((buffer[8] << 8) + buffer[9]);
        // Setpoint format depends on the closed loop type
        // The only valid options are vaccuum PID, fluid in BB, and open loop (fluid in PID is not implemeneted)
        switch (type) {
          case FLUID_IN_BANG_BANG: {
              // Ensure only this loop is enabled
              disableControlLoops();
              discpump.enable(true);
              fluid_in_bb.begin();
            }
            break;
          case VACUUM_PID: {
              // Convert to closed-loop pressure setpoint
              disableControlLoops();
              discpump.enable(true);
              pressure_vacuum_pid.begin();

              global_pid_setpoint = SSCX_to_psi(uint16_t((buffer[4] << 8) + buffer[5]));
            }
            break;
          case OPEN_LOOP_CTRL: {
              // Convert to open loop power setpoint
              float pwr_setting = uint16_t((buffer[4] << 8) + buffer[5]);
              disableControlLoops();
              if (pwr_setting > 0) {
                discpump.enable(true);
              }
              else {
                discpump.enable(false);
              }
              bool result = discpump.set_target(pwr_setting);
              if (!result) {
                state = INTERNAL_STATE_IDLE;
                execution_status = CMD_EXECUTION_ERROR;
                discpump.enable(false);
                return;
              }
            }
            break;
          default: {
              state = INTERNAL_STATE_IDLE;
              execution_status = CMD_INVALID;
              return;
            }
            break;
        }
        // We want to integrate the flowrate!
        integrate_flowrate = true;
        // set valves
        valves.transfer(FLUID_TO_RESERVOIR);
        debounce_time = DEBOUNCE_TIME_MS;

        execution_status = IN_PROGRESS;
        state = INTERNAL_STATE_LOADING_MEDIUM;
        time_cmd_operation = 0;
      }
      break;
    case UNLOAD_FLUID_VOLUME: {
        // Ensure the correct amount of data was sent
        // If we don't see exactly 10 bytes, don't do anything
        // 3 bytes for cmd and UID, 1 for setting the control type, 2 for setpoint (if applicable), 2 for timeout, 2 for volume setpoint

        if (size != 10) {
          state = INTERNAL_STATE_IDLE;
          execution_status = CMD_INVALID;
          return;
        }

        ClosedLoopType_t type = static_cast<ClosedLoopType_t>(buffer[3]);
        timeout_duration = uint16_t((buffer[6] << 8) + buffer[7]);
        cmd_data = uint16_t((buffer[8] << 8) + buffer[9]);
        // Setpoint format depends on the closed loop type
        // The only valid options are pressure PID, fluid out BB, fluid out PID, and open loop
        switch (type) {
          case FLUID_OUT_BANG_BANG: {
              disableControlLoops();
              discpump.enable(true);
              fluid_out_bb.begin();;
            }
            break;
          case FLUID_OUT_PID: {
              global_pid_setpoint = uint16_t((buffer[4] << 8) + buffer[5]) * SLF3X_MAX_VAL_uL_MIN;
              global_pid_setpoint /= UINT16_MAX;
              disableControlLoops();
              discpump.enable(true);
              fluid_out_pid.begin();
            }
            break;
          case PRESSURE_PID: {
              global_pid_setpoint = SSCX_to_psi(uint16_t((buffer[4] << 8) + buffer[5]));
              disableControlLoops();
              discpump.enable(true);
              pressure_press_pid.begin();
            }
            break;
          case OPEN_LOOP_CTRL: {
              // Convert to open loop power setpoint
              float pwr_setting = uint16_t((buffer[4] << 8) + buffer[5]);
              disableControlLoops();
              if (pwr_setting > 0) {
                discpump.enable(true);
              }
              else {
                discpump.enable(false);
              }
              bool result = discpump.set_target(pwr_setting);
              if (!result) {
                state = INTERNAL_STATE_IDLE;
                execution_status = CMD_EXECUTION_ERROR;
                discpump.enable(false);
                return;
              }
            }
            break;
          default: {
              state = INTERNAL_STATE_IDLE;
              execution_status = CMD_INVALID;
              return;
            }
        }
        // We want to integrate the flowrate!
        integrate_flowrate = true;
        integrated_volume_uL = 0;
        // set valves
        valves.transfer(FLUID_TO_CHAMBER);
        debounce_time = DEBOUNCE_TIME_MS;

        execution_status = IN_PROGRESS;
        state = INTERNAL_STATE_UNLOADING;
        time_cmd_operation = 0;
      }
      break;
    case VENT_VB0: {
        // Ensure the correct amount of data was sent
        // If we don't see exactly 7 bytes, don't do anything
        // 3 bytes for cmd and UID, 2 for vacuum threshold, 2 for timeout
        if (size != 7) {
          state = INTERNAL_STATE_IDLE;
          execution_status = CMD_INVALID;
          return;
        }

        disableControlLoops();
        valves.transfer(VALVES_VENT_VB0);

        cmd_data = uint16_t((buffer[3] << 8) + buffer[4]);
        timeout_duration = uint16_t((buffer[5] << 8) + buffer[6]);

        execution_status = IN_PROGRESS;
        state = INTERNAL_STATE_VENT_VB0;
        time_cmd_operation = 0;
      }
      break;
    case DELAY_MS: {
        // Ensure the correct amount of data was sent
        // If we don't see exactly 7 bytes, don't do anything
        // 3 bytes for cmd and UID, 4 for timeout time
        if (size != 7) {
          state = INTERNAL_STATE_IDLE;
          execution_status = CMD_INVALID;
          return;
        }
        timeout_duration = uint32_t((buffer[3] << 24) + (buffer[4] << 16) + (buffer[5] << 8) + buffer[6]);
        execution_status = IN_PROGRESS;
        state = INTERNAL_STATE_DELAYING;
        time_cmd_operation = 0;
      }
      break;
    default:
      state = INTERNAL_STATE_IDLE;
      execution_status = CMD_INVALID;
      break;
  }

  return;
}
