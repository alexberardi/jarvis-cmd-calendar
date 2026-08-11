"""Tests for the add_event WRITE command + its proposable action.

Mirrors the mocking approach in test_calendar_user_scope.py:
- _stub_log_client() so importing the command never reaches the real log client
- _install_get_calendar_events_shared() stubs the shared service/date modules
- a fake ICloudCalendarService recording add_event() calls is installed onto the
  stub module BEFORE the command is path-loaded (the command binds the class at
  import time via `from ... import ICloudCalendarService`)
- a dict-backed StorageBackend (real save/get) so the idempotency guard can be
  exercised; secrets keyed on (key, scope, user_id)
- SDK user ContextVar (set_current_user_id) + SimpleNamespace request_info

Coverage:
(a) create_event happy path → calls iCloud add_event with the mapped event_data
    keys (summary/start_time/end_time/location) and returns success.
(b) idempotency → a second create_event with the same idempotency_key does NOT
    call add_event again and still returns success.
(c) get_proposable_actions() exposes {"create_event": ...} and validates cleanly.
(d) unknown-speaker refusal when no user id is resolvable.
"""

import importlib.util
import os
import sys
import types
from datetime import datetime
from types import SimpleNamespace

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, ".."))


def _stub_log_client() -> None:
    if "jarvis_log_client" in sys.modules:
        return
    stub = types.ModuleType("jarvis_log_client")

    class _Logger:
        def __init__(self, **kwargs): ...
        def info(self, *args, **kwargs): ...
        def warning(self, *args, **kwargs): ...
        def error(self, *args, **kwargs): ...
        def debug(self, *args, **kwargs): ...

    stub.JarvisLogger = _Logger
    sys.modules["jarvis_log_client"] = stub


def _load_real(name: str, *parts: str):
    path = os.path.join(_ROOT, *parts)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _install_get_calendar_events_shared() -> None:
    """Stub the shared service/date submodules (idempotent — skips when the
    package is already present, matching the sibling test files)."""
    _stub_log_client()
    if "get_calendar_events_shared" not in sys.modules:
        pkg = types.ModuleType("get_calendar_events_shared")
        sys.modules["get_calendar_events_shared"] = pkg

        icloud = types.ModuleType("get_calendar_events_shared.icloud_calendar_service")
        icloud.ICloudCalendarService = type("ICloudCalendarService", (), {})
        sys.modules["get_calendar_events_shared.icloud_calendar_service"] = icloud

        gcal = types.ModuleType("get_calendar_events_shared.google_calendar_service")
        gcal.GoogleCalendarService = type("GoogleCalendarService", (), {})
        sys.modules["get_calendar_events_shared.google_calendar_service"] = gcal

        du = types.ModuleType("get_calendar_events_shared.date_util")
        du.parse_date_array = lambda keys: [datetime.now() for _ in keys]
        du.format_date_display = lambda *a, **k: "today"
        du.dates_to_strings = lambda dates: []
        sys.modules["get_calendar_events_shared.date_util"] = du


class _FakeICloudService:
    """Records add_event() calls; returns a configurable result."""

    instances: list = []

    def __init__(self, username, password, calendar_name="Home"):
        self.username = username
        self.password = password
        self.calendar_name = calendar_name
        self.added: list = []
        self.add_event_return = True
        _FakeICloudService.instances.append(self)

    def add_event(self, event_data):
        self.added.append(event_data)
        return self.add_event_return


