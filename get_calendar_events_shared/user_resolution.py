"""Resolve which users the background calendar agent runs for.

ALL personal calendar secrets (CALENDAR_TYPE, CALENDAR_USERNAME/PASSWORD,
GOOGLE_*_TOKEN) are USER-scoped, and background agents run with no user in
the SDK ContextVar — every JarvisStorage user-scope read returns None in
agent/discovery context. So the agent enumerates the user-scope secret rows
node-side and checks each candidate user's credentials explicitly.

On top of that auto-discovery sits the optional CALENDAR_AGENT_USER identity
(integration scope, value_type "user" — the mobile app renders a
household-member picker and stores the selected member's user id as a
string): when set, the agent watches exactly that member's calendar, so a
multi-user household never gets alerts fanned to everyone or to the wrong
person. An EXPLICIT identity must never silently fall back to other users —
a misconfigured value means the agent idles (with a warning).
"""

from collections.abc import Callable

try:
    from jarvis_log_client import JarvisLogger
    logger = JarvisLogger(service="jarvis-node")
except ImportError:
    import logging
    logger = logging.getLogger("jarvis-cmd-calendar")

from jarvis_command_sdk import JarvisStorage

# Integration scope — readable in agent context (no ambient user required).
_storage = JarvisStorage("get_calendar_events")

AGENT_USER_KEY = "CALENDAR_AGENT_USER"

# Any of these rows existing for a user marks them as a calendar candidate.
_CANDIDATE_KEYS = {"CALENDAR_TYPE", "CALENDAR_USERNAME", "GOOGLE_ACCESS_TOKEN"}


def find_configured_user_ids() -> list[int]:
    """User ids with a usable calendar config. Agent context has no ambient
    user, so enumerate user-scope secret rows node-side."""
    try:
        from services.secret_service import get_all_secrets, get_secret_value
    except ImportError:
        return []  # not on a node (e.g. Pantry container test)

    try:
        candidates: set[int] = set()
        for row in get_all_secrets("user"):
            if row.key in _CANDIDATE_KEYS and row.user_id is not None:
                candidates.add(row.user_id)

        usable: list[int] = []
        for uid in candidates:
            calendar_type = (
                get_secret_value("CALENDAR_TYPE", "user", user_id=uid) or ""
            ).strip().lower() or "icloud"
            if calendar_type == "google":
                if get_secret_value("GOOGLE_ACCESS_TOKEN", "user", user_id=uid):
                    usable.append(uid)
            elif calendar_type == "icloud":
                # Match the command's run() branch: iCloud needs both creds.
                if get_secret_value(
                    "CALENDAR_USERNAME", "user", user_id=uid
                ) and get_secret_value("CALENDAR_PASSWORD", "user", user_id=uid):
                    usable.append(uid)
            # Any other CALENDAR_TYPE is unsupported by the command — skip.
        return sorted(usable)
    except Exception as e:  # never raise from agent discovery
        logger.warning("Calendar user resolution failed", error=str(e))
        return []


def configured_agent_user_id() -> int | None:
    """The explicit CALENDAR_AGENT_USER identity as an int, or None.

    None when the secret is unset/blank OR doesn't parse as a user id —
    callers use this only for TARGETING; whether the agent runs at all is
    resolve_agent_user_ids()'s call.
    """
    raw = (_storage.get_secret(AGENT_USER_KEY) or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def resolve_agent_user_ids(
    configured: Callable[[], list[int]] | None = None,
) -> list[int]:
    """User ids the background agent should run for.

    CALENDAR_AGENT_USER set → exactly that user, and ONLY when they have a
    configured calendar: an explicit identity must never silently fall back
    to other users, so an unparseable value or one without a calendar means
    [] (agent idles) plus a warning. Unset/blank → the auto behavior, every
    calendar-configured user — zero-config single-user households are
    unchanged.

    ``configured`` overrides the calendar lookup; the agent passes its own
    module-level ``find_configured_user_ids`` reference so tests that patch
    it on the agent module keep working.
    """
    lookup = configured if configured is not None else find_configured_user_ids
    raw = (_storage.get_secret(AGENT_USER_KEY) or "").strip()
    if not raw:
        return lookup()
    try:
        uid = int(raw)
    except ValueError:
        logger.warning(
            f"CALENDAR_AGENT_USER is set to {raw!r} but that isn't a user id — agent idle"
        )
        return []
    if uid in lookup():
        return [uid]
    logger.warning(
        f"CALENDAR_AGENT_USER is set to {uid} but that user has no configured "
        "calendar — agent idle"
    )
    return []
