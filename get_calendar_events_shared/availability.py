"""Free/busy derivation for the ``availability`` context operation.

Turns raw calendar events into the compact envelope a server-side planner
bakes into a call brief ("Acceptable times: Thu 2-5pm; …"). Pure functions
over event tuples so they can be tested without a calendar account.

Waking-hours bounds are deliberate constants rather than settings: this
feeds a phone call to a business, and nobody wants Jarvis offering to book
a haircut at 6am because the calendar happened to be empty.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Iterable, Sequence

# Windows are only ever offered inside these hours, local time.
DAY_START = time(9, 0)
DAY_END = time(20, 0)

# Anything shorter isn't worth offering as an appointment slot.
MIN_WINDOW_MINUTES = 30

_DAY_FMT = "%a"  # Mon, Tue, …


@dataclass(frozen=True)
class Busy:
    """One occupied span, already clipped to the requested range."""

    start: datetime
    end: datetime
    summary: str = ""


def _fmt_time(dt: datetime) -> str:
    """4pm, 4:30pm — spoken-friendly, no leading zero."""
    hour = dt.hour % 12 or 12
    suffix = "am" if dt.hour < 12 else "pm"
    if dt.minute:
        return f"{hour}:{dt.minute:02d}{suffix}"
    return f"{hour}{suffix}"


def format_window(start: datetime, end: datetime) -> str:
    """'Thu 2-5pm' / 'Thu 11am-1pm' / 'Mon 10am-Wed 12am' — one window.

    A span crossing midnight names its end day: "Mon 10-12am" for a
    three-day vacation would read as a two-hour slot.
    """
    day = start.strftime(_DAY_FMT)
    if end.date() != start.date():
        return f"{day} {_fmt_time(start)}-{end.strftime(_DAY_FMT)} {_fmt_time(end)}"
    s_suffix = "am" if start.hour < 12 else "pm"
    e_suffix = "am" if end.hour < 12 else "pm"
    start_txt = _fmt_time(start)
    if s_suffix == e_suffix:
        # Drop the redundant meridiem on the start: "2-5pm", not "2pm-5pm".
        start_txt = start_txt[: -len(s_suffix)]
    return f"{day} {start_txt}-{_fmt_time(end)}"


def merge_busy(spans: Iterable[Busy]) -> list[Busy]:
    """Merge overlapping/adjacent busy spans so free-gap math is simple."""
    ordered = sorted(spans, key=lambda b: b.start)
    merged: list[Busy] = []
    for span in ordered:
        if merged and span.start <= merged[-1].end:
            last = merged[-1]
            if span.end > last.end:
                merged[-1] = Busy(last.start, span.end, last.summary or span.summary)
        else:
            merged.append(span)
    return merged


def free_windows(
    busy: Sequence[Busy],
    start_date: datetime,
    days: int,
    *,
    day_start: time = DAY_START,
    day_end: time = DAY_END,
    min_minutes: int = MIN_WINDOW_MINUTES,
) -> list[tuple[datetime, datetime]]:
    """Waking-hour gaps left over once busy spans are removed."""
    merged = merge_busy(busy)
    out: list[tuple[datetime, datetime]] = []

    for offset in range(max(days, 0)):
        day = (start_date + timedelta(days=offset)).date()
        cursor = datetime.combine(day, day_start)
        closes = datetime.combine(day, day_end)

        for span in merged:
            if span.end <= cursor or span.start >= closes:
                continue
            if span.start > cursor:
                gap_end = min(span.start, closes)
                if (gap_end - cursor) >= timedelta(minutes=min_minutes):
                    out.append((cursor, gap_end))
            cursor = max(cursor, min(span.end, closes))

        if closes - cursor >= timedelta(minutes=min_minutes):
            out.append((cursor, closes))

    return out


def build_availability(
    events: Iterable[object],
    start_date: datetime,
    days: int,
) -> dict[str, list[str]]:
    """The ``availability`` payload: rendered free windows + busy blocks.

    ``events`` are CalendarEvent-shaped (start_time/end_time/summary);
    all-day events occupy their whole day. Anything unparseable is skipped
    rather than raising — a single malformed event must not cost the user
    their whole envelope.
    """
    range_start = datetime.combine(start_date.date(), time.min)
    range_end = range_start + timedelta(days=max(days, 0))

    busy: list[Busy] = []
    for event in events:
        start = getattr(event, "start_time", None)
        end = getattr(event, "end_time", None)
        if not isinstance(start, datetime):
            continue
        if not isinstance(end, datetime) or end <= start:
            end = start + timedelta(hours=1)
        if end <= range_start or start >= range_end:
            continue
        busy.append(
            Busy(
                start=max(start, range_start),
                end=min(end, range_end),
                summary=str(getattr(event, "summary", "") or ""),
            )
        )

    merged = merge_busy(busy)
    free = free_windows(merged, range_start, days)

    return {
        "free": [format_window(s, e) for s, e in free],
        "busy": [
            f"{format_window(b.start, b.end)}{f' ({b.summary})' if b.summary else ''}"
            for b in merged
        ],
    }
