"""calendar_alerts → appt.upcoming Signal (leave-by producer) + start_iso tz-attach.

Covers the calendar side of the leave-by signal re-architecture:
- get_calendar_events._event_iso attaches the node's LOCAL tz to the naive-local
  wall-clock both providers return, so start_iso is an absolute instant (else
  reminder.set_at, which reads it as UTC, fires hours off).
- calendar_alerts emits appt.upcoming only for located, timed, in-lead-window
  events, with the exact facts/source_key/scope the CC bridge consumes, and
  dedups per (uid, event).
"""
import importlib.util
import os
import sys
import types
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, ".."))


def _stub_log_client() -> None:
    if "jarvis_log_client" in sys.modules:
        return
    stub = types.ModuleType("jarvis_log_client")

    class _Logger:
        def __init__(self, **kwargs): ...
        def info(self, *a, **k): ...
        def warning(self, *a, **k): ...
        def error(self, *a, **k): ...
        def debug(self, *a, **k): ...

    stub.JarvisLogger = _Logger
    sys.modules["jarvis_log_client"] = stub


def _load_real(name: str, *parts: str):
    path = os.path.join(_ROOT, *parts)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _install_shared() -> None:
    """Stub the get_calendar_events_shared service/date/availability submodules so
    the command + agent modules import off a node.

    NOTE: user_resolution is loaded REAL (not stubbed) — a stub would poison the
    shared sys.modules for sibling test files that assert on the real resolver.
    """
    _stub_log_client()
    if "get_calendar_events_shared" not in sys.modules:
        sys.modules["get_calendar_events_shared"] = types.ModuleType(
            "get_calendar_events_shared"
        )
    for sub, attrs in {
        "icloud_calendar_service": {"ICloudCalendarService": type("ICloudCalendarService", (), {})},
        "google_calendar_service": {"GoogleCalendarService": type("GoogleCalendarService", (), {})},
        "date_util": {
            "parse_date_array": lambda keys: [datetime.now() for _ in keys],
            "format_date_display": lambda *a, **k: "today",
            "dates_to_strings": lambda dates: [],
        },
        "availability": {"build_availability": lambda *a, **k: {}},
    }.items():
        mod_name = f"get_calendar_events_shared.{sub}"
        if mod_name not in sys.modules:
            m = types.ModuleType(mod_name)
            for k, v in attrs.items():
                setattr(m, k, v)
            sys.modules[mod_name] = m
    # google_calendar_service imports CalendarEvent from the icloud stub — ensure it
    # exists even if a sibling test installed the icloud stub without it.
    icloud = sys.modules["get_calendar_events_shared.icloud_calendar_service"]
    if not hasattr(icloud, "CalendarEvent"):
        icloud.CalendarEvent = type("CalendarEvent", (), {})
    if "get_calendar_events_shared.user_resolution" not in sys.modules:
        _load_real(
            "get_calendar_events_shared.user_resolution",
            "get_calendar_events_shared",
            "user_resolution.py",
        )


class _CapturingBackend:
    """SDK SignalsBackend that records the emit payloads."""

    def __init__(self) -> None:
        self.payloads: list = []

    def emit_signal(self, payload):
        self.payloads.append(payload)
        return "ok"


@pytest.fixture
def signals_backend():
    from jarvis_command_sdk.signals import set_signals_backend

    b = _CapturingBackend()
    set_signals_backend(b)
    yield b
    set_signals_backend(None)


@pytest.fixture
def agent_mod():
    _install_shared()
    return _load_real("appt_test_agent", "agents", "calendar_alerts", "agent.py")


@pytest.fixture
def command_mod():
    _install_shared()
    return _load_real("appt_test_command", "commands", "get_calendar_events", "command.py")


@pytest.fixture
def google_mod():
    _install_shared()
    return _load_real(
        "appt_test_google", "get_calendar_events_shared", "google_calendar_service.py"
    )


# ── Google datetime → node-local (the "Z"/UTC and cross-tz correctness fix) ──────
def test_google_utc_z_datetime_converted_to_node_local(google_mod, monkeypatch):
    # A UTC-encoded ("...Z") Google event must become the node's LOCAL wall-clock
    # (naive), NOT the raw UTC clock — else _event_iso stamps node tz on a UTC clock
    # and the leave-by reminder fires hours off.
    monkeypatch.setattr(google_mod, "_node_local_tz", lambda: ZoneInfo("America/New_York"))
    dt = google_mod.GoogleCalendarService._parse_google_datetime("2026-08-13T22:45:00Z")
    assert dt is not None and dt.tzinfo is None
    assert (dt.hour, dt.minute) == (18, 45)  # 22:45 UTC == 6:45 PM EDT


