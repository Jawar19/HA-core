"""Test the Rejseplanen sensor."""

from datetime import date, datetime, time, timedelta
import logging
from unittest.mock import AsyncMock, Mock, patch
import zoneinfo

from py_rejseplan.exceptions import (
    APIError,
    ConnectionError as RejseplanenConnectionError,
)
import pytest
from syrupy.assertion import SnapshotAssertion

from homeassistant.components.rejseplanen import sensor as rejseplanen_sensor
from homeassistant.components.rejseplanen.const import CONF_API_KEY, DOMAIN
from homeassistant.components.rejseplanen.sensor import (
    _get_current_departures,
    _get_delay_minutes,
    _get_departure_timestamp,
    _get_next_departure_cleanup_time,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er, issue_registry as ir

from .conftest import make_mock_departures

from tests.common import MockConfigEntry, snapshot_platform

LOGGER = logging.getLogger(__name__)

TZ_CPH = zoneinfo.ZoneInfo("Europe/Copenhagen")


@pytest.fixture
def fixed_now():
    """Fixture for a fixed datetime."""
    return datetime(2024, 1, 1, 12, 0, 0, tzinfo=zoneinfo.ZoneInfo("Europe/Copenhagen"))


async def test_sensor_snapshot(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    mock_config_entry: MockConfigEntry,
    mock_api: AsyncMock,
    snapshot: SnapshotAssertion,
) -> None:
    """Snapshot test of the sensors."""
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    assert mock_config_entry.state is ConfigEntryState.LOADED

    await snapshot_platform(hass, entity_registry, snapshot, mock_config_entry.entry_id)


@pytest.mark.parametrize("stop_id", [123456, 456789, 999999])
def test_get_current_departures(stop_id: int, patch_sensor_now) -> None:
    """Test the _get_current_departures helper function."""

    departures = make_mock_departures(stop_id)
    result = _get_current_departures(
        departures,
        TZ_CPH,
    )
    assert len(result) == len(departures)
    for dep in departures:
        assert dep in result


def test_get_next_departure_cleanup_time_with_mock_api(
    patch_sensor_now, mock_api
) -> None:
    """Test _get_next_departure_cleanup_time using the mock_api for departures."""

    departures = mock_api.get_filtered_departures(123456)

    LOGGER.debug(
        "Testing _get_next_departure_cleanup_time with departures from mock_api"
    )

    dep_time = datetime.combine(
        departures[0].rtDate,
        departures[0].rtTime,
        tzinfo=zoneinfo.ZoneInfo("Europe/Copenhagen"),
    )
    dep_time += timedelta(seconds=15)

    LOGGER.debug("Combined departure time: %s", dep_time)

    cleanup_time = _get_next_departure_cleanup_time(departures, TZ_CPH)
    assert cleanup_time is not None
    assert cleanup_time == dep_time

    LOGGER.debug("Next cleanup time from mock_api: %s", cleanup_time)
    LOGGER.debug("Departures: %s", departures)

    # Use mock_api to get departures for unknown stop_id (should be None)
    departures = mock_api.get_filtered_departures(999999)
    cleanup_time = _get_next_departure_cleanup_time(departures, TZ_CPH)
    assert cleanup_time is None


def test_get_departure_timestamp_with_mock_api(patch_sensor_now, mock_api) -> None:
    """Test the _get_departure_timestamp helper function using mock_api departures."""

    # Test with empty list - should return None
    result = _get_departure_timestamp(None, TZ_CPH)
    assert result is None

    # Test with index out of bounds - should return None
    departures = mock_api.get_filtered_departures(123456)

    # Test with valid departure at index 0 - should return realtime timestamp (since mock sets rtTime)
    result = _get_departure_timestamp(departures[0], TZ_CPH)
    assert result is not None
    # Should match the mock's rtDate/rtTime
    expected = datetime.combine(departures[0].rtDate, departures[0].rtTime).replace(
        tzinfo=zoneinfo.ZoneInfo("Europe/Copenhagen")
    )
    assert result == expected

    # Test with valid departure at index 1
    if len(departures) > 1:
        result = _get_departure_timestamp(departures[1], TZ_CPH)
        assert result is not None
        expected = datetime.combine(departures[1].rtDate, departures[1].rtTime).replace(
            tzinfo=zoneinfo.ZoneInfo("Europe/Copenhagen")
        )
        assert result == expected


def test_get_delay_minutes_none_departure() -> None:
    """Test _get_delay_minutes returns None when departure is None."""
    assert _get_delay_minutes(None, TZ_CPH) is None


def test_get_delay_minutes_with_delay(patch_sensor_now, mock_api) -> None:
    """Test _get_delay_minutes returns correct delay."""
    departures = mock_api.get_filtered_departures(123456)
    departure = departures[0]
    result = _get_delay_minutes(departure, TZ_CPH)
    # rtTime is 2 minutes after time in the mock
    assert result == 2


def test_get_delay_minutes_no_delay(patch_sensor_now, mock_api) -> None:
    """Test _get_delay_minutes returns 0 when no delay."""
    departure = Mock()
    base = datetime(2024, 1, 1, 12, 5, 0)
    departure.date = base.date()
    departure.time = base.time()
    departure.rtDate = base.date()
    departure.rtTime = base.time()
    result = _get_delay_minutes(departure, TZ_CPH)
    assert result == 0


async def test_coordinator_update_failed_api_error(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: Mock,
) -> None:
    """Test coordinator raises UpdateFailed on APIError."""
    mock_api.get_departures.side_effect = APIError("API error")
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_coordinator_update_failed_connection_error(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: Mock,
) -> None:
    """Test coordinator raises UpdateFailed on ConnectionError."""
    mock_api.get_departures.side_effect = RejseplanenConnectionError("network error")
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_coordinator_no_stops(
    hass: HomeAssistant,
    mock_api: Mock,
) -> None:
    """Test coordinator returns empty board when no stops are configured."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=DOMAIN,
        data={CONF_API_KEY: "test-key"},
        entry_id="no-stops-entry",
        subentries_data=[],
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    mock_api.get_departures.assert_not_called()


async def test_yaml_deprecated_setup_platform(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: Mock,
) -> None:
    """Test async_setup_platform creates a deprecation issue."""
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    await rejseplanen_sensor.async_setup_platform(hass, {}, Mock())
    await hass.async_block_till_done()

    issue_registry = ir.async_get(hass)
    assert issue_registry.async_get_issue("rejseplanen", "yaml_deprecated") is not None


async def test_departure_cleanup_callback(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: Mock,
    patch_sensor_now: None,
) -> None:
    """Test that departure cleanup is scheduled and fires correctly."""

    captured_callbacks: list = []

    def mock_track(hass, action, point_in_time):
        captured_callbacks.append(action)
        return lambda: None  # return a no-op unsubscribe

    with patch(
        "homeassistant.components.rejseplanen.sensor.async_track_point_in_time",
        side_effect=mock_track,
    ):
        mock_config_entry.add_to_hass(hass)
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

        # At least one cleanup should have been scheduled (one per sensor)
        assert len(captured_callbacks) > 0

        # Fire the first captured cleanup callback directly — should not raise
        await hass.async_add_executor_job(lambda: None)  # flush executor
        captured_callbacks[0](datetime(2024, 1, 1, 12, 7, 15, tzinfo=TZ_CPH))
        await hass.async_block_till_done()


async def test_coordinator_type_error(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: Mock,
) -> None:
    """Test coordinator raises UpdateFailed on TypeError."""
    mock_api.get_departures.side_effect = TypeError("bad type")
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_coordinator_get_filtered_departures_no_data(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: Mock,
) -> None:
    """Test get_filtered_departures returns empty list when coordinator has no data."""
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    coordinator = mock_config_entry.runtime_data
    coordinator.data = None
    result = coordinator.get_filtered_departures(stop_id=123456)
    assert result == []


async def test_coordinator_departure_type_filter(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: Mock,
) -> None:
    """Test get_filtered_departures filters by departure type bitflag."""
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    coordinator = mock_config_entry.runtime_data
    mock_board = Mock()
    dep = Mock()
    dep.stopExtId = 123456
    dep.direction = "End Point St."
    dep.product = Mock()
    dep.product.cls_id = 32  # BUS bitflag
    # Required for sorting and filtering in get_filtered_departures
    dep.date = date(2099, 1, 1)
    dep.time = time(12, 0, 0)
    dep.rtDate = date(2099, 1, 1)
    dep.rtTime = time(12, 0, 0)
    mock_board.departures = [dep]
    coordinator.data = mock_board

    # Filter for BUS (32) — should match
    result = coordinator.get_filtered_departures(
        stop_id=123456, departure_type_filter=32
    )
    assert dep in result

    # Filter for METRO (1024) — should not match
    result = coordinator.get_filtered_departures(
        stop_id=123456, departure_type_filter=1024
    )
    assert result == []


def test_get_delay_minutes_no_realtime(patch_sensor_now) -> None:
    """Test _get_delay_minutes returns None when no realtime date/time available."""
    departure = Mock()
    base = datetime(2024, 1, 1, 12, 5, 0)
    departure.date = base.date()
    departure.time = base.time()
    departure.rtDate = None
    departure.rtTime = None
    # With no rtDate/rtTime, realtime == planned, so delay is 0
    result = _get_delay_minutes(departure, TZ_CPH)
    assert result == 0