class _FakeBackend:
    """SDK StorageBackend with real data persistence + secret store.

    Data keyed on (command_name, data_key); secrets on (key, scope, user_id).
    """

    def __init__(self) -> None:
        self.data: dict = {}
        self.secrets: dict = {}

    def save(self, command_name, data_key, data, expires_at=None) -> None:
        self.data[(command_name, data_key)] = data

    def get(self, command_name, data_key):
        return self.data.get((command_name, data_key))

    def get_all(self, command_name):
        return {k[1]: v for k, v in self.data.items() if k[0] == command_name}

    def delete(self, command_name, data_key) -> bool:
        return self.data.pop((command_name, data_key), None) is not None

    def delete_all(self, command_name) -> int:
        keys = [k for k in self.data if k[0] == command_name]
        for k in keys:
            del self.data[k]
        return len(keys)

    def get_secret(self, key, scope, user_id=None):
        return self.secrets.get((key, scope, user_id))

    def set_secret(self, key, value, scope, value_type="string", user_id=None) -> None:
        self.secrets[(key, scope, user_id)] = value

    def delete_secret(self, key, scope, user_id=None) -> None:
        self.secrets.pop((key, scope, user_id), None)


@pytest.fixture
def backend():
    from jarvis_command_sdk import set_backend, set_current_user_id

    b = _FakeBackend()
    set_backend(b)
    yield b
    set_current_user_id(None)


def _load_add_event_command():
    """Install the fake iCloud service, then path-load the command module.

    The stub-module attribute is restored afterwards so sibling test files that
    reuse the shared package see the placeholder they expect. The command module
    keeps its own captured reference to the fake (bound at import time)."""
    _install_get_calendar_events_shared()
    icloud_mod = sys.modules["get_calendar_events_shared.icloud_calendar_service"]
    original = getattr(icloud_mod, "ICloudCalendarService", None)
    icloud_mod.ICloudCalendarService = _FakeICloudService
    _FakeICloudService.instances.clear()
    try:
        return _load_real("add_event_test_cmd", "commands", "add_event", "command.py")
    finally:
        if original is not None:
            icloud_mod.ICloudCalendarService = original


def _configure_icloud(backend, uid: int) -> None:
    backend.set_secret("CALENDAR_USERNAME", "a@icloud.com", "user", user_id=uid)
    backend.set_secret("CALENDAR_PASSWORD", "xxxx-xxxx-xxxx-xxxx", "user", user_id=uid)


# ---------------------------------------------------------------------------
# (a) create_event happy path
# ---------------------------------------------------------------------------


def test_create_event_writes_to_icloud_with_mapped_keys(backend):
    from jarvis_command_sdk import set_current_user_id

    cmd_mod = _load_add_event_command()
    cmd = cmd_mod.AddEventCommand()
    _configure_icloud(backend, 1)

    request_info = SimpleNamespace(voice_command="add dentist", user_id=1)
    data = {
        "title": "Dentist",
        "start": "2026-08-10T15:00:00",
        "end": "2026-08-10T16:00:00",
        "location": "123 Main St",
        "idempotency_key": "evt-1",
    }

    set_current_user_id(1)
    try:
        response = cmd.create_event(data, request_info)
    finally:
        set_current_user_id(None)

    assert response.success is True
    assert response.context_data["added"] is True

    assert len(_FakeICloudService.instances) == 1
    svc = _FakeICloudService.instances[-1]
    assert len(svc.added) == 1
    event_data = svc.added[0]
    # Mapped to the iCloud service's expected keys — NOT the LLM param names.
    assert event_data["summary"] == "Dentist"
    assert isinstance(event_data["start_time"], datetime)
    assert isinstance(event_data["end_time"], datetime)
    assert event_data["start_time"] == datetime(2026, 8, 10, 15, 0, 0)
    assert event_data["end_time"] == datetime(2026, 8, 10, 16, 0, 0)
    assert event_data["location"] == "123 Main St"
    assert "title" not in event_data  # proves the mapping happened


