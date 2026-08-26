"""Shared pytest fixtures.

Everything here runs without a vehicle, without a network and without an API
key. The vehicle is the in-process simulator; the LLM is a scripted double;
the serial port and the J2534 library are fakes. That is what lets the whole
suite run in CI on every push.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Iterator

import pytest

# Make the test doubles importable as a plain module.
sys.path.insert(0, str(Path(__file__).parent))

from majster_ai.config import CanBackend, Settings, load_settings, reset_settings  # noqa: E402
from majster_ai.logging_setup import reset_logging  # noqa: E402
from majster_ai.mcp_servers.car_interface.backends import TransportFactory  # noqa: E402
from majster_ai.mcp_servers.car_interface.service import CarInterfaceService  # noqa: E402
from majster_ai.mcp_servers.car_interface.simulator import (  # noqa: E402
    VehicleSimulator,
    build_freelander2_simulator,
    build_healthy_simulator,
)


@pytest.fixture(autouse=True)
def _isolate_globals(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Keep process-wide state from leaking between tests.

    Settings are cached and logging installs handlers; without this a test that
    sets MAJSTER_WRITE_ENABLED could silently enable writes for every test that
    runs after it -- which is precisely the bug this suite exists to catch.
    """
    for name in list(os_environ_keys()):
        monkeypatch.delenv(name, raising=False)
    reset_settings()
    reset_logging()
    yield
    reset_settings()
    reset_logging()


def os_environ_keys() -> list[str]:
    """Environment variables that could perturb a test."""
    import os

    return [
        key
        for key in os.environ
        if key.startswith("MAJSTER_") or key in {"ANTHROPIC_API_KEY", "TAVILY_API_KEY"}
    ]


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Safe defaults: simulator, READ_ONLY, temporary directories."""
    return load_settings(
        can_backend=CanBackend.VIRTUAL,
        manuals_dir=tmp_path / "manuals",
        vector_dir=tmp_path / "vectorstore",
        uds_retries=1,
        uds_timeout=0.05,
        uds_retry_backoff=0.0,
        log_level="CRITICAL",
    )


@pytest.fixture
def write_settings(tmp_path: Path) -> Settings:
    """Writes enabled, approval still required -- the HITL path."""
    return load_settings(
        can_backend=CanBackend.VIRTUAL,
        manuals_dir=tmp_path / "manuals",
        vector_dir=tmp_path / "vectorstore",
        write_enabled=True,
        require_approval=True,
        uds_retries=1,
        uds_timeout=0.05,
        uds_retry_backoff=0.0,
        log_level="CRITICAL",
    )


@pytest.fixture
def vehicle() -> VehicleSimulator:
    """A Freelander 2 with the standard fault set."""
    return build_freelander2_simulator()


@pytest.fixture
def healthy_vehicle() -> VehicleSimulator:
    """The same car with no stored faults."""
    return build_healthy_simulator()


@pytest.fixture
def ecm(vehicle: VehicleSimulator) -> Any:
    """The simulated engine control module."""
    return vehicle.get(0x7E0)


@pytest.fixture
def car(settings: Settings, vehicle: VehicleSimulator) -> Iterator[CarInterfaceService]:
    """A READ_ONLY car interface bound to the simulated vehicle."""
    service = CarInterfaceService(settings, factory=TransportFactory(settings, vehicle))
    yield service
    service.close()


@pytest.fixture
def write_car(write_settings: Settings, vehicle: VehicleSimulator) -> Iterator[CarInterfaceService]:
    """A write-enabled car interface bound to the simulated vehicle."""
    service = CarInterfaceService(write_settings, factory=TransportFactory(write_settings, vehicle))
    yield service
    service.close()


@pytest.fixture
def manuals_dir(tmp_path: Path) -> Path:
    """A small manual library with content worth retrieving."""
    directory = tmp_path / "manuals"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "fl2_engine.md").write_text(
        "# Turbocharger\n"
        "P0299 underboost. Inspect the variable geometry turbocharger actuator "
        "rod for seizure. Actuator vacuum should reach 0.6 bar at idle. Torque "
        "the clamp to 10 Nm.\n\n"
        "# Swirl Flaps\n"
        "P2015 indicates the intake manifold runner position sensor is out of "
        "range. The swirl flap linkage on the DW12 engine wears at the plastic "
        "bushes and is a known failure point.\n",
        encoding="utf-8",
    )
    (directory / "fl2_driveline.txt").write_text(
        "Haldex coupling: replace the pre-charge pump filter every 45000 km. "
        "Torque the housing bolts to 25 Nm.\n",
        encoding="utf-8",
    )
    return directory


def attach_log_capture(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    """Wire pytest's caplog into the ``majster_ai`` logger tree.

    The package sets ``propagate=False`` on its own root logger on purpose --
    so a host application that configures logging does not get every record
    twice -- which means caplog's handler on the *root* logger never sees them.

    Call this **after** ``configure_logging(..., force=True)``: that call
    deliberately clears existing handlers, and would otherwise remove the one
    attached here.
    """
    import logging

    logger = logging.getLogger("majster_ai")
    if caplog.handler not in logger.handlers:
        logger.addHandler(caplog.handler)
    logger.setLevel(logging.DEBUG)
    return caplog
