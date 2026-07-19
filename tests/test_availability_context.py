"""Free/busy derivation for the `availability` context operation.

Pure functions over event-shaped objects — no calendar account needed.
The consumer is the phone-call plan-draft step, which bakes the result
into a call brief the user reviews on the confirm card.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta

import pytest

from get_calendar_events_shared.availability import (
    Busy,
    build_availability,
    format_window,
    free_windows,
    merge_busy,
)


@dataclass
class _Event:
    start_time: datetime
    end_time: datetime
    summary: str = ""


MON = datetime(2026, 7, 20)  # a Monday


def _at(day_offset: int, hour: int, minute: int = 0) -> datetime:
    return MON + timedelta(days=day_offset, hours=hour, minutes=minute)


class TestFormatWindow:
    def test_same_meridiem_drops_redundant_suffix(self):
        assert format_window(_at(0, 14), _at(0, 17)) == "Mon 2-5pm"

    def test_crossing_noon_keeps_both(self):
        assert format_window(_at(0, 11), _at(0, 13)) == "Mon 11am-1pm"

    def test_half_hours_rendered(self):
        assert format_window(_at(0, 9, 30), _at(0, 10, 45)) == "Mon 9:30-10:45am"


class TestMergeBusy:
    def test_overlapping_spans_merge(self):
        merged = merge_busy(
            [
                Busy(_at(0, 10), _at(0, 12), "a"),
                Busy(_at(0, 11), _at(0, 13), "b"),
            ]
        )
        assert len(merged) == 1
        assert merged[0].start == _at(0, 10) and merged[0].end == _at(0, 13)

    def test_disjoint_spans_kept(self):
        merged = merge_busy(
            [Busy(_at(0, 9), _at(0, 10)), Busy(_at(0, 15), _at(0, 16))]
        )
        assert len(merged) == 2


class TestFreeWindows:
    def test_empty_day_is_one_full_window(self):
        free = free_windows([], MON, 1)
        assert free == [(datetime.combine(MON.date(), time(9, 0)),
                         datetime.combine(MON.date(), time(20, 0)))]

    def test_midday_meeting_splits_the_day(self):
        free = free_windows([Busy(_at(0, 12), _at(0, 13))], MON, 1)
        assert [format_window(s, e) for s, e in free] == ["Mon 9am-12pm", "Mon 1-8pm"]

    def test_short_gaps_are_not_offered(self):
        """A 15-minute slot is not a bookable appointment window."""
        free = free_windows(
            [Busy(_at(0, 9), _at(0, 12)), Busy(_at(0, 12, 15), _at(0, 20))], MON, 1
        )
        assert free == []

    def test_events_outside_waking_hours_ignored(self):
        free = free_windows([Busy(_at(0, 5), _at(0, 7))], MON, 1)
        assert [format_window(s, e) for s, e in free] == ["Mon 9am-8pm"]

    def test_multi_day_range(self):
        free = free_windows([], MON, 3)
        assert [format_window(s, e)[:3] for s, e in free] == ["Mon", "Tue", "Wed"]


class TestBuildAvailability:
    def test_free_and_busy_reported(self):
        out = build_availability(
            [_Event(_at(0, 12), _at(0, 13), "Lunch with Sam")], MON, 1
        )
        assert out["free"] == ["Mon 9am-12pm", "Mon 1-8pm"]
        assert out["busy"] == ["Mon 12-1pm (Lunch with Sam)"]

    def test_events_outside_range_excluded(self):
        out = build_availability([_Event(_at(9, 12), _at(9, 13), "Later")], MON, 2)
        assert out["busy"] == []

    def test_event_spanning_range_edge_is_clipped(self):
        """A multi-day event is clipped to the range and names both days."""
        out = build_availability(
            [_Event(_at(0, 10), _at(5, 10), "Vacation")], MON, 2
        )
        assert out["busy"] == ["Mon 10am-Wed 12am (Vacation)"]
        # Only the sliver before it starts remains bookable.
        assert out["free"] == ["Mon 9-10am"]

    def test_malformed_events_skipped_not_raised(self):
        """One bad event must not cost the user the whole envelope."""
        bad = _Event(start_time="not a datetime", end_time=None)  # type: ignore[arg-type]
        out = build_availability([bad, _Event(_at(0, 12), _at(0, 13))], MON, 1)
        assert out["busy"] == ["Mon 12-1pm"]

    def test_missing_end_time_assumes_one_hour(self):
        out = build_availability([_Event(_at(0, 14), None)], MON, 1)  # type: ignore[arg-type]
        assert out["busy"] == ["Mon 2-3pm"]

    def test_empty_calendar_is_all_free(self):
        out = build_availability([], MON, 1)
        assert out["busy"] == [] and out["free"] == ["Mon 9am-8pm"]


class TestCommandDeclaration:
    """The command advertises the op so the node handler can route to it."""

    def test_availability_declared_with_required_params(self):
        from commands.get_calendar_events.command import ReadCalendarCommand

        ops = ReadCalendarCommand().context_operations
        names = [op.name for op in ops]
        assert "availability" in names
        op = next(o for o in ops if o.name == "availability")
        assert op.missing_required({"start": "2026-07-20"}) == ["end"]

    def test_unknown_operation_fails_honestly(self):
        from commands.get_calendar_events.command import ReadCalendarCommand

        result = ReadCalendarCommand().execute_context_operation("inventory", {})
        assert not result.ok and "unsupported" in result.error

    def test_bad_date_range_rejected(self):
        from commands.get_calendar_events.command import ReadCalendarCommand

        result = ReadCalendarCommand().execute_context_operation(
            "availability", {"start": "nope", "end": "2026-07-27"}
        )
        assert not result.ok and "invalid date range" in result.error
