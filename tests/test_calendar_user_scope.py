"""Per-user calendar — agent identity resolution + unknown-speaker guard.

Calendar credentials are user-scoped; the background agent runs with no
ambient user in the SDK ContextVar. Coverage:

- find_configured_user_ids: per-provider usable checks against node-side
  user-scope secret rows (iCloud needs username+password, Google needs an
  access token, unsupported CALENDAR_TYPE excluded).
- resolve_agent_user_ids matrix: unset/blank → auto behavior (every
  calendar-configured user); set + configured calendar → exactly that uid;
  set + NO configured calendar → [] (an EXPLICIT identity must never
  silently fall back to other users); unparseable value → [].
- Agent run() iterates users with the SDK ContextVar set per user, clears
  it afterwards, attributes injected memories to the calendar's owner, and
  dedups alerts per user (two members can have same-named events).
- Command run() refuses with an "unknown_speaker" error when neither
  request_info.user_id nor the ContextVar identify the speaker, and passes
  the guard when the ContextVar alone is set.
"""

import asyncio
import importlib.util
import os
import sys
import types
from datetime import datetime, timedelta, timezone
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
    """Stub the service/date submodules (superset of the other test files'
    stubs, since they skip stubbing when "get_calendar_events_shared" already exists)
    and load the REAL user_resolution module."""
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

    # Another test file's stub may have installed the package without this
    # module — the real user_resolution must be loaded either way.
    if "get_calendar_events_shared.user_resolution" not in sys.modules:
        _load_real("get_calendar_events_shared.user_resolution", "get_calendar_events_shared", "user_resolution.py")


def _install_secret_service(rows: list, values: dict) -> None:
    """Stub the node-side services.secret_service the resolution module
    imports inside find_configured_user_ids.

    rows: objects with .key/.user_id (the user-scope table contents)
    values: {(key, user_id): value} for get_secret_value lookups
    """
    services_pkg = types.ModuleType("services")
    sys.modules["services"] = services_pkg
    ss = types.ModuleType("services.secret_service")
    ss.get_all_secrets = lambda scope, user_id=None: list(rows)
    ss.get_secret_value = lambda key, scope, user_id=None: values.get((key, user_id))
    sys.modules["services.secret_service"] = ss


def _remove_secret_service() -> None:
    sys.modules.pop("services.secret_service", None)
    sys.modules.pop("services", None)


class _FakeBackend:
    """Minimal SDK StorageBackend keyed on (key, scope, user_id)."""

    def __init__(self) -> None:
        self.secrets: dict = {}

    def save(self, command_name, data_key, data, expires_at=None) -> None: ...
    def get(self, command_name, data_key): return None
    def get_all(self, command_name): return {}
    def delete(self, command_name, data_key) -> bool: return False
    def delete_all(self, command_name) -> int: return 0

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
    _remove_secret_service()


@pytest.fixture
def user_resolution(backend):
    _install_get_calendar_events_shared()
    return sys.modules["get_calendar_events_shared.user_resolution"]


# ---------------------------------------------------------------------------
# find_configured_user_ids — per-provider usable checks
# ---------------------------------------------------------------------------


def _row(key: str, uid: int | None):
    return SimpleNamespace(key=key, user_id=uid)


def test_find_configured_icloud_needs_both_creds(user_resolution):
    _install_secret_service(
        rows=[_row("CALENDAR_USERNAME", 1), _row("CALENDAR_USERNAME", 2)],
        values={
            ("CALENDAR_USERNAME", 1): "a@icloud.com",
            ("CALENDAR_PASSWORD", 1): "xxxx-xxxx",
            ("CALENDAR_USERNAME", 2): "b@icloud.com",
            # user 2 has no password — not usable
        },
    )
    assert user_resolution.find_configured_user_ids() == [1]


def test_find_configured_google_needs_access_token(user_resolution):
    _install_secret_service(
        rows=[_row("CALENDAR_TYPE", 3), _row("CALENDAR_TYPE", 4)],
        values={
            ("CALENDAR_TYPE", 3): "google",
            ("GOOGLE_ACCESS_TOKEN", 3): "tok",
            ("CALENDAR_TYPE", 4): "google",
            # user 4 never finished OAuth — not usable
        },
    )
    assert user_resolution.find_configured_user_ids() == [3]


