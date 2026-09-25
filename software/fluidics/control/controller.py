import logging

from cobs import cobs
from ._def import *
from .discovery import find_serial_port, open_serial_port
from datetime import datetime
import os
from pathlib import Path
import numpy as np
import threading

from ..subscribers import Subscribers  # noqa: F401  -- re-exported; it lived here first
from time import time, sleep

SERIAL_NUMBER_DEBUGGING = '11972480'

# How long the reader thread naps when the port has nothing waiting. The MCU
# transmits every 60 ms, so anything well under that adds no meaningful latency
# while keeping idle wakeups down.
READER_IDLE_SLEEP_S = 0.005

_logger = logging.getLogger(__name__)


def to_int16(raw):
    '''Reinterpret an unsigned 16-bit value as signed int16.

    Equivalent to np.int16(raw) for raw in [0, 65535], but numpy's out-of-range
    cast is deprecated (warns) on this NumPy version and raises OverflowError on
    NumPy 2.x — every negative flow/volume/valve-mask reading has the high bit
    set and would hit that path.
    '''
    return raw - 65536 if raw > 32767 else raw


def raw_to_psi(raw_pressure):
    '''Convert a raw SSCX pressure count to psi.'''
    return (raw_pressure - MCU_CONSTANTS._output_min) * (MCU_CONSTANTS._p_max - MCU_CONSTANTS._p_min) / (MCU_CONSTANTS._output_max - MCU_CONSTANTS._output_min) + MCU_CONSTANTS._p_min


def split_byte(byte_in):
    '''
    Split single byte int two nibbles
    '''
    return ((byte_in >> 4), (byte_in & 0x0F))
def uint_to_bytes(uint, n_bytes):
    '''
    Split unsigned integer into n bytes. Returns error if number of bytes is too small
    '''
    out = []
    n_bytes_req = (np.ceil(np.log2(max(uint, 1))/8))
    assert n_bytes_req <= n_bytes, f"Overflow error, need at least {n_bytes_req} bytes"
    for i in range(n_bytes-1, -1, -1):
        shift_amt = i * 8
        shifted = uint >> shift_amt
        out.append(np.uint8(shifted & 0xFF))
    return out

def _interruptible_sleep(seconds, run_control):
    """Wait out a poll interval, or raise the run's cause if it is cancelled.

    The interval doubles as the wait on the signal, so a cancelled run is
    noticed within one interval rather than at the caller's deadline. A None
    signal is the uninterruptible case -- see wait_for_completion.
    """
    if run_control is None:
        sleep(seconds)
    else:
        run_control.sleep(seconds)


class PacketSubscribers:
    '''Mixin giving a controller a packet-stream subscriber list.

    Two flow sensors is the case that forced this — a single callback slot
    meant the second sensor silently displaced the first.
    '''

    def _init_packet_subscribers(self):
        self._packet_subscribers = Subscribers("Packet")

    def subscribe_packets(self, callback):
        '''Register callback(parsed_packet), called once per received packet.'''
        self._packet_subscribers.subscribe(callback)

    def unsubscribe_packets(self, callback):
        self._packet_subscribers.unsubscribe(callback)

    def _notify_packet_subscribers(self, parsed):
        self._packet_subscribers.notify(parsed)


# Define basic input/output from the microcontroller
class Microcontroller():
    def __init__(self, serial_number, use_cobs = True, cmd_len = MCU_CMD_LENGTH, buffer_len = MCU_MSG_LENGTH):
        '''
        Arguments:
            string serial_number: serial number of target microcontroller
            bool use_cobs: set True to enable Consistent Overhead Byte Stuffing encoding, default value True
        '''
        self.serial = None
        self.serial_number = serial_number
        self.use_cobs = use_cobs

        # Parameters for fixed-length messages 
        self.tx_buffer_length = cmd_len
        self.rx_buffer_length = buffer_len

        self.read_buffer = []
        return
    
    def __del__(self):
        '''
        Close the serial port
        '''
        self.serial.close()
        return
    
    def begin(self):
        '''
        Find a Serial device that matches the serial number and connect to it.
        '''
        self.read_buffer = []
        port = find_serial_port(self.serial_number, "Fluid controller (Teensy)")
        self.serial = open_serial_port(port, "Fluid controller (Teensy)",
                                       baudrate=2000000)
        _logger.info('Teensy connected')
        return
    
    def send_mcu_command(self, cmd):
        '''
        Format using COBS (for PacketSerial library) if necessary and write
        Arguments:
            uint8[] cmd: list of uint8 or equivalent
        Output:
            Writes data over Serial
        Returns:
            None
        '''
        cmd = bytearray(cmd)
        if self.use_cobs:
            cmd = bytearray(cobs.encode(cmd))
            cmd.append(0)
        
        self.serial.write(cmd)
        return
    
    def read_received_packet_nowait(self, discard_buffer=False):
        '''
        If there is serial data available, return it as an array of bytes.
        If we are using COBS, decode it first
        Arguments:
            None
        Inputs:
            Reads data over Serial
        Returns:
            uint8_t data[] or None: either the decoded data (variable length) or None
        '''
        if self.serial.in_waiting == 0:
            return None
        # If we want to get only the latest data, read and discard data until the buffer is the right length
        byte_in = None
        if discard_buffer:
            self.read_buffer = []
            while self.serial.in_waiting > (2 * self.rx_buffer_length):
                byte_in = ord(self.serial.read())
            if self.serial.in_waiting > self.rx_buffer_length:
                if (self.use_cobs) and (byte_in is not None):
                    while byte_in != 0:
                        byte_in = ord(self.serial.read())

        if self.use_cobs:
            # Read data into read_buffer until we hit an end-of-packet (0x00)
            while self.serial.in_waiting:
                byte_in = ord(self.serial.read())
                if byte_in != 0:
                    self.read_buffer.append(byte_in)
                else:
                    break

            # If the last byte isn't 0, we didn't read a full packet yet
            if byte_in != 0:
                return None
            # If it is 0, we have a full packet to decode. Clear the read buffer
            else:
                try:
                    output = cobs.decode(bytearray(self.read_buffer))
                finally:
                    # Clear on failure as well as success. The byte loop above
                    # always consumes through a delimiter, so a corrupt frame is
                    # fully drained by the time we get here — keeping its bytes
                    # would prepend them to the next frame and make every later
                    # decode fail too, until a spurious 0x00 happened to resync.
                    self.read_buffer = []

        # If we aren't using COBS, use fixed-length command rx
        else:
            # If we have too many bytes in the buffer, throw out the excess
            buffer_len = self.serial.in_waiting
            if buffer_len > self.rx_buffer_length:
                for _ in range(buffer_len - self.rx_buffer_length):
                    self.serial.read()
            # If we have too few bytes, return None
            elif buffer_len < self.rx_buffer_length:
                return None
            
            # Read the buffer
            output = []
            for _ in range(self.rx_buffer_length):
                output.append(ord(self.serial.read()))
        
        return output
    

