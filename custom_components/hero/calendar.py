"""Calendar platform for Hero."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any
import re

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .coordinator import HeroCalendarCoordinator
from .const import CONF_EXCLUDED_WORDS, CONF_HIDE_WEEK_EVENTS

_WEEK_MARKER = re.compile(r"week\s+\d+\b", re.IGNORECASE)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Set up the Hero calendar entity."""
    async_add_entities([HeroCalendarEntity(entry.runtime_data)])


class HeroCalendarEntity(CoordinatorEntity[HeroCalendarCoordinator], CalendarEntity):
    """Unified Hero calendar entity."""

    _attr_has_entity_name = True
    _attr_name = None

    def __init__(self, coordinator: HeroCalendarCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_calendar"

    @property
    def device_info(self) -> dict[str, Any]:
        """Return device info."""
        return {
            "identifiers": {("hero", self.coordinator.config_entry.entry_id)},
            "name": self.coordinator.data.name if self.coordinator.data else "Hero",
            "manufacturer": "Hero",
        }

    @property
    def event(self) -> CalendarEvent | None:
        """Return the next upcoming event."""
        now = dt_util.now()
        events = self._events_between(now, now + timedelta(days=365))
        return events[0] if events else None

    async def async_get_events(self, hass: HomeAssistant, start_date: datetime, end_date: datetime) -> list[CalendarEvent]:
        """Return calendar events in the requested range."""
        return self._events_between(start_date, end_date)

    def _events_between(self, start_date: datetime, end_date: datetime) -> list[CalendarEvent]:
        if not self.coordinator.data:
            return []

        hide_week_events = self.coordinator.config_entry.options.get(CONF_HIDE_WEEK_EVENTS, True)
        excluded_words = [
            word.strip()
            for word in self.coordinator.config_entry.options.get(CONF_EXCLUDED_WORDS, "").split(",")
            if word.strip()
        ]
        excluded_pattern = (
            re.compile(r"(?<!\w)(?:" + "|".join(re.escape(word) for word in excluded_words) + r")(?!\w)", re.IGNORECASE)
            if excluded_words else None
        )
        events = [
            _to_calendar_event(item)
            for item in self.coordinator.data.events
            if not (hide_week_events and _WEEK_MARKER.match(item.get("summary", "").strip()))
            and not (excluded_pattern and excluded_pattern.search(item.get("summary", "")))
        ]
        return sorted(
            (event for event in events if _event_overlaps(event, start_date, end_date)),
            key=lambda event: _sort_value(event.start),
        )


def _to_calendar_event(item: dict[str, Any]) -> CalendarEvent:
    start = _google_time(item.get("start", {}), is_end=False)
    end = _google_time(item.get("end", {}), is_end=True)
    return CalendarEvent(
        summary=item.get("summary", "Hero event"),
        start=start,
        end=end,
        description=item.get("description"),
        location=item.get("location"),
        uid=item.get("iCalUID") or item.get("id"),
    )


def _google_time(value: dict[str, str], is_end: bool) -> datetime | date:
    if "dateTime" in value:
        return dt_util.parse_datetime(value["dateTime"]) or datetime.fromisoformat(value["dateTime"])
    if "date" in value:
        return date.fromisoformat(value["date"])
    fallback = dt_util.now()
    return fallback if not is_end else fallback.replace(hour=23, minute=59, second=59)


def _event_overlaps(event: CalendarEvent, start_date: datetime, end_date: datetime) -> bool:
    event_start = _as_datetime(event.start)
    event_end = _as_datetime(event.end)
    return event_start < end_date and event_end > start_date


def _as_datetime(value: datetime | date) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else dt_util.as_local(value)
    return dt_util.as_local(datetime.combine(value, time.min))


def _sort_value(value: datetime | date) -> datetime:
    return _as_datetime(value)