def test_find_configured_skips_unsupported_type_and_null_user(user_resolution):
    _install_secret_service(
        rows=[_row("CALENDAR_TYPE", 5), _row("CALENDAR_USERNAME", None)],
        values={
            ("CALENDAR_TYPE", 5): "caldav",  # not a supported provider
            ("CALENDAR_USERNAME", 5): "a@x.com",
            ("CALENDAR_PASSWORD", 5): "pw",
        },
    )
    assert user_resolution.find_configured_user_ids() == []


def test_find_configured_off_node_returns_empty(user_resolution):
    _remove_secret_service()
    assert user_resolution.find_configured_user_ids() == []


# ---------------------------------------------------------------------------
# resolve_agent_user_ids — CALENDAR_AGENT_USER identity matrix
# ---------------------------------------------------------------------------


def _set_identity(backend, value: str) -> None:
    backend.set_secret("CALENDAR_AGENT_USER", value, "integration")


def test_identity_unset_runs_all_configured(user_resolution, backend):
    assert user_resolution.resolve_agent_user_ids(lambda: [1, 2]) == [1, 2]


def test_identity_set_and_configured_pins_to_that_user(user_resolution, backend):
    _set_identity(backend, "2")
    assert user_resolution.resolve_agent_user_ids(lambda: [1, 2]) == [2]


def test_identity_set_but_unconfigured_idles(user_resolution, backend):
    _set_identity(backend, "7")
    assert user_resolution.resolve_agent_user_ids(lambda: [1, 2]) == []


def test_identity_unparseable_idles(user_resolution, backend):
    _set_identity(backend, "alex")
    assert user_resolution.resolve_agent_user_ids(lambda: [1, 2]) == []


def test_configured_agent_user_id(user_resolution, backend):
    assert user_resolution.configured_agent_user_id() is None
    _set_identity(backend, "3")
    assert user_resolution.configured_agent_user_id() == 3
    _set_identity(backend, "nope")
    assert user_resolution.configured_agent_user_id() is None


# ---------------------------------------------------------------------------
# Agent run() — per-user iteration with the SDK ContextVar
# ---------------------------------------------------------------------------


def _load_agent():
    _install_get_calendar_events_shared()
    return _load_real("cal_user_scope_test_agent", "agents", "calendar_alerts", "agent.py")


class _FakeCommand:
    """Records the ambient SDK user at run() time."""

    def __init__(self, events: list) -> None:
        self.events = events
        self.seen_ctx_uids: list = []
        self.seen_request_uids: list = []

    def run(self, request_info, **kwargs):
        from jarvis_command_sdk import get_current_user_id

        self.seen_ctx_uids.append(get_current_user_id())
        self.seen_request_uids.append(request_info.user_id)
        return SimpleNamespace(success=True, context_data={"events": self.events})


class _RecordingRestClient:
    calls: list = []

    @classmethod
    def inject_memories(cls, memories, household_id=None):
        cls.calls.append(memories)
        return {"injected": len(memories), "updated": 0}


def _install_rest_client() -> None:
    clients_pkg = types.ModuleType("clients")
    sys.modules["clients"] = clients_pkg
    rc = types.ModuleType("clients.rest_client")
    _RecordingRestClient.calls = []
    rc.RestClient = _RecordingRestClient
    sys.modules["clients.rest_client"] = rc


def _event(title: str, minutes_from_now: int) -> dict:
    start = datetime.now(timezone.utc) + timedelta(minutes=minutes_from_now)
    return {
        "id": f"{title}-id",
        "title": title,
        "start_time": start.isoformat(),
        "is_all_day": False,
    }


