"""Tests for E-REDES sensor entities."""

from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

from homeassistant.components.sensor import SensorDeviceClass

from custom_components.eredes.const import SENSOR_LAST_REAL_DATA_DAY
from custom_components.eredes.coordinator import ERedesCoordinatorData
from custom_components.eredes.sensor import async_setup_entry

CPE = "PT0002000012345678AB"


async def test_last_real_data_day_sensor_reports_latest_reliable_day() -> None:
    """The date sensor exposes the latest day backed by real meter indexes."""
    coordinator = MagicMock()
    coordinator.cpe = CPE
    coordinator.data = ERedesCoordinatorData(
        consumption=None,
        today_kwh=0.0,
        current_power_w=0.0,
        last_reading=None,
        last_update=datetime(2026, 8, 11, 9, 0, tzinfo=UTC),
        last_real_data_day=date(2026, 8, 7),
    )
    entry = SimpleNamespace(
        runtime_data=SimpleNamespace(coordinator=coordinator),
    )
    entities = []

    def add_entities(new) -> None:
        entities.extend(new)

    await async_setup_entry(MagicMock(), entry, add_entities)

    sensor = next(
        entity
        for entity in entities
        if entity.entity_description.key == SENSOR_LAST_REAL_DATA_DAY
    )
    assert sensor.device_class == SensorDeviceClass.DATE
    assert sensor.native_value == date(2026, 8, 7)
    assert sensor.unique_id == f"{CPE}_{SENSOR_LAST_REAL_DATA_DAY}"
