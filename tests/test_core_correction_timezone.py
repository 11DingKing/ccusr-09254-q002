"""更正分摊在时区边界（跨日、DST）上的测试。"""

from __future__ import annotations

from datetime import datetime, timezone

from app.core.replay import Event, EventType, replay


def _event(event_id, event_type, student_id, payload):
    return Event(
        event_id=event_id,
        plan_version="P1",
        event_type=event_type,
        student_id=student_id,
        payload=payload,
        created_at=datetime.now(timezone.utc),
    )


def _checkin(eid, student, start, end, *, activity_type="regular"):
    return _event(
        eid,
        EventType.CHECKIN,
        student,
        {
            "activity_id": "A1",
            "activity_type": activity_type,
            "check_in_at": start,
            "check_out_at": end,
        },
    )


def _correction(eid, student, seconds, **target):
    return _event(
        eid,
        EventType.LEAVE_CORRECTION,
        student,
        {"adjustment_seconds": seconds, "reason": "r", **target},
    )


def _daily_map(progress):
    return {d.academic_day: d.seconds for d in progress.daily}


def test_shanghai_early_morning_correction_uses_local_date_not_utc():
    # 00:30-01:30 Shanghai is still the previous UTC day; attribution must
    # land on the local academic day.
    events = [
        _checkin(
            "E-01", "S1",
            "2024-03-15T00:30:00+08:00", "2024-03-15T01:30:00+08:00",
        ),
        _correction("E-02", "S1", -1800, academic_day="2024-03-15"),
    ]
    progress = replay(
        events, plan_version="P1", timezone_name="Asia/Shanghai", required_seconds=0
    ).students["S1"]
    assert _daily_map(progress) == {"2024-03-15": 1800}
    assert sum(d.seconds for d in progress.daily) == progress.total_seconds


def test_new_york_dst_fall_back_correction_uses_real_elapsed_seconds():
    # 2024-11-03 01:30 EDT -> 02:30 EST is TWO real hours; a checkin-linked
    # -1h correction removes exactly 3600 real seconds.
    events = [
        _checkin(
            "E-01", "S1",
            "2024-11-03T01:30:00-04:00", "2024-11-03T02:30:00-05:00",
        ),
        _correction("E-02", "S1", -3600, checkin_event_id="E-01"),
    ]
    progress = replay(
        events, plan_version="P1", timezone_name="America/New_York",
        required_seconds=0,
    ).students["S1"]
    assert progress.confirmed_seconds == 7200
    assert progress.total_seconds == 3600
    assert _daily_map(progress) == {"2024-11-03": 3600}


def test_new_york_dst_fall_back_overnight_split_sums_to_real_time():
    # 00:30 EDT -> 03:30 EST spans four real hours. Deduct 3h linked to the
    # check-in; one real hour remains, all attributed to Nov 3 (local date).
    events = [
        _checkin(
            "E-01", "S1",
            "2024-11-03T00:30:00-04:00", "2024-11-03T03:30:00-05:00",
        ),
        _correction("E-02", "S1", -3 * 3600, checkin_event_id="E-01"),
    ]
    progress = replay(
        events, plan_version="P1", timezone_name="America/New_York",
        required_seconds=0,
    ).students["S1"]
    assert progress.confirmed_seconds == 4 * 3600
    assert _daily_map(progress) == {"2024-11-03": 3600}
    assert sum(d.seconds for d in progress.daily) == progress.total_seconds


def test_new_york_dst_spring_forward_day_linked_correction():
    # 2024-03-10 is the spring-forward night. 09:00-11:00 EDT = two real
    # hours on the local academic day; a -90-minute correction leaves 30m.
    events = [
        _checkin(
            "E-01", "S1",
            "2024-03-10T09:00:00-04:00", "2024-03-10T11:00:00-04:00",
        ),
        _correction("E-02", "S1", -5400, academic_day="2024-03-10"),
    ]
    progress = replay(
        events, plan_version="P1", timezone_name="America/New_York",
        required_seconds=0,
    ).students["S1"]
    assert _daily_map(progress) == {"2024-03-10": 1800}
    assert progress.total_seconds == 1800


def test_utc_midnight_boundary_belongs_to_next_local_day():
    # 23:30-00:30 across two Shanghai days; both a day-linked correction on
    # day 1 and a checkin-linked correction must respect the split.
    events = [
        _checkin(
            "E-01", "S1",
            "2024-03-15T23:30:00+08:00", "2024-03-16T00:30:00+08:00",
        ),
        _correction("E-02", "S1", -1800, academic_day="2024-03-15"),
        _correction("E-03", "S1", -1800, checkin_event_id="E-01"),
    ]
    progress = replay(
        events, plan_version="P1", timezone_name="Asia/Shanghai", required_seconds=0
    ).students["S1"]
    # 1800 per day initially; E-02 zeroes day 1; E-03 then removes day 1's
    # remaining 0 (already gone) and 1800 from day 2.
    assert _daily_map(progress) == {"2024-03-15": 0, "2024-03-16": 0}
    assert progress.total_seconds == 0
