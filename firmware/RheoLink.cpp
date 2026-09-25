/* RheoLink.h
   Define RheoLink Class
   https://www.idex-hs.com/docs/default-source/product-manuals/rheolink-i2c-communication-protocol-for-titanex.pdf.zip
   Control and IDEX selector valve using I2C

    Functions:
        RheoLink: Set up the class
            Arguments: uint8_t address
            Returns: None
        bool begin: Initialize the device. Return false if failed
            Arguments: TwoWire *w_
            Returns: If begun successfully, return true
        bool send_command: Send a command to the 
            Arguments: uint8_t pos - the new position
            Returns: Return true if successful
        uint8_t get_position: Get current position and error status
            Arguments: None
            Returns:
              99 – valve failure (valve can not be homed)
              88 – non-volatile memory error
              77 – valve configuration error or command mode error
              66 – valve positioning error
              55 – data integrity error
              44 – data CRC error
              3x – Problem connecting over I2C
                  31 – Data too long for buffer
                  32 – NACK on address tx
                  33 – NACK on data tx
                  34 – other error
                  35 – timeout
                  36 – other other error
              22 – Not initialized
              current valve position (1 to N) otherwise

       Created on: 8/10/2023
         Author: Kevin Marx
*/
#include "RheoLink.h"

/*
  -----------------------------------------------------------------------------
  DESCRIPTION: RheoLink() initializes the selector valve class

  OPERATION:   None

  ARGUMENTS: None

  RETURNS: NONE

  INPUTS / OUTPUTS: NONE

  LOCAL VARIABLES: NONE

  SHARED VARIABLES: None

  GLOBAL VARIABLES: None

  DEPENDENCIES: None
  -----------------------------------------------------------------------------
*/
RheoLink::RheoLink()
{
  init_ = false;
  return;
}


/*
  -----------------------------------------------------------------------------
  DESCRIPTION: begin() initializes the I2C bus

  OPERATION:   We set the I2C address. The Wire library handles setting the low bit for read/write so we shift the address down a bit. Then, we store the I2C bus object pointer to a local variable

  ARGUMENTS:
      TwoWire *w:      Pointer to TwoWire object for I2C
      uint8_t address:      I2C write address of the pump

  RETURNS: 
      uint8_t err: I2C transmission error code

  INPUTS / OUTPUTS: NONE

  LOCAL VARIABLES: NONE

  SHARED VARIABLES:
     TwoWire *w_: pointer to stream object, written to and read from
     uint8_t address_: I2C address, written to and read from
     uint8_t pos_min, pos_max: written to

  GLOBAL VARIABLES: None

  DEPENDENCIES: Wire.h
  -----------------------------------------------------------------------------
*/
uint8_t RheoLink::begin(TwoWire &w, uint8_t address, uint8_t p_min, uint8_t p_max) {

  address_ = address >> 1;
  
  w_ = &w;
  w_->begin();
  w_->beginTransmission(address_);

  init_ = true;

  pos_min = p_min;
  pos_max = p_max;

  return w_->endTransmission(address_);
}
/*
  -----------------------------------------------------------------------------
  DESCRIPTION: send_command() sends a command with retry logic for robust communication

  OPERATION:   We initialize the checksum with the "write" address, then send the command and data, updating the checksum. We send the checksum and check for transmission errors. If an error occurs, we retry up to RheoLink_MAX_RETRIES times with a delay between attempts.

  ARGUMENTS:
      uint8_t cmd: The command
      int8_t data: The data

  RETURNS:
      uint8_t err: The I2C error code (0 = success, non-zero = error after all retries exhausted).

  INPUTS / OUTPUTS: Data is sent over I2C.

  LOCAL VARIABLES:
      uint8_t checksum: Stores the checksum
      uint8_t err:      Stores the error value
      uint8_t retry_count: Tracks number of retry attempts

  SHARED VARIABLES:
     TwoWire *w_: pointer to stream object

  GLOBAL VARIABLES: None

  DEPENDENCIES: Wire.h
  -----------------------------------------------------------------------------
*/
uint8_t RheoLink::send_command(RheoLinkCommand_t cmd, uint8_t data ) {
  uint8_t checksum;
  uint8_t err;
  uint8_t retry_count = 0;

  if (!init_){
    return 22;
  }
  
  // Retry loop for robust communication
  do {
    checksum = address_ << 1;

    w_->beginTransmission(address_);
    w_->write(cmd);
    checksum = checksum ^ cmd;
    w_->write(data);
    checksum = checksum ^ data;
    w_->write(checksum);

    err = w_->endTransmission(true);

    // there's a problem with the firmware on the selector valve - open and close a dummy transmission to terminate the command
    w_->beginTransmission(0);
    w_->endTransmission();

    // If successful, break out of retry loop
    if (err == 0) {
      break;
    }

    // If we have retries left, wait and try again
    if (retry_count < RheoLink_MAX_RETRIES) {
      delay(RheoLink_RETRY_DELAY);
      retry_count++;
    }

  } while (retry_count <= RheoLink_MAX_RETRIES);
  
  return err;
}

/*
  -----------------------------------------------------------------------------
  DESCRIPTION: read_register() reads a value from the selector valve with retry logic.

  OPERATION:   We send the I2C command to request the register, then attempt to read the response. Both the command transmission and data request operations include retry logic for robust communication.

  ARGUMENTS:
      RheoLinkCommand_t target: The register/command to read

  RETURNS:
      uint8_t err: The register value or error code (after all retries exhausted).

  INPUTS / OUTPUTS: Data is sent and received over I2C.

  LOCAL VARIABLES:
      uint8_t err: Stores the error values
      uint8_t retry_count: Tracks number of retry attempts for requestFrom
      uint8_t bytes_received: Number of bytes received from requestFrom

  SHARED VARIABLES:
     TwoWire *w_: pointer to stream object

  GLOBAL VARIABLES: None

  DEPENDENCIES: Wire.h
  -----------------------------------------------------------------------------
*/
uint8_t RheoLink::read_register(RheoLinkCommand_t target){
  if (!init_){
    return 22;
  }
  
  uint8_t err = this->send_command(target);

  // If there was a problem connecting, return an error
  if(err != 0){
    return 30 + err;
  }

  // Retry logic for requestFrom operation
  uint8_t retry_count = 0;
  uint8_t bytes_received = 0;

  do {
    // Request the data
    bytes_received = w_->requestFrom(address_, 3, true);

    // If we got data, break out of retry loop
    if(bytes_received > 0){
      break;
    }

    // If we have retries left, wait and try again
    if (retry_count < RheoLink_MAX_RETRIES) {
      delay(RheoLink_RETRY_DELAY);
      retry_count++;
    }

  } while (retry_count <= RheoLink_MAX_RETRIES);

  // If we didn't get any data after all retries, return an error
  if(bytes_received == 0){
    return 36;
  }

  // Parse the data RXed - we only care about the first byte
  err = w_->read();
  // Clear the buffer
  while(w_->available())
    w_->read();

  return err;
}


