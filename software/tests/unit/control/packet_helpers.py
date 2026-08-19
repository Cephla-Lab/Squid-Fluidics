# tests/unit/control/packet_helpers.py
"""Shared builder for MCU status packets.

Lives here rather than in each test module so the byte layout is stated once.
Two copies had already drifted (one set the command byte, the other didn't),
and Phase 2 firmware will populate the second flow slot at bytes 25-26.
"""

from fluidics.control._def import COMMAND_STATUS, MCU_MSG_LENGTH


def make_status_packet(uid=1, cmd=0, status=COMMAND_STATUS.COMPLETED_WITHOUT_ERRORS,
                       flow_raw=1000, flow_2_raw=0):
    """Build a 30-byte MCU status packet.

    flow_raw and flow_2_raw are signed values; they are packed two's-complement
    into bytes 23-24 and 25-26 the way the firmware transmits them.
    """
    msg = [0] * MCU_MSG_LENGTH
    msg[0] = (uid >> 8) & 0xFF
    msg[1] = uid & 0xFF
    msg[2] = cmd
    msg[3] = status

    for offset, raw in ((23, flow_raw), (25, flow_2_raw)):
        unsigned = raw & 0xFFFF
        msg[offset] = (unsigned >> 8) & 0xFF
        msg[offset + 1] = unsigned & 0xFF

    return msg
