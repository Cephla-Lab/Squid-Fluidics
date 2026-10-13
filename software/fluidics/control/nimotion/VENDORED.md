Vendored copy of servo_service from https://github.com/Cephla-Lab/nm-servo
Commit: 2f7146d4857a8b6629d14c3e5b493e6acd00a280 (main, 2026-10-13, PRs #1-#18 merged; no tag yet)
Previous copy: b76c5704adcc154218ea5e5f9ad08f2963f6267b (2026-10-12, PRs #1-#10). Refreshed 2026-10-13 for
#11 (stage window shutdown; GUI only), #12 (motion limits 607Fh/60C5h/60C6h written and read back on
connect), #13 (X1 stroke from 0.5 mm, clear of its origin switch), #14 (the blocking home() waits for the
axis to settle; the stage polls its own settle), #15 (Y2 0.5-109 mm), #16 (Z3 from 0.5 mm), #17 (X1 encoder
131072 counts/rev), #18 (the T5 tilt axis and axis units: stepper registers, raw register helpers,
reset_alarm, connect(initialize=), the guarded set_home_position, simulator changes; unused here).
License: MIT (nm-servo pyproject.toml; the upstream repository carries no LICENSE file yet)
Contents: the driver (servo_service -> fluidics.control.nimotion), its sim:// simulator and its unit tests
(tests/unit/control/nimotion). The jog GUI (servo_gui), bench tools and manuals stay upstream.

Maintenance rule (open question 4 of the plate-sipper design, decided 2026-10-12):
- Fix things in nm-servo first, never here.
- Refresh by repeating the copy of one merged commit of nm-servo main, in a reviewed commit, with
  ROBOT_DRIVER_REQUIRED=1 python -m pytest tests/unit/robot tests/unit/control/nimotion -q green.
- Record the new commit hash above. Do not edit in place.
Not copied from upstream's tests: tests/gui/ (12 files), test_tilt_model.py and
test_motor_control/test_registers.py::test_gui_register_table_agrees_with_library (all import servo_gui,
not vendored); test_headless.py (runs upstream's own checkout in a subprocess); test_set_encoder_resolution.py
(a bench tool under tools/, not vendored); tests/integration (hardware scripts, never collected upstream either). The copy is made by `git archive <commit>
servo_service tests`, the package taken verbatim, the tests' absolute imports rewritten with the regex
`(?<![\w.])servo_service\b` -> `fluidics.control.nimotion`.