def test_google_offset_datetime_converted_to_node_local(google_mod, monkeypatch):
    # A cross-tz offset (-07:00 = PT) event on an ET node must convert to ET, not
    # keep the foreign wall-clock.
    monkeypatch.setattr(google_mod, "_node_local_tz", lambda: ZoneInfo("America/New_York"))
    dt = google_mod.GoogleCalendarService._parse_google_datetime("2026-08-13T15:00:00-07:00")
    assert dt is not None and dt.tzinfo is None
    assert (dt.hour, dt.minute) == (18, 0)  # 3 PM PT == 6 PM ET


# ── _event_iso tz-attach ────────────────────────────────────────────────────────
def test_event_iso_attaches_local_tz_to_naive(command_mod, monkeypatch):
    # Both providers hand us naive-local wall-clock; the helper must attach a tz so
    # the instant is absolute. Pin a known zone.
    monkeypatch.setattr(command_mod, "_node_local_tz", lambda: ZoneInfo("America/New_York"))
    naive = datetime(2026, 8, 13, 18, 45, 0)  # 6:45 PM local, no tzinfo
    out = command_mod._event_iso(naive, False)
    parsed = datetime.fromisoformat(out)
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() is not None  # a real offset was attached
    # 6:45 PM EDT == 22:45 UTC
    assert parsed.astimezone(timezone.utc).hour == 22


def test_event_iso_none_for_all_day(command_mod):
    assert command_mod._event_iso(datetime(2026, 8, 13, 0, 0), True) is None
    assert command_mod._event_iso(None, False) is None


def test_event_iso_preserves_aware_datetime(command_mod):
    aware = datetime(2026, 8, 13, 18, 45, tzinfo=timezone.utc)
    assert command_mod._event_iso(aware, False) == aware.isoformat()


# ── appt.upcoming emit ──────────────────────────────────────────────────────────
def _start(minutes_from_now: int) -> datetime:
    return datetime.now(timezone.utc) + timedelta(minutes=minutes_from_now)


def _event(minutes: int, *, location="123 Main St", all_day=False, eid="evt-1"):
    start = _start(minutes)
    return {
        "id": eid,
        "title": "Dentist",
        "location": location,
        "is_all_day": all_day,
        "start_iso": None if all_day else start.isoformat(),
    }, start


def test_emit_appt_upcoming_payload_shape(agent_mod, signals_backend):
    agent = agent_mod.CalendarAlertAgent()
    event, start = _event(90)
    agent._maybe_emit_appt_upcoming(event, start, 90.0, uid=7)

    assert len(signals_backend.payloads) == 1
    payload = signals_backend.payloads[0]
    sig = payload["signal"]
    assert sig["kind"] == "appt.upcoming"
    assert sig["source_key"] == "appt:7:evt-1"
    assert sig["cacheable"] is False
    assert sig["scope"] == {"user_id": 7}
    data = payload["data"]
    assert data["title"] == "Dentist"
    assert data["location"] == "123 Main St"
    assert data["event_id"] == "evt-1"
    assert data["start_iso"] == event["start_iso"]
    assert "start_display" in data


def test_no_emit_without_location(agent_mod, signals_backend):
    agent = agent_mod.CalendarAlertAgent()
    event, start = _event(30, location="")
    agent._maybe_emit_appt_upcoming(event, start, 30.0, uid=7)
    assert signals_backend.payloads == []


def test_no_emit_for_all_day(agent_mod, signals_backend):
    agent = agent_mod.CalendarAlertAgent()
    event, start = _event(30, all_day=True)
    agent._maybe_emit_appt_upcoming(event, start, 30.0, uid=7)
    assert signals_backend.payloads == []


def test_no_emit_beyond_lead_window(agent_mod, signals_backend):
    agent = agent_mod.CalendarAlertAgent()
    event, start = _event(200)  # > 120 min out
    agent._maybe_emit_appt_upcoming(event, start, 200.0, uid=7)
    assert signals_backend.payloads == []


def test_no_emit_for_past_event(agent_mod, signals_backend):
    agent = agent_mod.CalendarAlertAgent()
    event, start = _event(-10)
    agent._maybe_emit_appt_upcoming(event, start, -10.0, uid=7)
    assert signals_backend.payloads == []


def test_emit_dedups_same_event(agent_mod, signals_backend):
    agent = agent_mod.CalendarAlertAgent()
    event, start = _event(90)
    agent._maybe_emit_appt_upcoming(event, start, 90.0, uid=7)
    agent._maybe_emit_appt_upcoming(event, start, 88.0, uid=7)
    assert len(signals_backend.payloads) == 1  # second is a dedup no-op
