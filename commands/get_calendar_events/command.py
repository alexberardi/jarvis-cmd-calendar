from datetime import datetime
from typing import Any, Callable, List

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
    AuthenticationConfig,
    CommandExample,
    CommandResponse,
    ContextOperation,
    ContextResult,
    DateKeys,
    FastPathPattern,
    IJarvisCommand,
    IJarvisParameter,
    IJarvisSecret,
    JarvisParameter,
    JarvisSecret,
    JarvisStorage,
    PreRouteResult,
    RequestInformation,
    get_current_user_id,
)

# Date-key resolution for pre-route. The full set of accepted date keys is
# in DateKeys; we list only the ones a regex can pluck reliably from a
# spoken utterance — keep this in sync with what run() can resolve.
_DATE_KEY_BY_PHRASE: dict[str, str] = {
    "today": DateKeys.TODAY,
    "tonight": DateKeys.TODAY,
    "tomorrow": DateKeys.TOMORROW,
    "tomorrow morning": DateKeys.TOMORROW,
    "tomorrow afternoon": DateKeys.TOMORROW,
    "tomorrow evening": DateKeys.TOMORROW,
    "tomorrow night": DateKeys.TOMORROW,
    "day after tomorrow": DateKeys.DAY_AFTER_TOMORROW,
    "the day after tomorrow": DateKeys.DAY_AFTER_TOMORROW,
    "this weekend": DateKeys.THIS_WEEKEND,
    "the weekend": DateKeys.THIS_WEEKEND,
    "weekend": DateKeys.THIS_WEEKEND,
    "next week": DateKeys.NEXT_WEEK,
    "this week": getattr(DateKeys, "THIS_WEEK", "this_week"),
    "yesterday": getattr(DateKeys, "YESTERDAY", "yesterday"),
}

# Longest-first so "the day after tomorrow" matches before "tomorrow".
_DATE_PHRASE_ALT = "|".join(
    sorted(_DATE_KEY_BY_PHRASE, key=len, reverse=True)
)


def _compose_calendar_message(events: list[dict], date_display: str) -> str:
    """Spoken summary used on the pre-route fast path.

    Lists up to the first 4 events with their start time and summary so the
    user hears a real itinerary instead of a generic count. Falls back to a
    bare count when there are more than 4.
    """
    if not events:
        return f"You have nothing on your calendar for {date_display}."
    if len(events) <= 4:
        parts = []
        for ev in events:
            start = ev.get("start_time", "")
            summary = (ev.get("summary") or "untitled").strip()
            if start == "All day":
                parts.append(f"all day, {summary}")
            elif start:
                parts.append(f"at {start}, {summary}")
            else:
                parts.append(summary)
        joined = "; ".join(parts)
        return f"You have {len(events)} event{'s' if len(events) != 1 else ''} on {date_display}: {joined}."
    summary_titles = ", ".join((ev.get("summary") or "untitled").strip() for ev in events[:3])
    return (
        f"You have {len(events)} events on {date_display}, including "
        f"{summary_titles}, and {len(events) - 3} more."
    )
from get_calendar_events_shared.icloud_calendar_service import ICloudCalendarService
from get_calendar_events_shared.google_calendar_service import GoogleCalendarService
from get_calendar_events_shared.date_util import parse_date_array, format_date_display, dates_to_strings
from get_calendar_events_shared.availability import build_availability

logger = JarvisLogger(service="jarvis-node")

# Default OAuth client ID — same Google Cloud project as Gmail.
# Users can override via GOOGLE_CLIENT_ID secret if they prefer their own.
_DEFAULT_CLIENT_ID = "683175564329-24fi9h6hck48hfrbjhb24vf12680e5ec.apps.googleusercontent.com"


class CalendarConfigurationError(RuntimeError):
    """Calendar isn't usable for this speaker (unauthenticated / misconfigured)."""