def test_agent_runs_per_user_with_contextvar(backend, monkeypatch):
    from jarvis_command_sdk import get_current_user_id

    agent_mod = _load_agent()
    _install_rest_client()
    monkeypatch.setattr(agent_mod, "resolve_agent_user_ids", lambda lookup=None: [1, 2])

    agent = agent_mod.CalendarAlertAgent()
    cmd1 = _FakeCommand([_event("Dentist", 10)])
    cmd2 = _FakeCommand([_event("Dentist", 10)])
    agent._cmds = {1: cmd1, 2: cmd2}

    asyncio.run(agent.run())

    # Each user's command ran exactly once with THEIR identity ambient.
    assert cmd1.seen_ctx_uids == [1] and cmd1.seen_request_uids == [1]
    assert cmd2.seen_ctx_uids == [2] and cmd2.seen_request_uids == [2]
    # ContextVar cleared after the loop.
    assert get_current_user_id() is None

    # Same-named events for different users both alert (per-user dedup keys).
    alerts = agent.get_alerts()
    assert len(alerts) == 2
    assert all("Dentist" in a.title for a in alerts)

    # Memories attributed to each calendar's owner, dedup key includes uid.
    assert len(_RecordingRestClient.calls) == 2
    for batch, uid in zip(_RecordingRestClient.calls, (1, 2)):
        assert all(m["user_id"] == uid for m in batch)
        assert all(f":{uid}:" in m["key"] for m in batch)

    # Second run: already-alerted events don't re-alert.
    asyncio.run(agent.run())
    assert agent.get_alerts() == []


def test_agent_one_user_failure_does_not_affect_others(backend, monkeypatch):
    """User 1's calendar blowing up must not skip user 2 or wipe alerts."""
    from jarvis_command_sdk import get_current_user_id

    agent_mod = _load_agent()
    _install_rest_client()
    monkeypatch.setattr(agent_mod, "resolve_agent_user_ids", lambda lookup=None: [1, 2])

    class _ExplodingCommand:
        def run(self, request_info, **kwargs):
            raise RuntimeError("CalDAV down")

    agent = agent_mod.CalendarAlertAgent()
    cmd2 = _FakeCommand([_event("Dentist", 10)])
    agent._cmds = {1: _ExplodingCommand(), 2: cmd2}

    asyncio.run(agent.run())

    assert cmd2.seen_ctx_uids == [2]
    assert len(agent.get_alerts()) == 1  # user 2's alert survived user 1's crash
    assert get_current_user_id() is None  # ContextVar cleared despite the raise


def test_agent_evicts_stale_command_cache(backend, monkeypatch):
    agent_mod = _load_agent()
    _install_rest_client()
    monkeypatch.setattr(agent_mod, "resolve_agent_user_ids", lambda lookup=None: [2])

    agent = agent_mod.CalendarAlertAgent()
    agent._cmds = {1: _FakeCommand([]), 2: _FakeCommand([])}

    asyncio.run(agent.run())

    assert 1 not in agent._cmds  # user 1 no longer resolved — cache dropped
    assert 2 in agent._cmds


def test_agent_recurring_event_alerts_again_after_prune(backend, monkeypatch):
    """A recurring event's NEXT instance must alert — keys carry the event
    start and well-past entries are pruned each run."""
    agent_mod = _load_agent()
    _install_rest_client()
    monkeypatch.setattr(agent_mod, "resolve_agent_user_ids", lambda lookup=None: [1])

    agent = agent_mod.CalendarAlertAgent()
    agent._cmds = {1: _FakeCommand([_event("Standup", 10)])}
    asyncio.run(agent.run())
    assert len(agent.get_alerts()) == 1

    # Yesterday's instance is long past — simulate it in the dedup map.
    stale_start = datetime.now(timezone.utc) - timedelta(days=1)
    agent._alerted_event_keys[f"1:Standup:{stale_start.isoformat()}:15min"] = stale_start
    asyncio.run(agent.run())
    assert all(
        start > datetime.now(timezone.utc) - timedelta(hours=2)
        for start in agent._alerted_event_keys.values()
    )  # stale key pruned
    # Today's instance (same start) stays deduped within its window.
    assert agent.get_alerts() == []


def test_agent_idles_with_no_configured_users(backend, monkeypatch):
    agent_mod = _load_agent()
    monkeypatch.setattr(agent_mod, "resolve_agent_user_ids", lambda lookup=None: [])

    agent = agent_mod.CalendarAlertAgent()
    asyncio.run(agent.run())
    assert agent.get_alerts() == []
    assert agent._cmds == {}  # never built a command — probed no calendar


def test_agent_declares_identity_secret(backend):
    agent_mod = _load_agent()
    agent = agent_mod.CalendarAlertAgent()
    identity = [s for s in agent.required_secrets if s.key == "CALENDAR_AGENT_USER"]
    assert len(identity) == 1
    assert identity[0].scope == "integration"
    assert identity[0].value_type == "user"
    assert identity[0].required is False


