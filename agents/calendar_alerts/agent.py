"""CalendarAlertAgent — monitors calendar events and generates time-proximity alerts.

Runs every 5 minutes. Produces alerts based on how soon events are:
- Event in <=15 min -> priority 3, TTL 15 min
- Event in <=60 min -> priority 2, TTL 30 min

Also injects today's events into the command center's memory system so
Jarvis has proactive awareness of the user's schedule.

Requires calendar secrets to be configured (skipped otherwise via standard
agent discovery secret validation).
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

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
    AgentSchedule,
    Alert,
    IJarvisAgent,
    IJarvisSecret,
    JarvisSecret,
    JarvisStorage,
    RequestInformation,
)

logger = JarvisLogger(service="jarvis-node")

REFRESH_INTERVAL_SECONDS = 300  # 5 minutes

_storage = JarvisStorage("calendar_alerts")


class CalendarAlertAgent(IJarvisAgent):
    """Background agent that monitors calendar for upcoming events."""

    def __init__(self) -> None:
        self._alerts: List[Alert] = []
        self._alerted_event_keys: set[str] = set()  # track already-alerted events

    @property
    def name(self) -> str:
        return "calendar_alerts"

    @property
    def description(self) -> str:
        return "Monitors calendar events and generates time-proximity alerts"

    @property
    def schedule(self) -> AgentSchedule:
        return AgentSchedule(
            interval_seconds=REFRESH_INTERVAL_SECONDS,
            run_on_startup=True,
        )

    @property
    def required_secrets(self) -> List[IJarvisSecret]:
        # At least one calendar provider must be configured
        return [
            JarvisSecret(
                "CALENDAR_TYPE",
                "Type of calendar service (icloud, google)",
                "integration",
                "string",
                required=False,
            ),
            JarvisSecret(
                "CALENDAR_USERNAME",
                "Username/Apple ID for calendar service",
                "integration",
                "string",
                required=False,
            ),
        ]

    def validate_secrets(self) -> List[str]:
        """Override: calendar credentials must be configured."""
        has_username = bool(_storage.get_secret("CALENDAR_USERNAME"))
        has_google = bool(_storage.get_secret("GOOGLE_ACCESS_TOKEN"))

        if not has_username and not has_google:
            return ["CALENDAR_USERNAME or GOOGLE_ACCESS_TOKEN"]
        return []

    @property
    def include_in_context(self) -> bool:
        return False

    async def run(self) -> None:
        """Fetch today's calendar events, generate alerts, and inject into CC memory."""
        try:
            try:
                from commands.get_calendar_events.command import ReadCalendarCommand
            except ImportError:
                from commands.custom_commands.get_calendar_events.command import ReadCalendarCommand

            cmd = ReadCalendarCommand()
            today = datetime.now().strftime("%Y-%m-%d")

            request_info = RequestInformation(
                voice_command="calendar check",
                conversation_id="calendar-alert-agent",
            )

            response = cmd.run(
                request_info,
                resolved_datetimes=[today],
            )

            if not response.success or not response.context_data:
                self._alerts = []
                return

            events = response.context_data.get("events", [])
            now = datetime.now(timezone.utc)
            self._alerts = []

            for event in events:
                self._process_event(event, now)

            # Inject events into CC memory for proactive voice context
            self._inject_memories(events)

        except Exception as e:
            logger.error("Calendar agent run failed", error=str(e))
            self._alerts = []

    def _process_event(self, event: Dict[str, Any], now: datetime) -> None:
        """Generate an alert if an event is within the alert window."""
        start_str = event.get("start_time") or event.get("start")
        title = event.get("title") or event.get("summary", "Untitled event")

        if not start_str:
            return

        try:
            # Parse ISO format
            if isinstance(start_str, str):
                start_str = start_str.replace("Z", "+00:00")
                event_start = datetime.fromisoformat(start_str)
                if event_start.tzinfo is None:
                    event_start = event_start.replace(tzinfo=timezone.utc)
            else:
                return
        except (ValueError, TypeError):
            return

        minutes_until = (event_start - now).total_seconds() / 60

        # Only alert for future events within 60 minutes
        if minutes_until < 0 or minutes_until > 60:
            return

        # Dedup: don't re-alert for the same event at the same proximity level
        if minutes_until <= 15:
            event_key = f"{title}:15min"
            priority = 3
            ttl = timedelta(minutes=15)
            time_desc = f"in {int(minutes_until)} minutes" if minutes_until > 1 else "starting now"
        else:
            event_key = f"{title}:60min"
            priority = 2
            ttl = timedelta(minutes=30)
            time_desc = f"in about {int(minutes_until)} minutes"

        if event_key in self._alerted_event_keys:
            return

        self._alerted_event_keys.add(event_key)

        self._alerts.append(Alert(
            source_agent=self.name,
            title=f"Upcoming: {title}",
            summary=f"{title} {time_desc}",
            created_at=now,
            expires_at=now + ttl,
            priority=priority,
        ))

    def get_context_data(self) -> Dict[str, Any]:
        return {}

    def get_alerts(self) -> List[Alert]:
        return list(self._alerts)

    def _inject_memories(self, events: List[Dict[str, Any]]) -> None:
        """Push calendar events into CC memory system for proactive context."""
        try:
            from clients.rest_client import RestClient
        except ImportError:
            logger.debug("RestClient not available — skipping memory injection")
            return

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        memories = []

        for event in events:
            title = event.get("title") or event.get("summary", "Untitled event")
            start_str = event.get("start_time") or event.get("start", "")
            end_str = event.get("end_time") or event.get("end", "")
            location = event.get("location", "")
            is_all_day = event.get("is_all_day", False)
            event_id = event.get("id", title)

            # Format as natural language
            if is_all_day:
                content = f"{title} (all day)"
            else:
                # Format times for readability
                start_display = start_str
                end_display = end_str
                try:
                    if isinstance(start_str, str):
                        s = start_str.replace("Z", "+00:00")
                        start_dt = datetime.fromisoformat(s)
                        start_display = start_dt.strftime("%I:%M%p")
                    if isinstance(end_str, str):
                        e = end_str.replace("Z", "+00:00")
                        end_dt = datetime.fromisoformat(e)
                        end_display = end_dt.strftime("%I:%M%p")
                except (ValueError, TypeError):
                    pass
                content = f"{start_display} - {end_display}: {title}"

            if location:
                content += f" at {location}"

            # TTL: expire 1 hour after event end, or 24h for all-day
            ttl_hours = 24.0
            if not is_all_day and end_str:
                try:
                    e_str = end_str.replace("Z", "+00:00") if isinstance(end_str, str) else ""
                    end_dt = datetime.fromisoformat(e_str)
                    hours_until_end = (end_dt - datetime.now(timezone.utc)).total_seconds() / 3600
                    ttl_hours = max(1.0, hours_until_end + 1.0)
                except (ValueError, TypeError):
                    pass

            memories.append({
                "content": content,
                "category": "calendar",
                "key": f"calendar:{today}:{event_id}",
                "ttl_hours": ttl_hours,
                "source": "calendar-agent",
            })

        if memories:
            result = RestClient.inject_memories(memories)
            if result:
                logger.info(
                    "Calendar agent injected memories",
                    count=result.get("injected", 0) + result.get("updated", 0),
                )
