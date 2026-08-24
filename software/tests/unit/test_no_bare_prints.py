# tests/unit/test_no_bare_prints.py
"""No print() calls in first-party code.

The logging migration converted all 33 of them; this keeps the next one from
slipping in. print output vanishes for a desktop-icon GUI launch and never
reaches the run log -- the two places the operator actually looks. Detection
is AST-based so `if debug: print(x)` and `print (x)` cannot slip past a
line regex. The vendored tecancavro/ is exempt, as always; gui.py and
run_sequences.py are covered alongside the package.
"""

import ast
from pathlib import Path

import fluidics

PACKAGE_ROOT = Path(fluidics.__file__).parent
SOFTWARE_ROOT = PACKAGE_ROOT.parent


def _print_calls(source_path):
    tree = ast.parse(source_path.read_text())
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "print"):
            yield node.lineno


def test_first_party_code_has_no_print_calls():
    sources = [p for p in sorted(PACKAGE_ROOT.rglob("*.py"))
               if "tecancavro" not in p.parts]
    sources += [SOFTWARE_ROOT / "gui.py", SOFTWARE_ROOT / "run_sequences.py"]
    offenders = [
        f"{source.relative_to(SOFTWARE_ROOT)}:{lineno}"
        for source in sources
        for lineno in _print_calls(source)
    ]
    assert offenders == [], (
        "Use a module logger (see fluidics/run_log.py), not print: "
        + ", ".join(offenders))