class ReadCalendarCommand(IJarvisCommand):

    def __init__(self) -> None:
        super().__init__()
        self._storage = JarvisStorage("get_calendar_events")
        # Cache calendar service instances so the underlying httpx.Client and
        # 1-hour iCloud auth cache survive across calls. Without this, each
        # call (every 5 min from the agent) created a fresh service, leaked
        # an httpx.Client, and forced a full CalDAV re-auth.
        self._cached_service: Any = None
        self._cached_service_key: tuple[str, ...] | None = None

    @property
    def command_name(self) -> str:
        return "get_calendar_events"

    @property
    def keywords(self) -> List[str]:
        return ["calendar", "events", "schedule", "appointments", "meetings", "what's on", "today's events", "agenda", "plans"]

    @property
    def description(self) -> str:
        return "Retrieve calendar events for specified dates or date ranges. Use for ALL calendar and scheduling queries."

    def generate_prompt_examples(self) -> List[CommandExample]:
        """Generate concise example utterances with expected parameters using date keys"""
        return [
            CommandExample(
                voice_command="What's on my calendar today?",
                expected_parameters={"resolved_datetimes": [DateKeys.TODAY]},
                is_primary=True
            ),
            CommandExample(
                voice_command="Show me my schedule for tomorrow",
                expected_parameters={"resolved_datetimes": [DateKeys.TOMORROW]}
            ),
            CommandExample(
                voice_command="What appointments do I have the day after tomorrow?",
                expected_parameters={"resolved_datetimes": [DateKeys.DAY_AFTER_TOMORROW]}
            ),
            CommandExample(
                voice_command="Show my calendar for this weekend",
                expected_parameters={"resolved_datetimes": [DateKeys.THIS_WEEKEND]}
            ),
            CommandExample(
                voice_command="Read my calendar",
                expected_parameters={"resolved_datetimes": [DateKeys.TODAY]}
            )
        ]

    def generate_adapter_examples(self) -> List[CommandExample]:
        """Generate varied examples for adapter training.

        Focus areas:
        - Implicit today (no date word -> resolved_datetimes: ["today"])
        - Day after tomorrow as single token
        - Various phrasings for calendar queries
        """
        examples = [
            # === IMPLICIT TODAY - Critical: no date word = today ===
            ("Read my calendar", [DateKeys.TODAY], True),
            ("What's on my calendar?", [DateKeys.TODAY], False),
            ("What's on my schedule?", [DateKeys.TODAY], False),
            ("Do I have any meetings?", [DateKeys.TODAY], False),
            ("Do I have any appointments?", [DateKeys.TODAY], False),
            ("Am I busy?", [DateKeys.TODAY], False),
            ("What are my plans?", [DateKeys.TODAY], False),
            ("Check my calendar", [DateKeys.TODAY], False),

            # === EXPLICIT TODAY ===
            ("What's on my calendar today?", [DateKeys.TODAY], False),
            ("What meetings do I have today?", [DateKeys.TODAY], False),
            ("What's my schedule for today?", [DateKeys.TODAY], False),

            # === TOMORROW ===
            ("What's on my calendar tomorrow?", [DateKeys.TOMORROW], False),
            ("Show me my schedule for tomorrow", [DateKeys.TOMORROW], False),
            ("What appointments do I have tomorrow?", [DateKeys.TOMORROW], False),

            # === DAY AFTER TOMORROW - single token ===
            ("What appointments do I have the day after tomorrow?", [DateKeys.DAY_AFTER_TOMORROW], False),
            ("What's on my calendar the day after tomorrow?", [DateKeys.DAY_AFTER_TOMORROW], False),
            ("Show my schedule for the day after tomorrow", [DateKeys.DAY_AFTER_TOMORROW], False),

            # === WEEKEND / WEEK ===
            ("What's on my calendar this weekend?", [DateKeys.THIS_WEEKEND], False),
            ("Show my calendar for this weekend", [DateKeys.THIS_WEEKEND], False),
            ("What meetings do I have next week?", [DateKeys.NEXT_WEEK], False),
        ]
        return [
            CommandExample(voice_command=voice, expected_parameters={"resolved_datetimes": dates}, is_primary=is_primary)
            for voice, dates, is_primary in examples
        ]

    @property
    def parameters(self) -> List[IJarvisParameter]:
        return [
            JarvisParameter("resolved_datetimes", "array<datetime>", description="Date keys like 'today', 'tomorrow', 'yesterday', 'this_weekend', 'next_week', etc. The server resolves these to actual dates.", required=True)
        ]

    # ── Context provider (plan-time availability) ─────────────────────────
    #
    # A server-side planner (the phone-call plan-draft step) asks for these
    # BEFORE acting, so a call brief can carry the user's real constraint
    # envelope. Read-only, plan-time only — never a live tool inside a call.

    @property
    def context_operations(self) -> list[ContextOperation]:
        return [
            ContextOperation(
                name="availability",
                description=(
                    "Free/busy windows from the speaker's calendar over a "
                    "date range, for scheduling on their behalf."
                ),
                params_schema={
                    "start": {
                        "type": "string",
                        "required": True,
                        "description": "Range start, ISO date (YYYY-MM-DD)",
                    },
                    "end": {
                        "type": "string",
                        "required": True,
                        "description": "Range end, ISO date (exclusive)",
                    },
                },
            )
        ]

    def execute_context_operation(self, operation: str, params: dict) -> ContextResult:
        if operation != "availability":
            return ContextResult.failed(f"unsupported context operation '{operation}'")

        try:
            start = datetime.strptime(str(params["start"]), "%Y-%m-%d")
            end = datetime.strptime(str(params["end"]), "%Y-%m-%d")
        except (KeyError, ValueError) as exc:
            return ContextResult.failed(f"invalid date range: {exc}")

        days = max((end - start).days, 1)

        # Same user-scoping refusal as run(): credentials are per-speaker, so
        # without a resolved user every secret read returns None and we would
        # report "missing credentials" for what is really an identity gap.
        if get_current_user_id() is None:
            return ContextResult.failed("unknown speaker — no personal calendar")

        try:
            service = self._build_calendar_service()
        except CalendarConfigurationError as exc:
            return ContextResult.failed(str(exc))

        try:
            events = service.read_events(start, days)
        except Exception as exc:  # noqa: BLE001 — upstream flakiness is data
            logger.warning(f"availability read_events failed: {exc}")
            return ContextResult.failed(f"calendar unreachable: {exc}")

        return ContextResult(data=build_availability(events, start, days))

    def _build_calendar_service(self):
        """Construct the configured calendar service or raise.

        Mirrors run()'s provider selection; extracted so the context op and
        the voice path can never drift on credentials or caching.
        """
        calendar_type = self._get_calendar_type()
        default_calendar = self._storage.get_secret("CALENDAR_DEFAULT_NAME", scope="user")

        if calendar_type == "google":
            access_token = self._storage.get_secret("GOOGLE_ACCESS_TOKEN", scope="user")
            refresh_token = self._storage.get_secret("GOOGLE_REFRESH_TOKEN", scope="user")
            client_id = self._get_client_id()
            if not access_token:
                raise CalendarConfigurationError(
                    "Google Calendar not authenticated. Complete OAuth setup first."
                )
            return self._get_or_create_service(
                ("google", access_token, refresh_token or "", client_id or "", default_calendar or "primary"),
                lambda: GoogleCalendarService(
                    access_token=access_token,
                    refresh_token=refresh_token or "",
                    client_id=client_id or "",
                    calendar_id=default_calendar or "primary",
                ),
            )

        if calendar_type == "icloud":
            username = self._storage.get_secret("CALENDAR_USERNAME", scope="user")
            password = self._storage.get_secret("CALENDAR_PASSWORD", scope="user")
            if not all([username, password]):
                raise CalendarConfigurationError("Missing iCloud calendar credentials")
            return self._get_or_create_service(
                ("icloud", str(username), str(password), default_calendar or "default"),
                lambda: ICloudCalendarService(username, password, default_calendar or "default"),
            )

        raise CalendarConfigurationError(f"Unsupported calendar type: {calendar_type}")

    def _get_calendar_type(self) -> str:
        """Read CALENDAR_TYPE from DB, defaulting to 'icloud'."""
        try:
            value = self._storage.get_secret("CALENDAR_TYPE", scope="user")
            return (value or "icloud").lower()
        except Exception:
            return "icloud"

    def _get_client_id(self) -> str:
        return self._storage.get_secret("GOOGLE_CLIENT_ID", scope="integration") or _DEFAULT_CLIENT_ID

    @property
    def associated_service(self) -> str:
        return "Calendar"

    @property
    def setup_guide(self) -> str | None:
        cal_type = self._get_calendar_type()
        if cal_type == "google":
            return (
                "## Google Calendar\n\n"
                "1. Set **Calendar Type** to `google`\n"
                "2. Tap **Authenticate with Google Calendar** below\n"
                "3. Sign in with your Google account and grant calendar access\n\n"
                "That's it — tokens are managed automatically.\n\n"
                "> **Advanced**: A default OAuth client ID is provided. "
                "To use your own, set the **Client ID** field before authenticating.\n"
            )
        return (
            "## Apple iCloud Calendar\n\n"
            "Jarvis connects to your iCloud calendar using an **app-specific password** "
            "(not your main Apple ID password).\n\n"
            "### Generate an App-Specific Password\n\n"
            "1. Go to [appleid.apple.com](https://appleid.apple.com) and sign in\n"
            "2. In the **Sign-In and Security** section, click **App-Specific Passwords**\n"
            "3. Click **+** to generate a new password\n"
            "4. Name it something like `Jarvis Calendar`\n"
            "5. Copy the generated password (format: `xxxx-xxxx-xxxx-xxxx`)\n\n"
            "### Configure Jarvis\n\n"
            "- **Username**: Your Apple ID email (e.g., `you@icloud.com`)\n"
            "- **Password**: The app-specific password from step 5\n"
            "- **Default Calendar**: The exact name of your calendar (e.g., `Home`, `Work`). "
            "Leave blank to use all calendars.\n\n"
            "> **Note**: If you have two-factor authentication enabled (most accounts do), "
            "you **must** use an app-specific password. Your regular password will not work.\n"
        )

    @property
    def required_secrets(self) -> List[IJarvisSecret]:
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

    @property
    def all_possible_secrets(self) -> List[IJarvisSecret]:
        return [
            JarvisSecret("CALENDAR_TYPE", "Type of calendar service (icloud, google)", "user", "string", is_sensitive=False, friendly_name="Calendar Type"),
            JarvisSecret("CALENDAR_DEFAULT_NAME", "Default calendar name to use", "user", "string", is_sensitive=False, friendly_name="Default Calendar"),
            JarvisSecret("CALENDAR_USERNAME", "Username/Apple ID for calendar service", "user", "string", friendly_name="Username"),
            JarvisSecret("CALENDAR_PASSWORD", "Password/app-specific password for calendar service", "user", "string", friendly_name="Password"),
            JarvisSecret("GOOGLE_CLIENT_ID", "Google OAuth client ID (optional — a default is provided)", "integration", "string", required=False, is_sensitive=False, friendly_name="Client ID (optional)"),
            JarvisSecret("GOOGLE_ACCESS_TOKEN", "Google OAuth access token (auto-populated)", "user", "string", friendly_name="Access Token"),
            JarvisSecret("GOOGLE_REFRESH_TOKEN", "Google OAuth refresh token (auto-populated)", "user", "string", friendly_name="Refresh Token"),
        ]

    @property
    def authentication(self) -> AuthenticationConfig | None:
        if self._get_calendar_type() != "google":
            return None
        client_id = self._get_client_id()
        return AuthenticationConfig(
            type="oauth",
            provider="google_calendar",
            friendly_name="Google Calendar",
            client_id=client_id,
            keys=["access_token", "refresh_token"],
            authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
            exchange_url="https://oauth2.googleapis.com/token",
            scopes=["https://www.googleapis.com/auth/calendar.readonly"],
            supports_pkce=True,
            extra_authorize_params={"access_type": "offline", "prompt": "consent"},
            requires_background_refresh=True,
            refresh_token_secret_key="GOOGLE_REFRESH_TOKEN",
        )

    def store_auth_values(self, values: dict[str, str]) -> None:
        """Store Google OAuth tokens from the mobile OAuth callback.

        Tokens are user-scoped — the caller (node auth pull / token refresh
        agent) sets the SDK user ContextVar to the token owner. Without an
        owner there is no correct row to write, so refuse loudly rather than
        store tokens nobody can read.
        """
        if get_current_user_id() is None:
            logger.error(
                "Refusing to store Google calendar tokens: no user in context "
                "(requires CC with OAuth user threading — update jarvis-command-center)"
            )
            return
        if "access_token" in values:
            self._storage.set_secret("GOOGLE_ACCESS_TOKEN", values["access_token"], scope="user")
        if "refresh_token" in values:
            self._storage.set_secret("GOOGLE_REFRESH_TOKEN", values["refresh_token"], scope="user")
        # Tokens changed — drop the cached service so the next call rebuilds it.
        self._cached_service = None
        self._cached_service_key = None
        try:
            from services.command_auth_service import clear_auth_flag
            clear_auth_flag("google_calendar")
        except ImportError:
            pass

    def _get_or_create_service(self, key: tuple[str, ...], factory: Callable[[], Any]) -> Any:
        """Return a cached calendar service, rebuilding only if `key` changed.

        `key` is a tuple of all inputs that affect the service (creds, calendar
        name, etc.). When it matches the cached key, the existing service —
        and its httpx.Client + 1-hour auth cache — is reused.
        """
        if self._cached_service is None or self._cached_service_key != key:
            # Best-effort close on the old client
            old = self._cached_service
            close = getattr(getattr(old, "session", None), "close", None) if old else None
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
            self._cached_service = factory()
            self._cached_service_key = key
        return self._cached_service

    @property
    def critical_rules(self) -> List[str]:
        return [
            "'day after tomorrow' = single key 'day_after_tomorrow', NOT two separate dates.",
        ]

    # ------------------------------------------------------------------
    # Fast-path patterns — bypass the LLM for calendar queries whose date
    # is unambiguous. Implicit "today" is the default for shapes without a
    # date phrase. All shapes are anchored — we don't want to claim
    # utterances like "remind me to check my calendar" that mention the
    # word "calendar" but aren't actually queries.
    # ------------------------------------------------------------------
    @property
    def fast_path_patterns(self) -> List[FastPathPattern]:
        # Verbs/nouns that unambiguously indicate a calendar query.
        # Combined here as a single alternation. `appointments` and
        # `meetings` are bare nouns; the rest wrap verb + noun.
        calendar_noun = (
            r"(?:calendar|schedule|appointments?|meetings?|agenda|plans?)"
        )
        return [
            FastPathPattern(
                id="get_calendar_events.with_date",
                description="Bypass LLM for 'what's on my calendar <date>' / 'meetings <date>'",
                example="what's on my calendar tomorrow",
                regex=(
                    r"^\s*(?:"
                    + r"what'?s\s+on\s+my\s+" + calendar_noun
                    + r"|what\s+(?:meetings?|appointments?)\s+do\s+i\s+have"
                    + r"|do\s+i\s+have\s+any\s+(?:meetings?|appointments?|plans?)"
                    + r"|am\s+i\s+busy"
                    + r"|show\s+(?:me\s+)?my\s+" + calendar_noun
                    + r"|show\s+my\s+" + calendar_noun
                    + r"|what'?s\s+my\s+" + calendar_noun
                    + r"|check\s+my\s+" + calendar_noun
                    + r")\s+(?:for\s+|on\s+|this\s+)?(?P<date>"
                    + _DATE_PHRASE_ALT
                    + r")\s*[?.!]*$"
                ),
                handler="_fp_with_date",
            ),
            FastPathPattern(
                id="get_calendar_events.implicit_today",
                description="Bypass LLM for bare calendar queries (defaults to today)",
                example="what's on my calendar",
                regex=(
                    r"^\s*(?:"
                    + r"what'?s\s+on\s+my\s+" + calendar_noun
                    + r"|what\s+(?:meetings?|appointments?)\s+do\s+i\s+have"
                    + r"|do\s+i\s+have\s+any\s+(?:meetings?|appointments?|plans?)"
                    + r"|am\s+i\s+busy"
                    + r"|show\s+(?:me\s+)?my\s+" + calendar_noun
                    + r"|read\s+my\s+" + calendar_noun
                    + r"|check\s+my\s+" + calendar_noun
                    + r"|what'?s\s+my\s+" + calendar_noun
                    + r"|what\s+are\s+my\s+plans"
                    + r")\s*[?.!]*$"
                ),
                handler="_fp_implicit_today",
            ),
        ]

    def _fp_with_date(self, match, voice_command: str) -> PreRouteResult | None:
        phrase = match.group("date").lower().strip()
        date_key = _DATE_KEY_BY_PHRASE.get(phrase)
        if date_key is None:
            return None
        return PreRouteResult(arguments={"resolved_datetimes": [date_key]})

    def _fp_implicit_today(self, match, voice_command: str) -> PreRouteResult | None:
        return PreRouteResult(arguments={"resolved_datetimes": [DateKeys.TODAY]})

    def run(self, request_info, **kwargs) -> CommandResponse:
        # Get parameters
        datetimes_array = kwargs.get("resolved_datetimes")

        # Debug: Check what type request_info actually is
        logger.debug(f"DEBUG: request_info type: {type(request_info)}")
        logger.debug(f"DEBUG: request_info content: {request_info}")

        # Handle both RequestInformation object and dictionary
        if hasattr(request_info, 'voice_command'):
            voice_command = request_info.voice_command
        elif isinstance(request_info, dict) and 'voice_command' in request_info:
            voice_command = request_info['voice_command']
        else:
            # Fallback if we can't get the voice command
            voice_command = "unknown command"
            logger.debug(f"WARNING: Could not extract voice_command from request_info: {request_info}")

        # Calendar credentials are user-scoped — without a resolved speaker
        # every secret read below returns None and the user would hear a
        # misleading "missing credentials" error. Refuse up front instead
        # (same pattern as export_shopping_list). The ContextVar fallback
        # covers callers that set the SDK user context but not request_info.
        if hasattr(request_info, "user_id"):
            speaker_user_id = request_info.user_id
        elif isinstance(request_info, dict):
            speaker_user_id = request_info.get("user_id")
        else:
            speaker_user_id = None
        if speaker_user_id is None:
            speaker_user_id = get_current_user_id()
        if speaker_user_id is None:
            message = (
                "I'm not sure whose calendar to check — I couldn't tell who's "
                "asking. Try training your voice in the app."
            )
            logger.warning("get_calendar_events refused: no speaker_user_id")
            return CommandResponse.error_response(
                error_details="Unknown speaker — cannot resolve a personal calendar.",
                context_data={"error": "unknown_speaker", "message": message},
            )

        if not datetimes_array:
            return CommandResponse.error_response(
                error_details="Missing required resolved_datetimes parameter",
                context_data={
                    "dates": [],
                    "events": [],
                    "error": "Missing dates"
                }
            )

        # Parse datetime parameters
        try:
            target_dates = parse_date_array(datetimes_array)
            logger.debug(f"DEBUG: Parsed target_dates: {[d.strftime('%Y-%m-%d %H:%M:%S') for d in target_dates]}")
        except ValueError as e:
            return CommandResponse.error_response(
                error_details=str(e),
                context_data={
                    "dates": datetimes_array if datetimes_array else [],
                    "events": [],
                    "error": "Invalid datetime format"
                }
            )

        # Log the original voice command for debugging
        logger.debug(f"Voice command received: '{voice_command}'")

        calendar_type = self._get_calendar_type()  # reported in context_data below

        try:
            # Initialize appropriate calendar service. Shared with the
            # availability context op so credentials/caching can't drift.
            try:
                calendar_service = self._build_calendar_service()
            except CalendarConfigurationError as exc:
                return CommandResponse.error_response(
                    error_details=str(exc),
                    context_data={
                        "dates": dates_to_strings(target_dates),
                        "events": [],
                        "error": str(exc),
                    },
                )

            # Collect events based on whether we have specific dates or are using the default
            all_events = []

            # Check if we're using specific dates from the LLM
            if len(target_dates) > 1:
                # Multiple specific dates from LLM - query the entire range
                start_date = target_dates[0]
                end_date = target_dates[-1]
                # Add 1 day buffer to catch events that span midnight
                total_span = (end_date - start_date).days + 1

                logger.debug(f"DEBUG: Multiple dates from LLM - querying range: {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')} (span: {total_span} days)")
                all_events = calendar_service.read_events(start_date, total_span)
                logger.debug(f"DEBUG: Found {len(all_events)} total events across specified range")
            else:
                # Single specific date from LLM - query just that date
                start_date = target_dates[0]
                logger.debug(f"DEBUG: Single date from LLM - querying: {start_date.strftime('%Y-%m-%d')} with 1 day lookahead")
                all_events = calendar_service.read_events(start_date, 1)
                logger.debug(f"DEBUG: Found {len(all_events)} events for single specified date")

            # Debug: Show all events with their IDs
            for i, event in enumerate(all_events):
                logger.debug(f"DEBUG: Event {i+1}: {event.summary} at {event.start_time} (ID: {event.id})")

            # Check if iCloud service actually authenticated successfully
            if hasattr(calendar_service, '_authenticated') and not calendar_service._authenticated:
                return CommandResponse.error_response(
                                        error_details="Calendar service authentication failed",
                    context_data={
                        "dates": dates_to_strings(target_dates),
                        "events": [],
                        "error": "Authentication failed"
                    }
                )

            if all_events:
                # Format events for response
                formatted_events = []
                for event in all_events:
                    formatted_event = {
                        "id": event.id,
                        "summary": event.summary,
                        "start_time": event.start_time.strftime("%I:%M %p").lstrip("0") if not event.is_all_day else "All day",
                        "end_time": event.end_time.strftime("%I:%M %p").lstrip("0") if not event.is_all_day else "All day",
                        "location": event.location,
                        "description": event.description,
                        "is_all_day": event.is_all_day
                    }
                    formatted_events.append(formatted_event)

                # Create summary message
                date_display = format_date_display(target_dates)
                message = f"You have {len(all_events)} event(s) on {date_display}"
                logger.debug(message)

                ctx = {
                    "dates": dates_to_strings(target_dates),
                    "calendar_type": calendar_type,
                    "calendar_name": default_calendar or "default",
                    "events": formatted_events,
                    "total_events": len(all_events),
                    "voice_command": voice_command,
                    "target_dates": dates_to_strings(target_dates),
                    "date_display": date_display,
                }
                # Always pre-compose a spoken summary so the command-center
                # voice fast-path speaks it directly (skipping the formatting
                # LLM, which otherwise risks generic filler like "Task completed.").
                ctx["message"] = _compose_calendar_message(formatted_events, date_display)
                return CommandResponse.follow_up_response(context_data=ctx)
            else:
                # No events found
                date_display = format_date_display(target_dates)
                message = f"No events found on {date_display}"
                logger.debug(message)

                ctx = {
                    "dates": dates_to_strings(target_dates),
                    "calendar_type": calendar_type,
                    "calendar_name": default_calendar or "default",
                    "events": [],
                    "total_events": 0,
                    "voice_command": voice_command,
                    "target_dates": dates_to_strings(target_dates),
                    "date_display": date_display,
                }
                ctx["message"] = f"You have nothing on your calendar for {date_display}."
                return CommandResponse.follow_up_response(context_data=ctx)

        except Exception as e:
            return CommandResponse.error_response(
                                error_details=str(e),
                context_data={
                    "dates": dates_to_strings(target_dates),
                    "events": [],
                    "error": str(e)
                }
            )
