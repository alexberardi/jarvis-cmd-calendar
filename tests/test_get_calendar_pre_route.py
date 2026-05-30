"""Pre-route tests for the get_calendar_events command.

Mocks out calendar_shared since the pre-route doesn't need real CalDAV /
Google API clients; we only exercise the regex layer.
"""

import importlib.util
import os
import sys
import types

import pytest


def _stub_calendar_shared() -> None:
    if "calendar_shared" in sys.modules:
        return
    pkg = types.ModuleType("calendar_shared")
    sys.modules["calendar_shared"] = pkg

    icloud = types.ModuleType("calendar_shared.icloud_calendar_service")
    icloud.ICloudCalendarService = type("ICloudCalendarService", (), {})
    sys.modules["calendar_shared.icloud_calendar_service"] = icloud

    gcal = types.ModuleType("calendar_shared.google_calendar_service")
    gcal.GoogleCalendarService = type("GoogleCalendarService", (), {})
    sys.modules["calendar_shared.google_calendar_service"] = gcal

    du = types.ModuleType("calendar_shared.date_util")
    du.parse_date_array = lambda *a, **k: None
    du.format_date_display = lambda *a, **k: ""
    du.dates_to_strings = lambda *a, **k: []
    sys.modules["calendar_shared.date_util"] = du


def _load_command():
    _stub_calendar_shared()
    here = os.path.dirname(os.path.abspath(__file__))
    cmd_path = os.path.join(here, "..", "commands", "get_calendar_events", "command.py")
    spec = importlib.util.spec_from_file_location("cal_cmd_under_test", cmd_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.ReadCalendarCommand


@pytest.fixture
def cmd():
    return _load_command()()


class TestPreRouteWithDate:
    @pytest.mark.parametrize("phrase,date_key", [
        ("what's on my calendar today", "today"),
        ("what's on my calendar tomorrow", "tomorrow"),
        ("show me my schedule for tomorrow", "tomorrow"),
        ("what appointments do I have the day after tomorrow", "day_after_tomorrow"),
        ("show my calendar for this weekend", "this_weekend"),
        ("what meetings do I have next week", "next_week"),
    ])
    def test_with_date(self, cmd, phrase, date_key):
        result = cmd.pre_route(phrase)
        assert result is not None
        assert result.arguments == {"resolved_datetimes": [date_key]}


class TestPreRouteImplicitToday:
    @pytest.mark.parametrize("phrase", [
        "what's on my calendar",
        "what's on my schedule",
        "what's my schedule for today",
        "do I have any meetings",
        "do I have any appointments",
        "am I busy",
        "read my calendar",
        "check my calendar",
        "what are my plans",
    ])
    def test_implicit_today(self, cmd, phrase):
        result = cmd.pre_route(phrase)
        assert result is not None
        assert result.arguments == {"resolved_datetimes": ["today"]}


class TestPreRouteNoMatch:
    @pytest.mark.parametrize("phrase", [
        "tell me a joke",
        "remind me to check my calendar",
        "what time is it",
        "",
    ])
    def test_returns_none(self, cmd, phrase):
        assert cmd.pre_route(phrase) is None


class TestFastPathPatterns:
    def test_ids_stable(self, cmd):
        ids = {p.id for p in cmd.fast_path_patterns}
        assert ids == {
            "get_calendar_events.with_date",
            "get_calendar_events.implicit_today",
        }
