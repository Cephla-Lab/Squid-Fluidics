Vendored copy of servo_service from https://github.com/Cephla-Lab/nm-servo
Commit: b76c5704adcc154218ea5e5f9ad08f2963f6267b (main, 2026-10-12, PRs #1-#10 merged; tag v0.2.0 once it exists)
License: MIT (nm-servo pyproject.toml; the upstream repository carries no LICENSE file yet)
Contents: the driver (servo_service -> fluidics.control.nimotion), its sim:// simulator and its unit tests
(tests/unit/control/nimotion). The jog GUI (servo_gui), bench tools and manuals stay upstream.

Maintenance rule (open question 4 of the plate-sipper design, decided 2026-10-12):
- Fix things in nm-servo first, never here.
- Refresh by repeating the copy of one merged commit of nm-servo main, in a reviewed commit, with
  ROBOT_DRIVER_REQUIRED=1 python -m pytest tests/unit/robot tests/unit/control/nimotion -q green.
- Record the new commit hash above. Do not edit in place.
Removed from the copied tests: test_motor_control/test_registers.py::test_gui_register_table_agrees_with_library,
which imports servo_gui (not vendored).
