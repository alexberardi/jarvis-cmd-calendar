"""add_event — a calendar WRITE command exposed as a proposable action.

Unlike `get_calendar_events` (read-only), this command creates an event. Its
``create_event`` ``@callback`` is declared in :meth:`proposable_actions` so an
agent — e.g. the email agent that spots "let's meet Tuesday at 3" — can surface
an "Add to your calendar?" confirm card. On tap, command-center's generic
dispatcher validates the proposed data against the declaration and runs this
callback on the node.

Backend selection, user scoping, and secret keys mirror ReadCalendarCommand:
credentials are user-scoped, so the speaker must be resolvable (request_info or
the SDK ContextVar) or we refuse rather than write to the wrong calendar. No new
secrets are introduced — the existing CALENDAR_* / GOOGLE_* creds are reused.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from typing import Any, List

try:
    from jarvis_log_client import JarvisLogger
except ImportError:
    import logging

    class JarvisLogger:
        def __init__(self, **kw): self._log = logging.getLogger(kw.get("service", __name__))
        def info(self, msg, **kw): self._log.info(msg)
        def warning(self, msg, **kw): self._log.warning(msg)
        def error(self, msg, **kw): self._log.error(msg)
        def debug(self, msg, **kw): self._log.debug(msg)

from jarvis_command_sdk import (
    BlastTier,
    CommandExample,
    CommandResponse,
    IJarvisCommand,
    IJarvisParameter,
    IJarvisSecret,
    JarvisParameter,
    JarvisSecret,
    JarvisStorage,
    ProposableAction,
    RequestInformation,
    callback,
    get_current_user_id,
)

from get_calendar_events_shared.icloud_calendar_service import ICloudCalendarService
from get_calendar_events_shared.google_calendar_service import GoogleCalendarService

logger = JarvisLogger(service="jarvis-node")

# Default OAuth client ID — same Google Cloud project as the read command.
_DEFAULT_CLIENT_ID = "683175564329-24fi9h6hck48hfrbjhb24vf12680e5ec.apps.googleusercontent.com"

# How long an idempotency record is retained. Long enough to absorb any
# dispatch retry / timeout-after-success window; well short of forever so the
# command-data table doesn't grow unbounded.
_IDEMPOTENCY_TTL = timedelta(days=30)


def _parse_dt(value: Any) -> datetime | None:
    """Accept an ISO-8601 string or a datetime; return a datetime or None.

    The card/agent sends ISO strings; a direct voice/tool call may already pass
    a datetime. The iCloud/Google services need real datetime objects (they call
    ``.strftime`` / ``.isoformat``), so normalise here.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        logger.warning("add_event could not parse datetime", value=text)
        return None


