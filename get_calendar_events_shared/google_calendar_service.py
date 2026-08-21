"""Google Calendar REST client using OAuth2 Bearer tokens.

Thin async-free wrapper around the Google Calendar v3 API.
Same interface as ICloudCalendarService: list_events via read_events().
On 401, flags re-auth so the mobile app prompts the user.
"""

from datetime import datetime, timedelta
from typing import List

import httpx

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

from get_calendar_events_shared.icloud_calendar_service import CalendarEvent

logger = JarvisLogger(service="jarvis-node")

BASE_URL = "https://www.googleapis.com/calendar/v3"


def _node_local_tz():
    """The node's local timezone, or None off a node / when unresolvable."""
    try:
        from zoneinfo import ZoneInfo

        from utils.timezone_util import get_user_timezone
        return ZoneInfo(get_user_timezone())
    except Exception:  # noqa: BLE001 — not on a node / tz lookup failed
        return None


class GoogleCalendarService:
    """REST client for Google Calendar v3 API."""

    def __init__(
        self,
        access_token: str,
        refresh_token: str,
        client_id: str,
        calendar_id: str = "primary",
    ) -> None:
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.client_id = client_id
        self.calendar_id = calendar_id

    def read_events(self, date: datetime | None = None, look_ahead_days: int = 1) -> List[CalendarEvent]:
        """Fetch events from Google Calendar for a date range.

        Args:
            date: Start date (default: now).
            look_ahead_days: Number of days to fetch.

        Returns:
            List of CalendarEvent objects.
        """
        if date is None:
            date = datetime.now()

        time_min = date.strftime("%Y-%m-%dT00:00:00Z")
        time_max = (date + timedelta(days=look_ahead_days)).strftime("%Y-%m-%dT00:00:00Z")

        params = {
            "timeMin": time_min,
            "timeMax": time_max,
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": "50",
        }

        try:
            response = httpx.get(
                f"{BASE_URL}/calendars/{self.calendar_id}/events",
                params=params,
                headers={"Authorization": f"Bearer {self.access_token}"},
                timeout=15.0,
            )

            if response.status_code == 401:
                logger.warning("Google Calendar returned 401 — flagging re-auth")
                self._flag_reauth()
                return []

            response.raise_for_status()
            data = response.json()
            return self._parse_events(data.get("items", []))

        except httpx.HTTPStatusError as e:
            logger.error("Google Calendar API error", status_code=e.response.status_code, detail=str(e))
            return []
        except Exception as e:
            logger.error("Google Calendar request failed", error=str(e))
            return []

    def create_event(self, event_data: dict) -> bool:
        """Create an event on Google Calendar via the v3 API.

        Mirrors ICloudCalendarService.add_event's contract — same event_data
        keys (``summary``, ``start_time``, ``end_time``, optional ``location`` /
        ``description``) — so the add_event command can call either backend with
        one mapped dict.

        NOTE (write scope / re-consent): this needs the
        ``https://www.googleapis.com/auth/calendar.events`` OAuth scope. The read
        command previously requested only ``calendar.readonly``; users who
        authenticated before the scope bump must re-consent before writes
        succeed (a stale read-only token 401s here → _flag_reauth).

        TODO: naive datetimes serialize without an offset; if a household reports
        events landing in the wrong timezone, thread a "timeZone" field through
        from the caller. iCloud (the primary backend) is unaffected.

        Returns True on 200/201.
        """
        start_time = event_data.get("start_time")
        end_time = event_data.get("end_time")
        if start_time is None:
            logger.error("Google Calendar create_event missing start_time")
            return False
        if end_time is None:
            end_time = start_time + timedelta(hours=1)

        body: dict = {
            "summary": event_data.get("summary", "New Event"),
            "start": {"dateTime": start_time.isoformat()},
            "end": {"dateTime": end_time.isoformat()},
        }
        if event_data.get("location"):
            body["location"] = event_data["location"]
        if event_data.get("description"):
            body["description"] = event_data["description"]

        try:
            response = httpx.post(
                f"{BASE_URL}/calendars/{self.calendar_id}/events",
                json=body,
                headers={"Authorization": f"Bearer {self.access_token}"},
                timeout=15.0,
            )

            if response.status_code == 401:
                logger.warning("Google Calendar returned 401 on create — flagging re-auth")
                self._flag_reauth()
                return False

            response.raise_for_status()
            return response.status_code in (200, 201)

        except httpx.HTTPStatusError as e:
            logger.error("Google Calendar create_event API error", status_code=e.response.status_code, detail=str(e))
            return False
        except Exception as e:
            logger.error("Google Calendar create_event failed", error=str(e))
            return False

    def _parse_events(self, items: list[dict]) -> List[CalendarEvent]:
        """Convert Google Calendar API items to CalendarEvent objects."""
        events: list[CalendarEvent] = []
        for item in items:
            try:
                start_raw = item.get("start", {})
                end_raw = item.get("end", {})

                is_all_day = "date" in start_raw and "dateTime" not in start_raw

                if is_all_day:
                    start_time = datetime.strptime(start_raw["date"], "%Y-%m-%d")
                    end_time = datetime.strptime(end_raw.get("date", start_raw["date"]), "%Y-%m-%d")
                else:
                    start_time = self._parse_google_datetime(start_raw.get("dateTime", ""))
                    end_time = self._parse_google_datetime(end_raw.get("dateTime", ""))

                if not start_time or not end_time:
                    continue

                events.append(CalendarEvent(
                    id=item.get("id", ""),
                    summary=item.get("summary", "No Title"),
                    start_time=start_time,
                    end_time=end_time,
                    location=item.get("location"),
                    description=item.get("description"),
                    is_all_day=is_all_day,
                ))
            except Exception as e:
                logger.debug("Skipping unparseable Google Calendar event", error=str(e))
                continue
        return events

    @staticmethod
    def _parse_google_datetime(dt_str: str) -> datetime | None:
        """Parse an RFC 3339 datetime from Google to the node's LOCAL wall-clock
        (NAIVE).

        Google encodes the true instant with an offset ("...-07:00") or as UTC
        ("...Z"). We parse that instant, convert it to the node's local timezone,
        and drop tzinfo — so the rest of the calendar stack (which assumes
        naive == local wall-clock) and the command's ``_event_iso`` tz-attach yield
        the CORRECT absolute time. The old ``.replace(tzinfo=None)`` kept the raw
        wall-clock (UTC for a "Z" event, or a foreign offset for a cross-tz event),
        which ``_event_iso`` then mis-stamped with the node tz → the leave-by
        reminder fired hours off.
        """
        if not dt_str:
            return None
        # Strip the colon in a "+HH:MM"/"-HH:MM" offset for %z ("Z" parses directly).
        if len(dt_str) >= 6 and dt_str[-3] == ":" and dt_str[-6] in "+-":
            dt_str = dt_str[:-3] + dt_str[-2:]
        try:
            aware = datetime.strptime(dt_str, "%Y-%m-%dT%H:%M:%S%z")
        except ValueError:
            return None
        local = _node_local_tz()
        if local is not None:
            return aware.astimezone(local).replace(tzinfo=None)
        # Node tz unresolvable (non-node / tests): keep the instant's own wall-clock.
        return aware.replace(tzinfo=None)

    @staticmethod
    def _flag_reauth() -> None:
        """Flag the google_calendar provider as needing re-authentication."""
        try:
            from services.command_auth_service import set_needs_auth
            set_needs_auth("google_calendar", "401 Unauthorized from Google Calendar API")
        except Exception:
            pass
