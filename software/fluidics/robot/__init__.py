"""The robot layer: labware geometry, the XYZ stage, the sipper. Pump-agnostic
(spec D3): only _driver.py imports the driver, the vendored fluidics.control.nimotion;
every other module imports nothing from fluidics.control (a fresh-interpreter test enforces it)."""
