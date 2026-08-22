# tests/unit/control/test_firmware_mirror.py
"""The host/firmware protocol mirror, checked instead of trusted.

_def.py's command IDs, status codes, valve states, loop types and a handful of
scalars must match the firmware headers by hand -- both CLAUDE.md files call
the mirror critical, but until this test nothing failed when it drifted.
These tests parse the C++ headers directly (a regex is enough: the enums and
defines involved are flat integer literals) and compare.

When one of these fails, the fix is never "edit the test": change both sides
together, and remember the rig only picks up the firmware half after a
reflash (`pio run -t upload`).

Deliberate asymmetries, so failures mean something:
- CMD_SET and COMMAND_STATUS must match the firmware enums exactly, both
  directions -- an opcode or status either side lacks is a wire protocol
  mismatch.
- VALVE_POSITIONS: every state the firmware names must match, but Python may
  name extra masks (SET_SOLENOID_VALVES accepts any uint16; the extras are
  named data, not opcodes). The extras are still pinned to an explicit list
  so a new one cannot appear unreviewed.
- The 15-byte command length (MCU_CMD_LENGTH) has no named firmware constant
  to compare against -- onPacketReceived just indexes the buffer -- so only
  the 30-byte reply length is mirrored here.
"""

import re
from pathlib import Path

import pytest

from fluidics.control import _def
from fluidics.control._def import CMD_SET, COMMAND_STATUS, MCU_CONSTANTS, VALVE_POSITIONS

FIRMWARE = Path(__file__).resolve().parents[4] / "firmware"


def _read(header):
    text = (FIRMWARE / header).read_text()
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"//[^\n]*", "", text)


def parse_enum(header, enum_name):
    """name -> value for a flat C++ enum.

    Every member must carry an explicit `= value`: all the mirrored enums do,
    and for wire-protocol constants an implicit member is exactly the kind of
    change that deserves to fail loudly here and be written out, not silently
    auto-numbered by a test.
    """
    body = re.search(rf"enum\s+{enum_name}\s*(?::\s*\w+\s*)?\{{(.*?)\}}",
                     _read(header), re.S)
    assert body, f"enum {enum_name} not found in firmware/{header}"
    values = {}
    for entry in body.group(1).split(","):
        entry = entry.strip()
        if not entry:
            continue
        name, eq, literal = entry.partition("=")
        assert eq, (f"{enum_name}.{entry} in firmware/{header} has no explicit "
                    f"value; give it one")
        values[name.strip()] = int(literal.strip(), 0)
    return values


def parse_define(header, name):
    """The numeric value of one #define, as int when it is one."""
    m = re.search(rf"#define\s+{name}\s+(\S+)", _read(header))
    assert m, f"#define {name} not found in firmware/{header}"
    literal = m.group(1)
    if literal == "UINT16_MAX":
        return (1 << 16) - 1
    try:
        return int(literal, 0)          # decimal, hex (0x..), binary (0b..)
    except ValueError:
        return float(literal)           # 1000.0, 10.0 -- == is numeric anyway


def _members(cls):
    return {k: v for k, v in vars(cls).items()
            if not k.startswith("_") and isinstance(v, int)}


class TestEnumMirrors:
    def test_command_ids_match_exactly(self):
        assert _members(CMD_SET) == parse_enum("_defs.h", "SerialCommands_t")

    def test_status_codes_match_exactly(self):
        assert _members(COMMAND_STATUS) == parse_enum("_defs.h", "CommandExecution_t")

    def test_loop_types_match(self):
        # MCU_CONSTANTS carries much more than loop types, so this is
        # one-directional: every firmware loop type must exist there unchanged.
        for name, value in parse_enum("_defs.h", "ClosedLoopType_t").items():
            assert getattr(MCU_CONSTANTS, name, None) == value, (
                f"MCU_CONSTANTS.{name}: "
                f"{getattr(MCU_CONSTANTS, name, '<missing>')} != firmware {value}")

    def test_firmware_named_valve_states_match(self):
        firmware = parse_enum("_defs.h", "ValvesStates_t")
        python = _members(VALVE_POSITIONS)
        for name, value in firmware.items():
            assert python.get(name) == value, (
                f"VALVE_POSITIONS.{name}: "
                f"{python.get(name, '<missing>')} != firmware {value}")
        # Python-only masks are legal (named data, not opcodes) but must be
        # added here deliberately, not by drift.
        assert set(python) - set(firmware) == {"TEST_PRESSURE", "TEST_VACUUM"}


SCALAR_MIRRORS = [
    ("MCU_MSG_LENGTH", _def.MCU_MSG_LENGTH, "_defs.h", "FROM_MCU_MSG_LENGTH"),
    ("VOLUME_UL_MAX", MCU_CONSTANTS.VOLUME_UL_MAX, "_defs.h", "VOLUME_UL_MAX"),
    ("KP_MAX", MCU_CONSTANTS.KP_MAX, "_defs.h", "KP_MAX"),
    ("KI_MAX", MCU_CONSTANTS.KI_MAX, "_defs.h", "KI_MAX"),
    ("KD_MAX", MCU_CONSTANTS.KD_MAX, "_defs.h", "KD_MAX"),
    ("ILIM_MAX", MCU_CONSTANTS.ILIM_MAX, "_defs.h", "ILIM_MAX"),
    ("TTP_MAX_PW", MCU_CONSTANTS.TTP_MAX_PW, "TTP.h", "TTP_MAX_PWR"),
    ("MCU_ASSUMED_SCALE_FACTOR_FLOW", MCU_CONSTANTS.MCU_ASSUMED_SCALE_FACTOR_FLOW,
     "SLF3X.h", "SLF3X_SCALE_FACTOR_FLOW"),
    ("SLF3X_MAX_VAL_uL_MIN", MCU_CONSTANTS.SLF3X_MAX_VAL_uL_MIN,
     "SLF3X.h", "SLF3X_MAX_VAL_uL_MIN"),
    ("SLF3X_WATER", MCU_CONSTANTS.SLF3X_WATER, "SLF3X.h", "SLF3X_MEDIUM_WATER"),
    ("SLF3X_IPA", MCU_CONSTANTS.SLF3X_IPA, "SLF3X.h", "SLF3X_MEDIUM_IPA"),
    ("MEDIUM_WATER", MCU_CONSTANTS.MEDIUM_WATER, "SLF3X.h", "SLF3X_MEDIUM_WATER"),
    ("MEDIUM_IPA", MCU_CONSTANTS.MEDIUM_IPA, "SLF3X.h", "SLF3X_MEDIUM_IPA"),
]


class TestScalarMirrors:
    @pytest.mark.parametrize(
        "py_name,py_value,header,c_name", SCALAR_MIRRORS,
        ids=[m[0] for m in SCALAR_MIRRORS])
    def test_scalar_matches_firmware(self, py_name, py_value, header, c_name):
        assert py_value == parse_define(header, c_name), (
            f"{py_name} != firmware/{header}:{c_name}")
