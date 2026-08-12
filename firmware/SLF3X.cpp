/*
    SLF3X.cpp
    This file contains a class and functions for initializing and reading from the flow sensor.

    class SLF3X:
      bool begin: Initialize sensor on given I2C bus, returns false if sensor not present
        Arguments: uint16_t n_tries, TwoWire &W, uint8_t medium
      uint8_t read: Read a value from the sensor on a given I2C bus into the shared readings variable and optionally perform the CRC. Returns flags indicating specific failures in the reading process.
        Arguments: bool do_crc, TwoWire &W, int16_t *readings

    Other functions:
      double SLF3X_to_celsius: Convert from raw to degrees Celsius
        Arguments: int16_t raw_temp
      double SLF3X_to_uLmin: Convert from raw to microliters per minute
        Arguments: int16_t raw_flow

      static uint8_t crc: Perform a CRC
        Arguments: uint8_t *data
*/

#include "SLF3X.h"

/*
  -----------------------------------------------------------------------------
  DESCRIPTION: calculate_crc() takes a two-uint8_t array as an argument and returns the CRC-8

  OPERATION:   We pass the data array by reference and perform a (non-destructive) CRC calculating operation using the CRC parameters defined in the sensor's datasheet, then we return the array.

  ARGUMENTS:
      uint8_t *data: pointer to data array

  RETURNS:
      uint8_t calc_crc: the calculated CRC

  INPUTS / OUTPUTS: None

  LOCAL VARIABLES: None

  SHARED VARIABLES:
      uint8_t *data: The array is read and is not written to

  GLOBAL VARIABLES: None

  DEPENDENCIES: None
  -----------------------------------------------------------------------------
*/
static uint8_t calculate_crc(uint8_t *dat) {
  uint8_t calc_crc = 0xFF;
  for (uint8_t b = 0; b < 2; ++b) {
    calc_crc ^= (dat[b]);
    for (uint8_t i = 8; i > 0; --i) {
      if (calc_crc & 0x80) {
        calc_crc = (calc_crc << 1) ^ SLF3X_CRC_POLYNOMIAL;
      } else {
        calc_crc = (calc_crc << 1);
      }
    }
  }

  return calc_crc;
}

// Initialize the SLF3X class
SLF3X::SLF3X() {
  init = false;
  return;
}

/*
  -----------------------------------------------------------------------------
  DESCRIPTION: begin() initializes the sensor on a given I2C bus. It returns true if it was initialized successfully and false otherwise.

  OPERATION:   We first try sending reset signals until the chip is properly reset, then we send requests to put it in continuous mode. If the number of tries exceeds SLF3X_N_TRIES, give up and return false. Otherwise, the sensor was initialized properly and return true. We also save whether we want to do CRCs to a shared variable.

  ARGUMENTS:
      TwoWire &W:        streaming class to read from (e.g. Wire, Wire1, etc. Must be I2C)
      uint8_t medium:    Select whether to use water ior IPA calibration data
      bool do_crc:       Set true to do CRCs for each sensor reading

  RETURNS:
      bool sensor_connected: true if the sensor returns valid data, false otherwise

  INPUTS / OUTPUTS: The I2C lines are used as inputs and outputs to transmit data

  LOCAL VARIABLES: 
      uint16 n, n_tries: Track the number of times we have tried to connect
      int8_t ret: I2C error code

  SHARED VARIABLES:
      TwoWire *w_:         I2C class
      bool do_crc_:        Written to

  GLOBAL VARIABLES: None

  DEPENDENCIES: Wire.h
  -----------------------------------------------------------------------------
*/
bool SLF3X::begin(TwoWire &W, uint8_t medium, bool do_crc) {
  // Cleared here and set only on the success path below. It used to be set
  // true as the first statement, before any I2C traffic, and neither failure
  // return cleared it -- so a sensor that failed to initialize still reported
  // itself present, and guards keyed on init (e.g. the CLEAR_LINES check)
  // passed on a rig whose flow sensor was absent or unresponsive.
  init = false;
  uint16_t n = 0;
  int8_t ret = 0;
  uint16_t n_tries = SLF3X_N_TRIES;
  
  do_crc_ = do_crc;
  w_ = &W;

  w_->begin();

  n_tries++; // Account for guaranteed 2 tries (1 resetting, 1 reading)

  do {
    n++; // Count number of times we have tried resetting
    // Send reset signal
    w_->beginTransmission(SLF3X_GEN_RST_ADDRESS);
    w_->write(SLF3X_GEN_RST_CMD);
    ret = w_->endTransmission();
    delay(50);
  } while (n < n_tries && ret != 0);

  // If we have not successfully reset, return out of this function
  if (n >= n_tries) {
    return false;
  }

  // Set the sensor to cts mode
  n_tries++;
  do {
    n++; // Additionally count number of tries setting the sensor
    w_->beginTransmission(SLF3X_ADDRESS);
    w_->write(SLF3X_START_CTS_MEAS);
    w_->write(medium);
    ret = w_->endTransmission();
    delay(50);
  } while (n < n_tries && ret != 0);

  // If we have not successfully set, return out of this function
  if (n >= n_tries) {
    return false;
  }

  // If we get here, that means setup was a success
  delay(100); // at least 60 ms needed for reliable measurements to begin
  init = true;
  return true;
}

