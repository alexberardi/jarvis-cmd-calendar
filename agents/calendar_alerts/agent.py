"""CalendarAlertAgent — monitors calendar events and generates time-proximity alerts.

Runs every 5 minutes. Produces alerts based on how soon events are:
- Event in <=15 min -> priority 3, TTL 15 min
- Event in <=60 min -> priority 2, TTL 30 min

Also injects today's events into the command center's memory system so
Jarvis has proactive awareness of the user's schedule.

Calendar credentials are USER-scoped, and agents run with no ambient user in
the SDK ContextVar — so the agent resolves which users to run for via
get_calendar_events_shared.user_resolution and iterates them, setting the ContextVar
per user. The optional CALENDAR_AGENT_USER identity pins the agent to one
household member.
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
    set_current_user_id,
)

from get_calendar_events_shared.user_resolution import (
    find_configured_user_ids,
    resolve_agent_user_ids,
)

logger = JarvisLogger(service="jarvis-node")

REFRESH_INTERVAL_SECONDS = 300  # 5 minutes
MAX_USERS_PER_RUN = 5

_storage = JarvisStorage("calendar_alerts")


class CalendarAlertAgent(IJarvisAgent):
    """Background agent that monitors calendar for upcoming events."""

    def __init__(self) -> None:
        self._alerts: List[Alert] = []
        # Already-alerted events: dedup key -> event start time. Pruned each
        # run once the event is well past, so recurring events (same title,
        # later instance) can alert again and the map can't grow unbounded.
        self._alerted_event_keys: dict[str, datetime] = {}
        # One command instance per user, reused across runs so each user's
        # cached iCloud/Google service survives, instead of doing a full
        # re-auth every 5 minutes.
        self._cmds: dict[int, Any] = {}

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
                "user",
                "string",
                required=False,
            ),
            JarvisSecret(
                "CALENDAR_USERNAME",
                "Username/Apple ID for calendar service",
                "user",
                "string",
                required=False,
            ),
            JarvisSecret(
                "CALENDAR_AGENT_USER",
                "The household member whose calendar this agent watches. "
                "Leave unset to watch every member with a configured calendar.",
                "integration",
                "user",
                required=False,
                is_sensitive=False,
                friendly_name="Watch user",
            ),
        ]

    def validate_secrets(self) -> List[str]:
        """Agent requires at least one user with usable calendar credentials.

        Calendar secrets are user-scoped and agent discovery runs with no
        ambient user in the SDK ContextVar, so enumerate configured users
        node-side instead of relying on ContextVar-resolved reads (which
        always return None in this context).
        """
        if find_configured_user_ids():
            return []
        # No configured user (or not on a node) — fall back to ambient reads
        # so non-node contexts still report something sensible.
        has_username = bool(_storage.get_secret("CALENDAR_USERNAME", scope="user"))
        has_google = bool(_storage.get_secret("GOOGLE_ACCESS_TOKEN", scope="user"))

        if not has_username and not has_google:
            return ["CALENDAR_USERNAME or GOOGLE_ACCESS_TOKEN"]
        return []

    @property
    def include_in_context(self) -> bool:
        return False

    def _command_for_user(self, uid: int) -> Any:
        if uid not in self._cmds:
            try:
                from commands.get_calendar_events.command import ReadCalendarCommand
            except ImportError:
                from commands.custom_commands.get_calendar_events.command import ReadCalendarCommand
            self._cmds[uid] = ReadCalendarCommand()
        return self._cmds[uid]

    def _evict_stale_commands(self, active_uids: set[int]) -> None:
        """Drop cached commands for users no longer resolved (left household,
        deconfigured, or pinned away) so their authenticated sessions and
        credentials don't stay resident forever."""
        for uid in [u for u in self._cmds if u not in active_uids]:
            cmd = self._cmds.pop(uid)
            close = getattr(getattr(getattr(cmd, "_cached_service", None), "session", None), "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass

    def _prune_alerted_keys(self, now: datetime) -> None:
        """Forget dedup keys for events that started over 2 hours ago — far
        outside the 60-minute alert window, so the next instance of a
        recurring event alerts again."""
        cutoff = now - timedelta(hours=2)
        for key in [k for k, start in self._alerted_event_keys.items() if start < cutoff]:
            del self._alerted_event_keys[key]

    def _run_for_user(self, uid: int, today: str, now: datetime) -> None:
        """One user's fetch + alerts + memory injection. Raises on failure —
        the caller contains it so other users still run."""
        request_info = RequestInformation(
            voice_command="calendar check",
            conversation_id="calendar-alert-agent",
            user_id=uid,
        )
        response = self._command_for_user(uid).run(
            request_info,
            resolved_datetimes=[today],
        )

        if not response.success or not response.context_data:
            return

        events = response.context_data.get("events", [])
        for event in events:
            self._process_event(event, now, uid)

        # Inject events into CC memory for proactive voice context
        self._inject_memories(events, uid)

    async def run(self) -> None:
        """Fetch today's calendar events per configured user, generate alerts,
        and inject into CC memory. One user's failure must not affect the
        others, so each iteration is contained."""
        # The module-level find_configured_user_ids is passed explicitly
        # so the CALENDAR_AGENT_USER identity check sees the same lookup
        # tests patch on this module.
        user_ids = resolve_agent_user_ids(find_configured_user_ids)
        if not user_ids:
            logger.debug("Calendar agent: no users with a configured calendar")
            self._alerts = []
            return

        today = datetime.now().strftime("%Y-%m-%d")
        now = datetime.now(timezone.utc)
        self._alerts = []
        self._prune_alerted_keys(now)
        active = set(user_ids[:MAX_USERS_PER_RUN])
        self._evict_stale_commands(active)

        for uid in user_ids[:MAX_USERS_PER_RUN]:
            # Set the SDK user ContextVar so the command's user-scope
            # secret reads resolve this user's calendar credentials.
            set_current_user_id(uid)
            try:
                self._run_for_user(uid, today, now)
            except Exception as e:
                logger.error(
                    "Calendar agent run failed for user", user_id=uid, error=str(e)
                )
            finally:
                set_current_user_id(None)

    def _process_event(self, event: Dict[str, Any], now: datetime, uid: int) -> None:
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

        # Dedup: don't re-alert for the same event at the same proximity
        # level. Keyed per user (two members can have same-named events) and
        # per event start (recurring events alert again on later instances).
        if minutes_until <= 15:
            event_key = f"{uid}:{title}:{event_start.isoformat()}:15min"
            priority = 3
            ttl = timedelta(minutes=15)
            time_desc = f"in {int(minutes_until)} minutes" if minutes_until > 1 else "starting now"
        else:
            event_key = f"{uid}:{title}:{event_start.isoformat()}:60min"
            priority = 2
            ttl = timedelta(minutes=30)
            time_desc = f"in about {int(minutes_until)} minutes"

        if event_key in self._alerted_event_keys:
            return

        self._alerted_event_keys[event_key] = event_start

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

    def _inject_memories(self, events: List[Dict[str, Any]], uid: int) -> None:
        """Push calendar events into CC memory system for proactive context.

        Memories are attributed to the calendar's owner (user_id), and the
        dedup key includes the uid so two members' same-id events don't
        overwrite each other.
        """
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
                "key": f"calendar:{today}:{uid}:{event_id}",
                "ttl_hours": ttl_hours,
                "source": "calendar-agent",
                "user_id": uid,
            })

        if memories:
            result = RestClient.inject_memories(memories)
            if result:
                logger.info(
                    "Calendar agent injected memories",
                    count=result.get("injected", 0) + result.get("updated", 0),
                )
