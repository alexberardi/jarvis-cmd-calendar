# jarvis-cmd-calendar

Calendar integration for [Jarvis](https://github.com/alexberardi/jarvis-node-setup). Supports iCloud CalDAV and Google Calendar.

## Install

```bash
python scripts/command_store.py install --url https://github.com/alexberardi/jarvis-cmd-calendar
```

## Voice Commands

- "What's on my calendar today?"
- "Show me my schedule for tomorrow"
- "What appointments do I have this weekend?"
- "Do I have any meetings?"
- "What's on my calendar the day after tomorrow?"

## Components

| Component | Type | Description |
|-----------|------|-------------|
| get_calendar_events | command | Read calendar events for any date range |
| calendar_alerts | agent | Alerts for upcoming events (15min and 60min windows) |

## Providers

| Provider | Auth | Setup |
|----------|------|-------|
| iCloud | App-specific password | Set CALENDAR_USERNAME + CALENDAR_PASSWORD |
| Google | OAuth2 | Authenticate via mobile app |

## Structure

```
commands/get_calendar_events/command.py   # Voice command
agents/calendar_alerts/agent.py           # Upcoming event alerts
lib/icloud_calendar_service.py            # iCloud CalDAV client
lib/google_calendar_service.py            # Google Calendar API
lib/date_util.py                          # Date parsing utilities
```