/*
  -----------------------------------------------------------------------------
  DESCRIPTION: read() reads the raw flow rate, temperature, and flags from the sensor over I2C and optionally calculates the CRC. If there is a CRC mismatch, we return INT16_MAX in the array instead of the actual value

  OPERATION:   We request 9 uint8_ts from the sensor, flow_high, flow_low, flow_crc, temp_high, temp_low, temp_crc, flags_high, flags_low, flags_crc. We read the data into a shared array and optionally perform the CRC; if the CRC fails or if there is some other error, we return false. If the read is successfull, return true.

  ARGUMENTS:
      int16_t *readings: Pointer to array for storing data. The data at this array will be overwritten.

  RETURNS:
      uint8_t error: 0 if the reading succeeded, bits set if there was an error
        bit 0 set - couldn't get any readings
        bit 1 set - flow CRC failed
        bit 2 set - temperature CRC failed
        bit 3 set - flags CRC failed

  INPUTS / OUTPUTS: The I2C lines are used as inputs and outputs to transmit data

  LOCAL VARIABLES: 
      uint8_t    crc[3]: For performing CRC
      uint8_t    rx[6]: Received data
      uint8_t    err: I2C read error
      uint8_t    crc_calc: The calculated CRC

  SHARED VARIABLES:
     TwoWire &W:          I2C class
     bool do_crc_:        Read from
     int16_t readings[3]: Used to store the data. We are writing to this array.
        readings[0]: raw flow value
        readings[1]: raw temp value
        readings[3]: flags from the SLF3X

  GLOBAL VARIABLES: None

  DEPENDENCIES: Wire.h
  -----------------------------------------------------------------------------
*/
uint8_t SLF3X::read(int16_t *readings) {
  uint8_t    crc[3];  // store CRC
  uint8_t    rx[6];   // store raw data
  uint8_t    err = 0; // assume no error
  uint8_t    crc_calc;

  

  // put default values in readings[]
  for (uint8_t i = 0; i < 3; i++) {
    readings[i] = INT16_MAX;
  }
  if(!init){
    return 0xFF;
  }
  
  w_->requestFrom(SLF3X_ADDRESS, 9);
  // Return with error if we fail to read all the uint8_ts
  if (w_->available() < 9) {
    err |= (1 << 0);
    return err;
  }

  // Read all the data
  for (uint8_t i = 0; i < 3; i++) {
    rx[(2 * i)] = w_->read(); // read the MSB from the sensor
    rx[(2 * i) + 1] = w_->read(); // read the LSB from the sensor
    crc[i]    = w_->read();
    readings[i]  = rx[(2 * i)] << 8;
    readings[i] |= rx[(2 * i) + 1];
  }
  // If we aren't doing the CRC, we're done here
  if (!do_crc_) {
    return err;
  }
  // Perform the CRC
  for (uint8_t i = 0; i < 3; i++) {
    crc_calc = calculate_crc(rx + (2 * i * sizeof(rx[0])));
    // Set the error flags if there was a problem reading
    if (crc_calc != crc[i]) {
      err |= (1 << (i + 1));
    }
  }

  return err;
}

/*
  -----------------------------------------------------------------------------
  DESCRIPTION: SLF3X_to_celsius() converts from a raw sensor reading to degrees Celsius

  OPERATION:   We divide the raw value by the scale value and return it

  ARGUMENTS:
      int16_t  raw_temp: raw temperature value from the sensor

  RETURNS:
      double temp:         temperature in degrees Celsius

  INPUTS / OUTPUTS: None

  LOCAL VARIABLES: None

  SHARED VARIABLES: None

  GLOBAL VARIABLES: None

  DEPENDENCIES: None
  -----------------------------------------------------------------------------
*/
double SLF3X_to_celsius(int16_t raw_temp) {
  return ((double)raw_temp) / SLF3X_SCALE_FACTOR_TEMP;
}

/*
  -----------------------------------------------------------------------------
  DESCRIPTION: SLF3X_to_uLmin() converts from a raw sensor reading to microliters per minute

  OPERATION:   We divide the raw value by the scale value and return it

  ARGUMENTS:
      int16_t  raw_flow: raw flow value from the sensor

  RETURNS:
      double flow:        flow rate in units microliters per minute

  INPUTS / OUTPUTS: None

  LOCAL VARIABLES: None

  SHARED VARIABLES: None

  GLOBAL VARIABLES: None

  DEPENDENCIES: None
  -----------------------------------------------------------------------------
*/
double SLF3X_to_uLmin(int16_t raw_flow) {
  return ((double)raw_flow) / SLF3X_SCALE_FACTOR_FLOW;
}