class FluidController(Microcontroller, PacketSubscribers):
    def __init__(self, serial_number, use_cobs = True, log_measurements = False, debug = False):
        '''
        Initialize logging and microcontroller connection. This class inherits from Microcontroller
        Arguments:
            String serial_number: serial number of the microcontroller
            bool use_cobs: use Consistent Overhead Byte Stuffing for Serial I/O
            bool log_measurements: save logs to a CSV
            bool debug: print debug info
        '''
        self.log_measurements = log_measurements

        if(self.log_measurements):
            self.measurement_file = open(os.path.join(Path.home(),"Downloads","Fluidic Controller Logged Measurement_" + datetime.now().strftime('%Y-%m-%d %H-%M-%S.%f') + ".csv"), "w+")
            self.measurement_file.write("timestamp,rx_uid,rx_cmd,cmd_status,mcu_state,bs1,bs2,mcu_time,sv0,sv1,sv2,sv3,sv4,valves,pump,p0,p1,p2,p3,f1_raw,f2_raw,vol_uL\n")
            self.counter_measurement_file_flush = 1

        self.cmd_uid = 0
        self.cmd_sent = CMD_SET.CLEAR
        self.timestamp_last_mismatch = None
        self.debug = debug

        self.serial_number = serial_number
        self.use_cobs = use_cobs

        self.recorded_data = {}

        self._init_status_state()

        super().__init__(self.serial_number, self.use_cobs)

        return

    def close(self):
        '''Stop the reader thread, flush the log, and release the serial port.

        Callers should invoke this explicitly rather than leaving the port to
        __del__: the reader thread owns the port for the process's lifetime, so
        without an orderly stop the device can stay busy and a second run in the
        same process cannot reopen it.

        Idempotent, and safe on a partially-constructed object. Defensive
        attribute lookups matter because if __init__ raised partway through
        (e.g. the log file failed to open, before _init_status_state() or
        Microcontroller.__init__() ran), GC still calls __del__ on the
        half-built object; plain attribute access would raise AttributeError
        here and mask whatever error __init__ originally raised.
        '''
        if getattr(self, '_reader_thread', None) is not None:
            self.stop_reading()
        if getattr(self, 'measurement_file', None) is not None:
            self.measurement_file.close()
            self.measurement_file = None
        if getattr(self, 'serial', None) is not None:
            self.serial.close()
            self.serial = None

    def __del__(self):
        self.close()

    def begin(self):
        '''Connect to the microcontroller, then start the reader thread that owns the port.'''
        super().begin()
        self.start_reading()
    
    def wait_for_completion(self, timeout=30, run_control=None):
        '''Block until the MCU reports a terminal status for the command we just sent.

        run_control makes the wait interruptible: a cancelled run raises its
        cause out of the poll instead of waiting out the deadline. Pass it
        where the command's own bound is longer than an abort should wait --
        a rotary valve move is the case this exists for, since RheoLink
        retries around a 2 s poll. A command already bounded at a second or
        two (the disc pump's power commands) does not need it, and one
        reachable from stop(), close() or DeviceSet.make_safe() must not have
        it: those run on a run that is already cancelled and have to confirm
        their command completed.

        Matches on both the command UID and the publish sequence number
        (`_status_seq`, bumped by `_publish_status`), not UID alone. UID is not
        enough: CMD_SET.CLEAR resets both the host's and the firmware's UID
        counter to 0 (controller_teensy41.ino), so two consecutive CLEAR
        commands share UID 0. Without the sequence check, the terminal packet
        still sitting there from the *first* CLEAR -- same UID, already
        published -- would be accepted for the second one before it had even
        reached the firmware. Requiring seq > seq-at-send restricts acceptance
        to a packet the reader thread published after this command went out,
        which is the guarantee this method's contract actually needs.

        Bounded even if the MCU has never sent a single packet (unplugged,
        crashed, wrong serial port): this polls a non-blocking snapshot
        (`_peek_status`) rather than `get_mcu_status()`, which would block
        forever waiting for the first packet and defeat the timeout entirely.
        '''
        deadline = time() + timeout
        while True:
            data, seq = self._peek_status()
            if (data is not None
                    and data['MCU_received_command_UID'] == self.cmd_uid
                    and seq > self._seq_at_send):
                status = data['MCU_command_execution_status']
                if status != COMMAND_STATUS.IN_PROGRESS:
                    return status
            if time() > deadline:
                raise TimeoutError(
                    f"MCU did not complete command {self.cmd_sent} "
                    f"(uid {self.cmd_uid}) within {timeout}s"
                )
            _interruptible_sleep(0.005, run_control)

    def add_uid_to_cmd(self, cmd):
        '''Break cmd_uid into two bytes and overwrite the first two bytes of the command array with the uid'''
        cmd[0] = self.cmd_uid >> 8
        cmd[1] = self.cmd_uid & 0xFF
        return cmd
    
    def _parse_packet(self, msg):
        '''Decode a 30-byte MCU status packet into a dict. Pure — no I/O.

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
        byte 23-24  : flow sensor 1 reading
        byte 25-26  : flow sensor 2 reading
        byte 27     : elapsed time since the start of the last internal program (in seconds)
        byte 28-29  : total volume (ul), range: 0 - 5000
        '''
        MCU_received_command_UID = (msg[0] << 8) + msg[1]
        MCU_received_command = msg[2]
        MCU_command_execution_status = msg[3]
        MCU_interal_program = msg[4]

        bubble_sensor_1_state, bubble_sensor_2_state = split_byte(msg[5])

        selector_valve_1_pos = msg[6]
        selector_valve_2_pos = msg[7]
        selector_valve_3_pos = msg[8]
        selector_valve_4_pos = msg[9]
        selector_valve_5_pos = msg[10]

        solenoid_valves = to_int16((int(msg[11]) << 8) + msg[12])

        measurement_pump_power = MCU_CONSTANTS.TTP_MAX_PW * float((int(msg[13]) << 8) + msg[14]) / np.iinfo(np.uint16).max

        _pressure_1_raw = (int(msg[15]) << 8) + msg[16]
        _pressure_2_raw = (int(msg[17]) << 8) + msg[18]
        _pressure_3_raw = (int(msg[19]) << 8) + msg[20]
        _pressure_4_raw = (int(msg[21]) << 8) + msg[22]

        pressure_1 = raw_to_psi(_pressure_1_raw)
        pressure_2 = raw_to_psi(_pressure_2_raw)
        pressure_3 = raw_to_psi(_pressure_3_raw)
        pressure_4 = raw_to_psi(_pressure_4_raw)

        # Raw int16 exactly as received. 32767 is the SLF3X "no reading"
        # sentinel, which the driver compares against before scaling.
        flow_1_raw = to_int16((int(msg[23]) << 8) + msg[24])
        flow_2_raw = to_int16((int(msg[25]) << 8) + msg[26])

        MCU_CMD_time_elapsed = msg[27]

        vol_ul = (float(to_int16((int(msg[28]) << 8) + msg[29])) / np.iinfo(np.int16).max) * MCU_CONSTANTS.VOLUME_UL_MAX

        return {
            "MCU_received_command_UID": MCU_received_command_UID,
            "MCU_received_command": MCU_received_command,
            "MCU_command_execution_status": MCU_command_execution_status,
            "MCU_interal_program": MCU_interal_program,
            "bubble_sensor_states": [bubble_sensor_1_state, bubble_sensor_2_state],
            "MCU_CMD_time_elapsed": MCU_CMD_time_elapsed,
            "selector_valves_pos": [selector_valve_1_pos, selector_valve_2_pos, selector_valve_3_pos, selector_valve_4_pos, selector_valve_5_pos],
            "solenoid_valves": solenoid_valves,
            "measurement_pump_power": measurement_pump_power,
            "pressures": [pressure_1, pressure_2, pressure_3, pressure_4],
            # Raw counts only. Turning these into uL/min needs the installed
            # sensor's scale factor, which lives with the driver that knows
            # which part it is talking to (flow_sensor.py). Scaling here as
            # well would be a second copy, free to disagree -- and it did.
            "flowrates_raw": [flow_1_raw, flow_2_raw],
            "vol_ul": vol_ul,
        }

    def _init_status_state(self):
        '''Set up shared packet state. Called from __init__ and by tests.'''
        self._status_lock = threading.Lock()
        self._latest_status = None
        self._status_seq = 0
        self._seq_at_send = 0
        self._reader_thread = None
        self._terminate_reader = False
        self._init_packet_subscribers()

    def _publish_status(self, parsed):
        '''Store a parsed packet, bump the sequence, notify subscribers.'''
        with self._status_lock:
            self._latest_status = parsed
            self._status_seq += 1
            self.recorded_data = parsed

        self._log_packet(parsed)
        self._notify_packet_subscribers(parsed)

    def _reader_loop(self):
        while not self._terminate_reader:
            try:
                msg = self.read_received_packet_nowait()
                if msg is None:
                    sleep(READER_IDLE_SLEEP_S)
                    continue
                if len(msg) != MCU_MSG_LENGTH:
                    continue
                self._publish_status(self._parse_packet(msg))
            except Exception as e:
                # A corrupt COBS frame raises from cobs.decode, and the port
                # raises during shutdown. Neither should kill the only thread
                # feeding every consumer — drop the frame and carry on.
                # read_received_packet_nowait clears its own buffer on a failed
                # decode, so the next frame resyncs deterministically.
                if not self._terminate_reader:
                    _logger.error("Reader thread error: %s", e)
                sleep(READER_IDLE_SLEEP_S)

    def start_reading(self):
        '''Begin consuming packets. The reader thread owns the serial port.'''
        if self._reader_thread is not None:
            return
        self._terminate_reader = False
        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader_thread.start()

    def stop_reading(self):
        self._terminate_reader = True
        if self._reader_thread is not None:
            self._reader_thread.join(timeout=2)
            self._reader_thread = None

    def _log_packet(self, d):
        if not (self.log_measurements or self.debug):
            return
        b1, b2 = d["bubble_sensor_states"]
        sv = d["selector_valves_pos"]
        p = d["pressures"]
        f = d["flowrates_raw"]
        line = (f"{datetime.now().strftime('%m/%d %H:%M:%S')},"
                f"{d['MCU_received_command_UID']},"
                f"{d['MCU_received_command']},"
                f"{d['MCU_command_execution_status']},"
                f"{d['MCU_interal_program']},"
                f"{b1:>04b},{b2:>04b},"
                f"{d['MCU_CMD_time_elapsed']},"
                f"{sv[0]},{sv[1]},{sv[2]},{sv[3]},{sv[4]},"
                f"{d['solenoid_valves']:>016b},"
                f"{d['measurement_pump_power']:.2f},"
                f"{p[0]:.2f},{p[1]:.2f},{p[2]:.2f},{p[3]:.2f},"
                f"{f[0]},{f[1]},"
                f"{d['vol_ul']:.2f}\n")
        if self.log_measurements:
            self.measurement_file.write(line)
            self.counter_measurement_file_flush += 1
            if self.counter_measurement_file_flush >= 500:
                self.counter_measurement_file_flush = 0
                self.measurement_file.flush()
        if self.debug:
            _logger.debug(line.rstrip())

    def _peek_status(self):
        '''Non-blocking: return (parsed packet copy, publish sequence number),
        or (None, None) if the reader thread has not published anything yet.

        Both values are read from a single lock acquisition so they describe
        the same packet -- reading them separately could interleave with the
        reader thread publishing a new one in between. Used by callers (like
        wait_for_completion) that must enforce their own timeout instead of
        blocking indefinitely, unlike get_mcu_status().
        '''
        with self._status_lock:
            if self._latest_status is not None:
                return dict(self._latest_status), self._status_seq
        return None, None

    def get_mcu_status(self):
        '''Return the most recent packet as a dict, waiting for the first one.

        Blocks indefinitely if no packet has ever been published (e.g. called
        before the MCU has sent anything). Callers that need a bound on that
        wait -- such as wait_for_completion -- must use _peek_status() instead.
        '''
        while True:
            data, _ = self._peek_status()
            if data is not None:
                return data
            sleep(0.001)

    def send_command(self, command, *args):
        '''
        Commands are formatted as UID, command, parameters (arb. length)
        Parameters are formatted differently depending on the command
        '''
        command_array = [0, 0] # Initialize with two empty cells for UID
        self.cmd_uid += 1

        self.cmd_sent = np.uint8(command)
        assert self.cmd_sent == command, "Command is not uint8"
        command_array.append(self.cmd_sent)

        if command == CMD_SET.CLEAR:
            self.cmd_uid = 0
            pass
        elif command == CMD_SET.INITIALIZE_DISC_PUMP:
            # For disc pump, we need pwr limit (2 bytes), source, mode, and stream config
            assert len(args) == 1, "Need power limit"
            pwr_lim = np.uint16(args[0])
            assert pwr_lim == args[0], "Power limit not a uint16"

            pwr_lim_hi, pwr_lim_lo = uint_to_bytes(pwr_lim, 2)

            command_array.append(pwr_lim_hi)
            command_array.append(pwr_lim_lo)
            pass
        elif command == CMD_SET.INITIALIZE_PRESSURE_SENSOR:
            # Need index of pressure sensor to init
            assert len(args) == 1, "Need sensor index"
            idx = np.uint8(args[0])
            assert idx == args[0], "Index is not a uint8"
            
            command_array.append(idx)
            pass
        elif command == CMD_SET.INITIALIZE_FLOW_SENSOR:
            # Need index, medium, and do_crc
            assert len(args) == 3, "Need I2C index, liquid medium, and whether we do CRC positional arguments"
            idx = np.uint8(args[0])
            assert idx == args[0], "Index not an int16"
            medium = np.uint8(args[1])
            assert medium in MCU_CONSTANTS.MEDIA, "Medium not recognized, must be MEDIUM_IPA or MEDIUM_WATER"
            do_crc = bool(args[2])
            assert do_crc == args[2], "do_crc setting is not a boolean"

            command_array.append(idx)
            command_array.append(medium)
            command_array.append(do_crc)
            pass
        elif command == CMD_SET.INITIALIZE_BUBBLE_SENSORS:
            # Need no additional info
            assert len(args) == 0, "Unnecessary arguments present"
            pass
        elif command == CMD_SET.INITIALIZE_VALVES:
            # Need no additional info
            assert len(args) == 0, "Unnecessary arguments present"
            pass
        elif command == CMD_SET.INITIALIZE_BANG_BANG_PARAMS:
            # Need loop type, low threshold, high threshold, min out, max out, and timestep
            assert len(args) == 6, "Need loop type, low/high thresholds, min/max outputs, and timestep (ms)"
            
            loop_type = np.uint8(args[0])
            assert loop_type in MCU_CONSTANTS.BB_LOOP_TYPES, "loop type is not a bang-bang type"

            t_lower_intermediate = args[1] * MCU_CONSTANTS.MCU_ASSUMED_SCALE_FACTOR_FLOW
            t_lower = np.uint16(t_lower_intermediate)
            assert t_lower_intermediate == t_lower, "Error calculating lower bound"
            t_upper_intermediate = args[2] * MCU_CONSTANTS.MCU_ASSUMED_SCALE_FACTOR_FLOW
            t_upper = np.uint16(t_upper_intermediate)
            assert t_upper_intermediate == t_upper, "Error calculating upper bound"

            o_lower = np.uint16(args[3])
            assert args[3] == o_lower, "Lower output not uint16"
            o_upper = np.uint16(args[4])
            assert args[4] == o_upper, "Upper output not uint16"

            timestep = np.uint32(args[5])
            assert timestep == args[5], "Timestep is not uint32"

            t_lower_hi, t_lower_lo = uint_to_bytes(t_lower, 2)
            t_upper_hi, t_upper_lo = uint_to_bytes(t_upper, 2)
            o_lower_hi, o_lower_lo = uint_to_bytes(o_lower, 2)
            o_upper_hi, o_upper_lo = uint_to_bytes(o_upper , 2)

            tstep_3, tstep_2, tstep_1, tstep_0 = uint_to_bytes(timestep, 4)

            command_array.append(loop_type)
            command_array.append(t_lower_hi)
            command_array.append(t_lower_lo)
            command_array.append(t_upper_hi)
            command_array.append(t_upper_lo)
            command_array.append(o_lower_hi)
            command_array.append(o_lower_lo)
            command_array.append(o_upper_hi)
            command_array.append(o_upper_lo)
            command_array.append(tstep_3)
            command_array.append(tstep_2)
            command_array.append(tstep_1)
            command_array.append(tstep_0)

            pass
        elif command == CMD_SET.INITIALIZE_PID_PARAMS:
            # Need loop type, Kp, Ki, Kd, intergral winding limit, min/max outputs, and timestep
            assert len(args) == 8, "Need loop type, Kp/Ki/Kd, winding limit, min/max outputs, and timestep (ms)"

            loop_type = np.uint8(args[0])
            assert loop_type in MCU_CONSTANTS.PID_LOOP_TYPES, "loop type is not PID type"

            kp_intermediate = int((args[1]/MCU_CONSTANTS.KP_MAX) * np.iinfo(np.uint16).max)
            kp = np.uint16(kp_intermediate)
            assert kp_intermediate == kp, "Error calculating Kp"

            ki_intermediate = int((args[2]/MCU_CONSTANTS.KI_MAX) * np.iinfo(np.uint16).max)
            ki = np.uint16(ki_intermediate)
            assert ki_intermediate == ki, "Error calculating Ki"

            kd_intermediate = int((args[3]/MCU_CONSTANTS.KD_MAX) * np.iinfo(np.uint16).max)
            kd = np.uint16(kd_intermediate)
            assert kd_intermediate == kd, "Error calculating Kd"

            ilim_intermediate = int((args[4]/MCU_CONSTANTS.ILIM_MAX) * np.iinfo(np.uint16).max)
            ilim = np.uint16(ilim_intermediate)
            assert ilim_intermediate == ilim, "Error calculating integral winding limit"
            

            o_lower = np.uint16(args[5])
            assert args[5] == o_lower, "Lower output not uint16"
            o_upper = np.uint16(args[6])
            assert args[6] == o_upper, "Upper output not uint16"

            timestep = np.uint32(args[7])
            assert timestep == args[7], "Timestep is not uint32"

            kp_hi, kp_lo = uint_to_bytes(kp, 2)
            ki_hi, ki_lo = uint_to_bytes(ki, 2)
            kd_hi, kd_lo = uint_to_bytes(kd, 2)
            ilim_hi, ilim_lo = uint_to_bytes(ilim, 2)
            o_lower_hi, o_lower_lo = uint_to_bytes(o_lower, 2)
            o_upper_hi, o_upper_lo = uint_to_bytes(o_upper , 2)

            tstep_3, tstep_2, tstep_1, tstep_0 = uint_to_bytes(timestep, 4)

            command_array.append(loop_type)
            command_array.append(kp_hi)
            command_array.append(kp_lo)
            command_array.append(ki_hi)
            command_array.append(ki_lo)
            command_array.append(kd_hi)
            command_array.append(kd_lo)
            command_array.append(ilim_hi)
            command_array.append(ilim_lo)
            command_array.append(o_lower_hi)
            command_array.append(o_lower_lo)
            command_array.append(o_upper_hi)
            command_array.append(o_upper_lo)
            command_array.append(tstep_3)
            command_array.append(tstep_2)
            command_array.append(tstep_1)
            command_array.append(tstep_0)
            pass
        elif command == CMD_SET.SET_SOLENOID_VALVES:
            # For setting all valves, need 16 bit setting
            assert len(args) == 1, "Need setting"
            setting = np.uint16(args[0])
            assert setting == args[0], "Setting not an int16"

            setting_hi, setting_lo = uint_to_bytes(setting, 2)
            command_array.append(setting_hi)
            command_array.append(setting_lo)
            pass
        elif command == CMD_SET.SET_SOLENOID_VALVE:
            # For setting an individual valve, need on/off bool and index
            assert len(args) == 2, "Need on/off bool and index"
            on_off = bool(args[0])
            assert on_off == args[0], "on_off setting is not a boolean"
            idx = np.uint8(args[1])
            assert idx == args[1], "idx is not uint8"

            command_array.append(on_off)
            command_array.append(idx)
            pass
        elif command == CMD_SET.SET_PUMP_PWR_OPEN_LOOP:
            # To set the pump power, we just need the power
            assert len(args) == 1, "Need power (milliwatts)"
            pwr = np.uint16(args[0])
            assert pwr == args[0], "power is not uint16"
            pwr_hi, pwr_lo = uint_to_bytes(pwr, 2)
            command_array.append(pwr_hi)
            command_array.append(pwr_lo)
            pass
        elif command == CMD_SET.INITIALIZE_ROTARY:
            # Initialize rotary valve with index and max positions
            assert len(args) == 2, "Need index and number of positions"
            idx = np.uint8(args[0])
            assert idx == args[0], "index is not uint8"
            n_pos = np.uint8(args[1])
            assert n_pos == args[1], "Number of positions is not uint8"

            command_array.append(idx)
            command_array.append(n_pos)
            pass
        elif command == CMD_SET.SET_ROTARY_VALVE:
            # Set rotary valve position, we need index and target position
            assert len(args) == 2, "Need index and target position"
            idx = np.uint8(args[0])
            assert idx == args[0], "index is not uint8"
            target = np.uint8(args[1])
            assert target == args[1], "target position is not uint8"

            command_array.append(idx)
            command_array.append(target)
            pass
        elif command ==  CMD_SET.BEGIN_CLOSED_LOOP:
            # Begin one of the control loops. We need the type of loop to begin
            assert len(args) == 1, "Need loop type"
            loop_type = np.uint8(args[0])
            assert loop_type in MCU_CONSTANTS.LOOP_TYPES, "loop type not valid"
            
            command_array.append(loop_type)
            pass
        elif command == CMD_SET.STOP_CLOSED_LOOP:
            # Need no additional info
            assert len(args) == 0, "Unnecessary arguments present"
            pass
        elif (command == CMD_SET.REMOVE_ALL_MEDIUM) or (command == CMD_SET.EJECT_MEDIUM):
            # Need open loop disc pump power, debounce time (ms), timeout time(ms), and pressure scale
            assert len(args) == 4, "Need power, debounce time, timeout time, and pressure scale"

            o_power = np.uint16(args[0])
            assert args[0] == o_power, "Open loop power not uint16"
            
            t_debounce = np.uint16(args[1])
            assert t_debounce == args[1], "debounce time is not uint16"

            t_timeout = np.uint16(args[2])
            assert t_timeout == args[2], "timeout time is not uint16"

            pscale_intermediate = int(args[3] * np.iinfo(np.uint8).max)
            pscale = np.uint8(pscale_intermediate)
            assert pscale == pscale_intermediate, "error calculating pscale"

            pwr_hi, pwr_lo = uint_to_bytes(o_power, 2)
            db_hi, db_lo = uint_to_bytes(t_debounce, 2)
            tt_hi,tt_lo = uint_to_bytes(t_timeout, 2)
            command_array.append(pwr_hi)
            command_array.append(pwr_lo)
            command_array.append(tt_hi)
            command_array.append(tt_lo)
            command_array.append(db_hi)
            command_array.append(db_lo)
            command_array.append(pscale)
            pass
        elif command == CMD_SET.CLEAR_LINES:
            # Need open loop disc pump power, debounce time (ms), and timeout time(ms)
            assert len(args) == 3, "Need power, debounce time, and timeout time"

            o_power = np.uint16(args[0])
            assert args[0] == o_power, "Open loop power not uint16"
            
            t_debounce = np.uint16(args[1])
            assert t_debounce == args[1], "debounce time is not uint16"

            t_timeout = np.uint16(args[2])
            assert t_timeout == args[2], "timeout time is not uint16"

            pwr_hi, pwr_lo = uint_to_bytes(o_power, 2)
            db_hi, db_lo = uint_to_bytes(t_debounce, 2)
            tt_hi,tt_lo = uint_to_bytes(t_timeout, 2)
            command_array.append(pwr_hi)
            command_array.append(pwr_lo)
            command_array.append(tt_hi)
            command_array.append(tt_lo)
            command_array.append(db_hi)
            command_array.append(db_lo)
            
        elif command == CMD_SET.LOAD_FLUID_TO_SENSOR:
            # Need open loop power and timeout time 
            assert len(args) == 2, "Need power and timeout time"

            pwr = np.uint16(args[0])
            assert pwr == args[0], "power is not uint16"
            t_timeout = np.uint16(args[1])
            assert t_timeout == args[1], "timeout time is not uint16"

            pwr_hi, pwr_lo = uint_to_bytes(pwr, 2)
            tt_hi,tt_lo = uint_to_bytes(t_timeout, 2)
            command_array.append(pwr_hi)
            command_array.append(pwr_lo)
            command_array.append(tt_hi)
            command_array.append(tt_lo)
            pass
        elif command == CMD_SET.VOL_INTEGRATE_SETTING:
            # Set whether to perform volume integration and reset the integrated volume
            assert len(args) == 2, "Need vol integration setting and reset setting"
            do_integration = bool(args[0])
            assert do_integration == args[0], "do_integration setting is not a boolean"
            reset_volume = bool(args[1])
            assert reset_volume == args[1], "reset_volume setting is not a boolean" 

            command_array.append(do_integration)
            command_array.append(reset_volume)
            pass
        elif (command == CMD_SET.LOAD_FLUID_VOLUME) or (command == CMD_SET.UNLOAD_FLUID_VOLUME):
            # Need control type, setpoint (ignore if control type is BB), timeout time in ms, and volume to load
            loop_type = np.uint8(args[0])
            assert loop_type == args[0], "Loop type must be uint8"
            if (loop_type == MCU_CONSTANTS.FLUID_IN_BANG_BANG) or (loop_type == MCU_CONSTANTS.FLUID_OUT_BANG_BANG) :
                assert len(args) == 3, "Need control type, timeout time, and volume target"
                i_shift = 1
            else:
                assert len(args) == 4, "Need control type, setpoint, timeout time, and volume target"
                i_shift = 0
            
            if command == CMD_SET.LOAD_FLUID_VOLUME:
                assert loop_type in [MCU_CONSTANTS.OPEN_LOOP_CTRL, MCU_CONSTANTS.FLUID_IN_BANG_BANG, MCU_CONSTANTS.VACUUM_PID], "Control type must be open loop, fluid in BB, or vacuum PID"
            else:
                assert loop_type in [MCU_CONSTANTS.OPEN_LOOP_CTRL, MCU_CONSTANTS.FLUID_OUT_BANG_BANG, MCU_CONSTANTS.FLUID_OUT_PID, MCU_CONSTANTS.PRESSURE_PID], "Control type must be open loop, fluid out BB, fluid out PID, or pressure PID"
            
            t_timeout = np.uint16(args[2-i_shift])
            assert t_timeout == args[2-i_shift], "timeout time is not uint16"

            volume_intermediate = int((args[3-i_shift]/MCU_CONSTANTS.VOLUME_UL_MAX) * np.iinfo(np.uint16).max)
            volume = np.uint16(volume_intermediate)
            assert volume_intermediate == volume, "Error calculating volume"

            # Setpoint is either a pressure or disc pump power depending on loop type
            setpoint = 0
            if loop_type == MCU_CONSTANTS.OPEN_LOOP_CTRL:
                setpoint = np.uint16(args[1])
                assert setpoint == args[1], "power not uint16"
            elif (loop_type == MCU_CONSTANTS.VACUUM_PID) or (loop_type == MCU_CONSTANTS.PRESSURE_PID):
                setpoint_intermediate = int(MCU_CONSTANTS._output_min + (args[0] - MCU_CONSTANTS._p_min) * (MCU_CONSTANTS._output_max - MCU_CONSTANTS._output_min) / (MCU_CONSTANTS._p_max - MCU_CONSTANTS._p_min))
                setpoint = np.uint16(setpoint_intermediate)
                assert setpoint == setpoint_intermediate, "Error calculating pressure setpoint"
            elif loop_type == MCU_CONSTANTS.FLUID_OUT_PID:
                setpoint_intermediate = int((abs(args[1])/abs(MCU_CONSTANTS.SLF3X_MAX_VAL_uL_MIN)) * np.iinfo(np.uint16).max)
                setpoint = np.uint16(setpoint_intermediate)
                assert setpoint == setpoint_intermediate, "Error calculating pressure setpoint"
            
            # 1 for setting the control type, 2 for setpoint (if applicable), 2 for timeout, 2 for volume setpoint
            sp_hi, sp_lo = uint_to_bytes(setpoint, 2)
            tt_hi, tt_lo = uint_to_bytes(t_timeout, 2)
            vol_hi, vol_lo = uint_to_bytes(volume, 2)
            command_array.append(loop_type)
            command_array.append(sp_hi)
            command_array.append(sp_lo)
            command_array.append(tt_hi)
            command_array.append(tt_lo)
            command_array.append(vol_hi)
            command_array.append(vol_lo)
            pass
        elif command == CMD_SET.VENT_VB0:
            # 3 bytes for cmd and UID, 2 for vacuum threshold, 2 for timeout
            assert len(args) == 2, "Need vacuum threshold and timeout time"

            vacuum_intermediate = int(MCU_CONSTANTS._output_min + (args[0] - MCU_CONSTANTS._p_min) * (MCU_CONSTANTS._output_max - MCU_CONSTANTS._output_min) / (MCU_CONSTANTS._p_max - MCU_CONSTANTS._p_min))
            vacuum = np.uint16(vacuum_intermediate)
            assert vacuum_intermediate == vacuum, "Error calculating vacuum setting"
            t_timeout = np.uint16(args[1])
            assert t_timeout == args[1], "timeout time is not uint16"

            tt_hi, tt_lo = uint_to_bytes(t_timeout, 2)
            v_hi, v_lo = uint_to_bytes(vacuum, 2)
            command_array.append(v_hi)
            command_array.append(v_lo)
            command_array.append(tt_hi)
            command_array.append(tt_lo)
            pass
        elif command == CMD_SET.DELAY_MS:
            # Do nothing for set time
            assert len(args) == 1, "Need delay time"
            delaytime = np.uint32(args[0])
            assert delaytime == args[0], "delay time is not uint32"

            dt_3, dt_2, dt_1, dt_0 = uint_to_bytes(delaytime, 4)
            command_array.append(dt_3)
            command_array.append(dt_2)
            command_array.append(dt_1)
            command_array.append(dt_0)
        else:
            # If we don't recognize the command, raise an error
            raise Exception("Command not recognized")

        self.add_uid_to_cmd(command_array)
        self.send_mcu_command(command_array)
        # Record the publish sequence right after transmitting -- the write is
        # synchronous, so anything published before it returns is unambiguously
        # stale. Capturing this before send_mcu_command() would leave a window
        # between releasing the lock and the bytes actually leaving the host,
        # during which the reader thread could consume one of the firmware's
        # unconditional 60 ms idle packets and bump _status_seq, making a
        # packet that predates this command satisfy `seq > _seq_at_send`. See
        # wait_for_completion's docstring for why UID matching alone isn't
        # enough to catch that.
        with self._status_lock:
            self._seq_at_send = self._status_seq
        pass

    def send_command_blocking(self, command, *args, timeout=30):
        '''Send a command, then write logs while waiting for it to complete.

        timeout is keyword-only (falls out naturally after *args) and passed
        straight through to wait_for_completion. The 30 s default suits fast
        commands, but several firmware operations (e.g. CLEAR_LINES,
        UNLOAD_FLUID_VOLUME) take a caller-supplied timeout parameter of their
        own that routinely runs 35-50 s; callers issuing those must pass a
        matching timeout here or this will raise before the firmware finishes.
        '''
        self.send_command(command, *args)
        return self.wait_for_completion(timeout=timeout)