def test_validate_secrets_passes_when_any_user_configured(backend, monkeypatch):
    agent_mod = _load_agent()
    monkeypatch.setattr(agent_mod, "find_configured_user_ids", lambda: [1])
    agent = agent_mod.CalendarAlertAgent()
    assert agent.validate_secrets() == []


def test_validate_secrets_reports_missing_when_none_configured(backend, monkeypatch):
    agent_mod = _load_agent()
    monkeypatch.setattr(agent_mod, "find_configured_user_ids", lambda: [])
    agent = agent_mod.CalendarAlertAgent()
    assert agent.validate_secrets() == ["CALENDAR_USERNAME or GOOGLE_ACCESS_TOKEN"]


# ---------------------------------------------------------------------------
# Command run() — unknown-speaker guard
# ---------------------------------------------------------------------------


def _load_command():
    _install_get_calendar_events_shared()
    # Another test file's date_util stub may return None from
    # parse_date_array — repoint to working fakes before the (fresh)
    # command module binds them at import time.
    du = sys.modules["get_calendar_events_shared.date_util"]
    du.parse_date_array = lambda keys: [datetime.now() for _ in keys]
    du.dates_to_strings = lambda dates: []
    return _load_real("cal_user_scope_test_cmd", "commands", "get_calendar_events", "command.py")


def test_command_refuses_unknown_speaker(backend):
    cmd_mod = _load_command()
    cmd = cmd_mod.ReadCalendarCommand()
    request_info = SimpleNamespace(voice_command="what's on my calendar", user_id=None)

    response = cmd.run(request_info, resolved_datetimes=["today"])

    assert not response.success
    assert response.context_data["error"] == "unknown_speaker"
    assert "calendar" in response.context_data["message"].lower()


def test_command_contextvar_identity_passes_guard(backend):
    from jarvis_command_sdk import set_current_user_id

    cmd_mod = _load_command()
    cmd = cmd_mod.ReadCalendarCommand()
    request_info = SimpleNamespace(voice_command="what's on my calendar", user_id=None)

    set_current_user_id(1)
    try:
        response = cmd.run(request_info, resolved_datetimes=["today"])
    finally:
        set_current_user_id(None)

    # Identity resolved via the ContextVar → past the guard; with no creds
    # configured it errors about credentials, NOT about the speaker.
    assert not response.success
    assert response.context_data.get("error") != "unknown_speaker"


def test_command_request_info_identity_passes_guard(backend):
    cmd_mod = _load_command()
    cmd = cmd_mod.ReadCalendarCommand()
    request_info = SimpleNamespace(voice_command="what's on my calendar", user_id=1)

    response = cmd.run(request_info, resolved_datetimes=["today"])

    assert not response.success
    assert response.context_data.get("error") != "unknown_speaker"


def test_store_auth_values_writes_user_scoped_tokens(backend):
    from jarvis_command_sdk import set_current_user_id

    cmd_mod = _load_command()
    cmd = cmd_mod.ReadCalendarCommand()

    set_current_user_id(7)
    try:
        cmd.store_auth_values({"access_token": "at", "refresh_token": "rt"})
    finally:
        set_current_user_id(None)

    assert backend.secrets[("GOOGLE_ACCESS_TOKEN", "user", 7)] == "at"
    assert backend.secrets[("GOOGLE_REFRESH_TOKEN", "user", 7)] == "rt"


def test_store_auth_values_refuses_without_user(backend):
    cmd_mod = _load_command()
    cmd = cmd_mod.ReadCalendarCommand()

    cmd.store_auth_values({"access_token": "at", "refresh_token": "rt"})

    # No owner — nothing stored anywhere (loud log, no orphan rows).
    assert backend.secrets == {}


def test_manifest_declares_identity_secret():
    import yaml

    with open(os.path.join(_ROOT, "jarvis_package.yaml")) as f:
        manifest = yaml.safe_load(f)
    entries = [s for s in manifest["secrets"] if s["key"] == "CALENDAR_AGENT_USER"]
    assert len(entries) == 1
    assert entries[0]["scope"] == "integration"
    assert entries[0]["value_type"] == "user"
