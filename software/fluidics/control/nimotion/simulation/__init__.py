"""
Drive simulator

Use the port name "sim://" anywhere a serial port is expected (ServoService,
ModbusClient, the GUI) to talk to simulated drives instead of hardware:

    with ServoService(port="sim://") as service:
        service.enable()
        service.home()
        service.move_to(30.0, velocity_mm_s=50.0)

The simulated drives answer Modbus RTU frames, so the whole stack above the
serial port runs unchanged. See drive.py for what is and is not modelled.
"""

from .bus import (
    SIMULATOR_PORT,
    SimulatedBus,
    SimulatedSerial,
    get_bus,
    install_bus,
    is_simulator_port,
    reset_buses,
)
from .drive import SimulatedDrive

__all__ = [
    "SIMULATOR_PORT",
    "SimulatedBus",
    "SimulatedDrive",
    "SimulatedSerial",
    "get_bus",
    "install_bus",
    "is_simulator_port",
    "reset_buses",
]
