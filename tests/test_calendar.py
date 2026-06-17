"""Calendar filtering regressions without a Home Assistant runtime."""
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import pytest


@pytest.fixture
def calendar():
    root = Path(__file__).parents[1] / 'custom_components' / 'hero'
    names = ('homeassistant', 'homeassistant.components',
             'homeassistant.components.calendar', 'homeassistant.config_entries',
             'homeassistant.core', 'homeassistant.helpers',
             'homeassistant.helpers.entity_platform',
             'homeassistant.helpers.update_coordinator', 'homeassistant.util',
             'homeassistant.util.dt', 'hero_calendar_test',
             'hero_calendar_test.coordinator')
    modules = {name: ModuleType(name) for name in names}

    @dataclass
    class Event:
        summary: str
        start: object
        end: object
        description: object = None
        location: object = None
        uid: object = None

    class CoordinatorEntity:
        def __class_getitem__(cls, item):
            return cls

        def __init__(self, coordinator):
            self.coordinator = coordinator

    modules['homeassistant.components.calendar'].CalendarEntity = type('CalendarEntity', (), {})
    modules['homeassistant.components.calendar'].CalendarEvent = Event
    modules['homeassistant.config_entries'].ConfigEntry = object
    modules['homeassistant.core'].HomeAssistant = object
    modules['homeassistant.helpers.entity_platform'].AddEntitiesCallback = object
    modules['homeassistant.helpers.update_coordinator'].CoordinatorEntity = CoordinatorEntity
    modules['hero_calendar_test.coordinator'].HeroCalendarCoordinator = object
    modules['hero_calendar_test'].__path__ = [str(root)]
    dt = modules['homeassistant.util.dt']
    dt.now = lambda: datetime(2026, 9, 1, tzinfo=timezone.utc)
    dt.as_local = lambda value: value.replace(tzinfo=timezone.utc)
    dt.parse_datetime = datetime.fromisoformat
    with patch.dict(sys.modules, modules):
        spec = importlib.util.spec_from_file_location('hero_calendar_test.calendar', root / 'calendar.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        yield module


@pytest.mark.parametrize(('words', 'hide_week', 'expected'), [
    (' Senior, junior, , ', True, ['Seniors Camp', 'Assembly', 'C++ Club']),
    ('', True, ['Senior Camp', 'JUNIOR sports', 'Seniors Camp', 'Assembly', 'C++ Club']),
    ('Senior', False, ['JUNIOR sports', 'Seniors Camp', 'Assembly', 'C++ Club', 'Week 2']),
    ('C++', True, ['Senior Camp', 'JUNIOR sports', 'Seniors Camp', 'Assembly']),
])
def test_filters_calendar_and_next_event(calendar, words, hide_week, expected):
    titles = ['Senior Camp', 'JUNIOR sports', 'Seniors Camp', 'Assembly', 'C++ Club', 'Week 2']
    items = [dict(summary=title, start={'date': f'2026-09-{day:02}'},
                  end={'date': f'2026-09-{day + 1:02}'})
             for day, title in enumerate(titles, 2)]
    coordinator = SimpleNamespace(
        config_entry=SimpleNamespace(entry_id='test', options={
            'excluded_words': words, 'hide_week_events': hide_week}),
        data=SimpleNamespace(events=items),
    )
    entity = calendar.HeroCalendarEntity(coordinator)
    events = asyncio.run(entity.async_get_events(None,
        datetime(2026, 9, 1, tzinfo=timezone.utc),
        datetime(2026, 10, 1, tzinfo=timezone.utc)))
    assert [event.summary for event in events] == expected
    assert entity.event.summary == expected[0]
    assert [item['summary'] for item in items] == titles
    coordinator.config_entry.options = {'excluded_words': ','.join(titles)}
    assert entity.event is None