class AddEventCommand(IJarvisCommand):

    def __init__(self) -> None:
        super().__init__()
        # Own data namespace for idempotency records. Secrets are keyed globally
        # by (key, scope, user_id), so reusing the calendar creds works despite
        # the distinct command_name.
        self._storage = JarvisStorage("add_event")

    @property
    def command_name(self) -> str:
        return "add_event"

    @property
    def description(self) -> str:
        return (
            "Add or create a new event on the user's calendar. Use when the user "
            "wants to schedule, book, or put something on their calendar."
        )

    @property
    def keywords(self) -> List[str]:
        return [
            "add event", "create event", "schedule", "book", "new appointment",
            "put on my calendar", "add to calendar", "make an appointment",
        ]

    @property
    def parameters(self) -> List[IJarvisParameter]:
        return [
            JarvisParameter("title", "string", required=True, description="Title/summary of the event, e.g. 'Dentist appointment'."),
            JarvisParameter("start", "datetime", required=True, description="Event start date and time (ISO-8601)."),
            JarvisParameter("end", "datetime", required=False, description="Event end date and time (ISO-8601). Defaults to one hour after start."),
            JarvisParameter("location", "string", required=False, description="Optional event location."),
            JarvisParameter("idempotency_key", "string", required=True, description="Stable de-dup key so a retried confirmation does not create a duplicate event."),
        ]

    @property
    def proposable_actions(self) -> List[ProposableAction]:
        # Opt create_event in to being surfaced by ANY agent as a confirm card.
        # This declaration IS the capability grant — command-center refuses to
        # dispatch a callback that isn't listed here.
        return [
            ProposableAction(
                callback="create_event",
                params=self.parameters,
                card_title="Add to your calendar?",
                confirm_label="Add",
                editable=["title", "start", "end", "location"],
                blast_tier=BlastTier.reversible,
                idempotency_param="idempotency_key",
            )
        ]

    def generate_prompt_examples(self) -> List[CommandExample]:
        return [
            CommandExample(
                voice_command="Add a dentist appointment tomorrow at 3pm",
                expected_parameters={"title": "Dentist appointment", "start": "2026-08-09T15:00:00"},
                is_primary=True,
            ),
            CommandExample(
                voice_command="Put team standup on my calendar Monday at 9am",
                expected_parameters={"title": "Team standup", "start": "2026-08-11T09:00:00"},
            ),
            CommandExample(
                voice_command="Schedule lunch with Sam Friday from noon to 1pm at The Diner",
                expected_parameters={
                    "title": "Lunch with Sam",
                    "start": "2026-08-15T12:00:00",
                    "end": "2026-08-15T13:00:00",
                    "location": "The Diner",
                },
            ),
        ]

    def generate_adapter_examples(self) -> List[CommandExample]:
        examples = [
            ("Add a dentist appointment tomorrow at 3pm", {"title": "Dentist appointment", "start": "2026-08-09T15:00:00"}, True),
            ("Create a meeting called Budget Review on Thursday at 10am", {"title": "Budget Review", "start": "2026-08-14T10:00:00"}, False),
            ("Book a haircut Saturday at 2pm", {"title": "Haircut", "start": "2026-08-16T14:00:00"}, False),
            ("Put team standup on my calendar Monday at 9am", {"title": "Team standup", "start": "2026-08-11T09:00:00"}, False),
            ("Schedule a call with mom tonight at 8pm", {"title": "Call with mom", "start": "2026-08-08T20:00:00"}, False),
            ("Add lunch with Sam Friday from noon to 1pm at The Diner", {"title": "Lunch with Sam", "start": "2026-08-15T12:00:00", "end": "2026-08-15T13:00:00", "location": "The Diner"}, False),
            ("Make an appointment for the doctor next Tuesday at 11:30am", {"title": "Doctor", "start": "2026-08-12T11:30:00"}, False),
        ]
        return [
            CommandExample(voice_command=voice, expected_parameters=params, is_primary=primary)
            for voice, params, primary in examples
        ]

    @property
    def associated_service(self) -> str:
        return "Calendar"

    @property
    def required_secrets(self) -> List[IJarvisSecret]:
        # Reuse the read command's calendar credentials — no new secrets.
        cal_type = self._get_calendar_type()
        secrets: list[IJarvisSecret] = [
            JarvisSecret("CALENDAR_TYPE", "Type of calendar service (icloud, google)", "user", "string", is_sensitive=False, friendly_name="Calendar Type"),
            JarvisSecret("CALENDAR_DEFAULT_NAME", "Default calendar name to use", "user", "string", is_sensitive=False, friendly_name="Default Calendar"),
        ]
        if cal_type == "google":
            secrets.append(
                JarvisSecret("GOOGLE_CLIENT_ID", "Google OAuth client ID (optional — a default is provided)", "integration", "string", required=False, is_sensitive=False, friendly_name="Client ID (optional)"),
            )
        else:
            secrets.extend([
                JarvisSecret("CALENDAR_USERNAME", "Username/Apple ID for calendar service", "user", "string", friendly_name="Username"),
                JarvisSecret("CALENDAR_PASSWORD", "Password/app-specific password for calendar service", "user", "string", friendly_name="Password"),
            ])
        return secrets

    # ------------------------------------------------------------------
    # Config helpers (mirror ReadCalendarCommand)
    # ------------------------------------------------------------------

    def _get_calendar_type(self) -> str:
        try:
            value = self._storage.get_secret("CALENDAR_TYPE", scope="user")
            return (value or "icloud").lower()
        except Exception:
            return "icloud"

    def _get_client_id(self) -> str:
        return self._storage.get_secret("GOOGLE_CLIENT_ID", scope="integration") or _DEFAULT_CLIENT_ID

    def _resolve_speaker(self, request_info) -> int | None:
        """User scoping — request_info wins, then the SDK ContextVar."""
        if hasattr(request_info, "user_id"):
            speaker_user_id = request_info.user_id
        elif isinstance(request_info, dict):
            speaker_user_id = request_info.get("user_id")
        else:
            speaker_user_id = None
        if speaker_user_id is None:
            speaker_user_id = get_current_user_id()
        return speaker_user_id

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------

    @callback("create_event")
    def create_event(self, data: dict, request_info) -> CommandResponse:
        """Create a calendar event from a confirmed proposable-action card.

        Signature: ``create_event(self, data: dict, request_info) -> CommandResponse``.

        event_data mapping (LLM/card params → iCloud/Google service keys):
            title           → summary
            start (ISO/dt)  → start_time (datetime)
            end (ISO/dt)    → end_time  (datetime; defaults to start + 1h)
            location        → location (omitted when absent)

        IDEMPOTENCY CONTRACT: command-center de-dupes *dispatch* on
        ``idempotency_key`` (see ProposableAction.idempotency_param), but a
        confirm that succeeds on the calendar yet times out on the way back can
        be retried. This no-op guard — checking for an existing record under the
        key BEFORE writing and recording one AFTER a successful write — is the
        only thing that prevents that timeout-after-success from double-booking.
        """
        speaker_user_id = self._resolve_speaker(request_info)
        if speaker_user_id is None:
            message = (
                "I'm not sure whose calendar to add this to — I couldn't tell "
                "who's asking. Try training your voice in the app."
            )
            logger.warning("add_event refused: no speaker_user_id")
            return CommandResponse.error_response(
                error_details="Unknown speaker — cannot resolve a personal calendar.",
                context_data={"error": "unknown_speaker", "added": False, "message": message},
            )

        data = data or {}
        title = data.get("title")
        start = _parse_dt(data.get("start"))
        if not title or start is None:
            return CommandResponse.error_response(
                error_details="add_event requires a title and a valid start datetime.",
                context_data={"error": "invalid_params", "added": False, "message": "I need an event title and a start time to add it."},
            )

        end = _parse_dt(data.get("end")) or (start + timedelta(hours=1))
        location = data.get("location")
        idempotency_key = data.get("idempotency_key")

        # Idempotency guard (see contract above): if we've already written this
        # key, do NOT write again — just report success.
        if idempotency_key:
            existing = self._storage.get(idempotency_key)
            if existing is not None:
                logger.info("add_event idempotent no-op", idempotency_key=idempotency_key)
                return CommandResponse.final_response(
                    context_data={"added": True, "idempotent": True, "message": "That's already on your calendar."}
                )

        # Map to the service-layer event_data keys.
        event_data: dict[str, Any] = {"summary": title, "start_time": start, "end_time": end}
        if location:
            event_data["location"] = location

        ok, error_message = self._write_event(event_data)
        if not ok:
            return CommandResponse.error_response(
                error_details=error_message or "Failed to add the calendar event.",
                context_data={"added": False, "message": error_message or "I couldn't add that to your calendar."},
            )

        # Record the idempotency key AFTER a confirmed write (JSON-serialisable).
        if idempotency_key:
            self._storage.save(
                idempotency_key,
                {
                    "summary": title,
                    "start_time": start.isoformat(),
                    "end_time": end.isoformat(),
                    "location": location,
                    "user_id": speaker_user_id,
                    "created_at": datetime.now().isoformat(),
                },
                expires_at=datetime.now() + _IDEMPOTENCY_TTL,
            )

        return CommandResponse.final_response(
            context_data={"added": True, "message": "Added to your calendar."}
        )

    def _write_event(self, event_data: dict) -> tuple[bool, str | None]:
        """Pick the configured backend and write. Returns (ok, error_message)."""
        calendar_type = self._get_calendar_type()
        default_calendar = self._storage.get_secret("CALENDAR_DEFAULT_NAME", scope="user")

        if calendar_type == "google":
            access_token = self._storage.get_secret("GOOGLE_ACCESS_TOKEN", scope="user")
            refresh_token = self._storage.get_secret("GOOGLE_REFRESH_TOKEN", scope="user")
            client_id = self._get_client_id()
            if not access_token:
                return False, "Google Calendar isn't connected yet — finish setup in the app."
            service = GoogleCalendarService(
                access_token=access_token,
                refresh_token=refresh_token or "",
                client_id=client_id or "",
                calendar_id=default_calendar or "primary",
            )
            return bool(service.create_event(event_data)), None

        if calendar_type == "icloud":
            username = self._storage.get_secret("CALENDAR_USERNAME", scope="user")
            password = self._storage.get_secret("CALENDAR_PASSWORD", scope="user")
            if not all([username, password]):
                return False, "Your iCloud calendar credentials aren't set up yet."
            service = ICloudCalendarService(username, password, default_calendar or "default")
            return bool(service.add_event(event_data)), None

        return False, f"Unsupported calendar type: {calendar_type}"

    def _derive_idempotency_key(self, title: Any, start: Any) -> str:
        """Deterministic fallback key for the voice/tool path.

        The proposable-action card always carries a stable idempotency_key; a
        direct voice call rarely does, so derive one from the event identity to
        keep repeated phrasings within the TTL window from double-writing.
        """
        raw = f"{title}|{start}"
        return "voice_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def run(self, request_info, **kwargs) -> CommandResponse:
        """Voice/tool entry point — delegate to the same write path as the card.

        Directory inference registers this command as an LLM tool, so a user can
        say "add a dentist appointment tomorrow at 3pm" directly. We funnel it
        through create_event so there is one write + idempotency code path.
        """
        idempotency_key = kwargs.get("idempotency_key") or self._derive_idempotency_key(
            kwargs.get("title"), kwargs.get("start")
        )
        data = {
            "title": kwargs.get("title"),
            "start": kwargs.get("start"),
            "end": kwargs.get("end"),
            "location": kwargs.get("location"),
            "idempotency_key": idempotency_key,
        }
        return self.create_event(data, request_info)
