"""Data coordinator for Hero calendar events."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import HeroApiError, HeroClient
from .const import DOMAIN, SCAN_INTERVAL

_LOGGER = logging.getLogger(__name__)


@dataclass
class HeroCalendarData:
    """Cached Hero calendar data."""

    name: str
    calendar_id: str
    events: list[dict[str, Any]]
    range_start: datetime
    range_end: datetime


class HeroCalendarCoordinator(DataUpdateCoordinator[HeroCalendarData]):
    """Fetch and cache Hero calendar events."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: HeroClient) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=SCAN_INTERVAL,
        )
        self.client = client

    async def _async_update_data(self) -> HeroCalendarData:
        """Fetch a rolling year of calendar events every six hours."""
        try:
            name, calendar_id = await self.client.async_get_school_calendar_id()
            now = dt_util.now()
            start = now - timedelta(days=7)
            end = now + timedelta(days=365)
            events = await self.client.async_get_google_events(calendar_id, start, end)
        except HeroApiError as err:
            raise UpdateFailed(str(err)) from err

        return HeroCalendarData(name=name, calendar_id=calendar_id, events=events, range_start=start, range_end=end)

