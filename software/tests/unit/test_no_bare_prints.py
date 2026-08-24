# tests/unit/test_no_bare_prints.py
"""No bare print() in first-party fluidics code.

The logging migration converted all 30-odd of them; this keeps the next one
from slipping in. print output vanishes for a desktop-icon GUI launch and
never reaches the run log -- the two places the operator actually looks.
The vendored tecancavro/ is exempt, as always.
"""

import re
from pathlib import Path

import fluidics

PRINT_LINE = re.compile(r"^\s*print\(")


def test_fluidics_has_no_bare_prints():
    package_root = Path(fluidics.__file__).parent
    offenders = []
    for source in sorted(package_root.rglob("*.py")):
        if "tecancavro" in source.parts:
            continue
        for number, line in enumerate(source.read_text().splitlines(), 1):
            if PRINT_LINE.match(line):
                offenders.append(f"{source.relative_to(package_root)}:{number}")
    assert offenders == [], (
        "Use a module logger (see fluidics/run_log.py), not print: "
        + ", ".join(offenders))