def test_end_defaults_to_one_hour_when_equal_or_missing(backend):
    """A missing end — or one that echoes start (the extractor's zero-length
    quirk) — becomes a 1-hour event, like a normal calendar default."""
    from datetime import timedelta
    from jarvis_command_sdk import set_current_user_id

    cmd_mod = _load_add_event_command()
    cmd = cmd_mod.AddEventCommand()
    _configure_icloud(backend, 1)
    request_info = SimpleNamespace(voice_command="add dentist", user_id=1)

    def _end_time_for(end_value, key):
        data = {"title": "Dentist", "start": "2026-08-11T18:00:00", "idempotency_key": key}
        if end_value is not None:
            data["end"] = end_value
        set_current_user_id(1)
        try:
            cmd.create_event(data, request_info)
        finally:
            set_current_user_id(None)
        return _FakeICloudService.instances[-1].added[-1]["end_time"]

    expected = datetime(2026, 8, 11, 18, 0, 0) + timedelta(hours=1)
    assert _end_time_for("2026-08-11T18:00:00", "eq-1") == expected  # end == start
    assert _end_time_for(None, "miss-1") == expected  # end omitted


# ---------------------------------------------------------------------------
# (b) idempotency
# ---------------------------------------------------------------------------


def test_create_event_is_idempotent_on_repeat_key(backend):
    from jarvis_command_sdk import set_current_user_id

    cmd_mod = _load_add_event_command()
    cmd = cmd_mod.AddEventCommand()
    _configure_icloud(backend, 1)

    request_info = SimpleNamespace(voice_command="add dentist", user_id=1)
    data = {
        "title": "Dentist",
        "start": "2026-08-10T15:00:00",
        "end": "2026-08-10T16:00:00",
        "idempotency_key": "same-key",
    }

    set_current_user_id(1)
    try:
        first = cmd.create_event(dict(data), request_info)
        second = cmd.create_event(dict(data), request_info)
    finally:
        set_current_user_id(None)

    assert first.success is True
    assert second.success is True
    # The write happened exactly once; the second call short-circuited on the
    # stored idempotency record and never touched the calendar service.
    assert len(_FakeICloudService.instances) == 1
    assert len(_FakeICloudService.instances[0].added) == 1


# ---------------------------------------------------------------------------
# (c) proposable action declaration
# ---------------------------------------------------------------------------


def test_get_proposable_actions_exposes_create_event(backend):
    cmd_mod = _load_add_event_command()
    cmd = cmd_mod.AddEventCommand()

    actions = cmd.get_proposable_actions()  # validates: raises if misdeclared

    assert set(actions.keys()) == {"create_event"}
    action = actions["create_event"]
    assert action.callback == "create_event"
    assert action.card_title == "Add to your calendar?"
    assert action.confirm_label == "Add"
    assert action.idempotency_param == "idempotency_key"
    assert set(action.editable) == {"title", "start", "end", "location"}
    param_names = {p.name for p in action.params}
    assert {"title", "start", "end", "location", "idempotency_key"} <= param_names


def test_listening_signal_types_advertises_appt_detected(backend):
    cmd_mod = _load_add_event_command()
    cmd = cmd_mod.AddEventCommand()

    # The command declares it's designed for detected-appointment Signals, and it
    # rides the standard advertisement (get_command_schema) so the situation
    # matcher can group it as purpose-built for "appt.detected".
    assert cmd.listening_signal_types == ["appt.detected"]
    assert cmd.get_command_schema()["listening_signal_types"] == ["appt.detected"]


# ---------------------------------------------------------------------------
# (d) unknown-speaker refusal
# ---------------------------------------------------------------------------


def test_create_event_refuses_unknown_speaker(backend):
    cmd_mod = _load_add_event_command()
    cmd = cmd_mod.AddEventCommand()

    request_info = SimpleNamespace(voice_command="add dentist", user_id=None)
    data = {
        "title": "Dentist",
        "start": "2026-08-10T15:00:00",
        "idempotency_key": "evt-x",
    }

    response = cmd.create_event(data, request_info)

    assert not response.success
    assert response.context_data["error"] == "unknown_speaker"
    # Never constructed a calendar service — refused before any write.
    assert _FakeICloudService.instances == []
