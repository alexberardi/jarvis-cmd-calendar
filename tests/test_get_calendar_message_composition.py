"""Verify ReadCalendarCommand.run() composes a spoken `message` on the pre-route fast path."""

import importlib.util
import os
import sys
import types

import pytest


def _stub_get_calendar_events_shared() -> None:
    if "get_calendar_events_shared" in sys.modules:
        return
    pkg = types.ModuleType("get_calendar_events_shared")
    sys.modules["get_calendar_events_shared"] = pkg

    icloud = types.ModuleType("get_calendar_events_shared.icloud_calendar_service")
    icloud.ICloudCalendarService = type("ICloudCalendarService", (), {})
    sys.modules["get_calendar_events_shared.icloud_calendar_service"] = icloud

    gcal = types.ModuleType("get_calendar_events_shared.google_calendar_service")
    gcal.GoogleCalendarService = type("GoogleCalendarService", (), {})
    sys.modules["get_calendar_events_shared.google_calendar_service"] = gcal

    du = types.ModuleType("get_calendar_events_shared.date_util")
    du.parse_date_array = lambda *a, **k: None
    du.format_date_display = lambda *a, **k: "today"
    du.dates_to_strings = lambda *a, **k: []
    sys.modules["get_calendar_events_shared.date_util"] = du


def _load_command():
    _stub_get_calendar_events_shared()
    here = os.path.dirname(os.path.abspath(__file__))
    cmd_path = os.path.join(here, "..", "commands", "get_calendar_events", "command.py")
    spec = importlib.util.spec_from_file_location("cal_msg_test", cmd_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def cmd_module():
    return _load_command()


def test_compose_no_events(cmd_module):
    msg = cmd_module._compose_calendar_message([], "today")
    assert "nothing" in msg.lower() or "no" in msg.lower()
    assert "today" in msg.lower()


def test_compose_one_event(cmd_module):
    events = [{"summary": "Dentist appointment", "start_time": "14:00", "is_all_day": False}]
    msg = cmd_module._compose_calendar_message(events, "tomorrow")
    assert "Dentist" in msg
    assert "1 event" in msg
    assert "14:00" in msg


def test_compose_all_day_event(cmd_module):
    events = [{"summary": "Conference", "start_time": "All day", "is_all_day": True}]
    msg = cmd_module._compose_calendar_message(events, "Friday")
    assert "all day" in msg.lower()
    assert "Conference" in msg


def test_compose_many_events(cmd_module):
    events = [
        {"summary": f"Event {i}", "start_time": f"{i + 9}:00", "is_all_day": False}
        for i in range(6)
    ]
    msg = cmd_module._compose_calendar_message(events, "today")
    assert "6 events" in msg
    assert "3 more" in msg