class FluidControllerSimulation(PacketSubscribers):
    # What a command takes on the wire, spent in wait_for_completion: the
    # simulation blocks where the real controller blocks, so an abort can
    # land in the same places.
    COMMAND_SECONDS = 1

    def __init__(self, serial_number, use_cobs = True, log_measurements = False, debug = False):
        self.data = {
            'selector_valves_pos': {0: 1, 1: 1, 2: 1, 3: 1, 4: 1}
        }
        # Every command asked of it, in order -- the record the time estimate
        # reads valve moves from, the way it reads chains from the pump's
        # `executed`.
        self.sent = []
        # Never fires — this class has no packet stream — but a real FlowSensor
        # against a simulated controller is a supported combination, so it must
        # offer the same subscribe/unsubscribe surface.
        self._init_packet_subscribers()
        return

    def begin(self):
        _logger.info("Simulated fluid controller.")

    def start_reading(self):
        pass

    def stop_reading(self):
        pass

    def close(self):
        pass

    def send_command(self, command, *args):
        self.sent.append((command,) + args)
        if command == CMD_SET.SET_ROTARY_VALVE:
            self.data['selector_valves_pos'][args[0]] = args[1]

    def send_command_blocking(self, command, *args, timeout=30):
        self.send_command(command, *args)
        return self.wait_for_completion(timeout=timeout)

    def wait_for_completion(self, timeout=30, run_control=None):
        """Where a simulated command spends its time, and so where a cancelled
        run raises -- the same place the real controller's poll would.

        Returns a terminal status like the real one: callers branch on it.
        """
        _interruptible_sleep(self.COMMAND_SECONDS, run_control)
        return COMMAND_STATUS.COMPLETED_WITHOUT_ERRORS

    def get_mcu_status(self):
        return self.data