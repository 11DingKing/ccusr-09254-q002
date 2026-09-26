"""更正事件归属、跨日拆分、超额扣减与异常审计的核心测试。"""

from __future__ import annotations

from datetime import datetime, timezone

from app.core.replay import (
    AdjustmentTarget,
    AnomalyCode,
    Event,
    EventType,
    replay,
)


def _event(event_id, event_type, student_id, payload, plan_version="P1"):
    return Event(
        event_id=event_id,
        plan_version=plan_version,
        event_type=event_type,
        student_id=student_id,
        payload=payload,
        created_at=datetime.now(timezone.utc),
    )


def _checkin(eid, student, start, end, *, activity_type="regular", activity_id="A1"):
    return _event(
        eid,
        EventType.CHECKIN,
        student,
        {
            "activity_id": activity_id,
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


def test_checkin_linked_correction_deducts_from_the_business_day():
    events = [
        _checkin(
            "E-01", "S1",
            "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00",
        ),
        _correction("E-02", "S1", -1800, checkin_event_id="E-01"),
    ]
    progress = replay(
        events, plan_version="P1", timezone_name="Asia/Shanghai", required_seconds=0
    ).students["S1"]
    assert progress.confirmed_seconds == 7200
    assert progress.allocated_adjustment_seconds == -1800
    assert progress.total_seconds == 5400
    assert _daily_map(progress) == {"2024-03-15": 5400}
    assert sum(d.seconds for d in progress.daily) == progress.total_seconds
    adj = progress.adjustments[0]
    assert adj.target_type == AdjustmentTarget.CHECKIN
    assert adj.is_fully_applied
    assert adj.allocations[0].academic_day == "2024-03-15"
    assert not progress.anomalies


def test_checkin_linked_correction_splits_across_midnight():
    # 22:00-02:00 across two days (7200 each); a -3h correction eats the
    # whole first day plus one hour of the second.
    events = [
        _checkin(
            "E-01", "S1",
            "2024-03-15T22:00:00+08:00", "2024-03-16T02:00:00+08:00",
        ),
        _correction("E-02", "S1", -3 * 3600, checkin_event_id="E-01"),
    ]
    progress = replay(
        events, plan_version="P1", timezone_name="Asia/Shanghai", required_seconds=0
    ).students["S1"]
    assert _daily_map(progress) == {"2024-03-15": 0, "2024-03-16": 3600}
    assert progress.total_seconds == 3600
    assert sum(d.seconds for d in progress.daily) == 3600
    allocations = {
        a.academic_day: a.seconds
        for a in progress.adjustments[0].allocations
    }
    assert allocations == {"2024-03-15": -7200, "2024-03-16": -3600}


def test_correction_referencing_later_checkin_resolves_out_of_order():
    # The correction's event id sorts before its target check-in; a replay
    # must still resolve the reference and keep totals/daily consistent.
    events = [
        _checkin(
            "E-10", "S1",
            "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00",
        ),
        _correction("E-05", "S1", -900, checkin_event_id="E-10"),
    ]
    progress = replay(
        events, plan_version="P1", timezone_name="Asia/Shanghai", required_seconds=0
    ).students["S1"]
    assert progress.total_seconds == 7200 - 900
    assert _daily_map(progress) == {"2024-03-15": 6300}
    assert not progress.anomalies


def test_day_linked_over_deduction_never_makes_day_negative():
    # Two hours on 03-15; a -5h correction targeting that day must floor the
    # day at zero, apply only -2h, and leave 3h unapplied in an anomaly.
    events = [
        _checkin(
            "E-01", "S1",
            "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00",
        ),
        _checkin(
            "E-03", "S1",
            "2024-03-16T08:00:00+08:00", "2024-03-16T10:00:00+08:00",
        ),
        _correction("E-02", "S1", -5 * 3600, academic_day="2024-03-15"),
    ]
    progress = replay(
        events, plan_version="P1", timezone_name="Asia/Shanghai", required_seconds=0
    ).students["S1"]
    days = _daily_map(progress)
    assert days["2024-03-15"] == 0
    assert days["2024-03-16"] == 7200
    assert progress.total_seconds == 7200
    assert all(d.seconds >= 0 for d in progress.daily)
    assert sum(d.seconds for d in progress.daily) == progress.total_seconds

    adj = progress.adjustments[0]
    assert adj.seconds == -5 * 3600
    assert adj.applied_seconds == -7200
    anomaly = progress.anomalies[0]
    assert anomaly.code == AnomalyCode.EXCEEDS_TARGET.value
    assert anomaly.detail["unapplied_seconds"] == 3 * 3600


def test_multiple_corrections_apply_in_sequence_and_clamp_independently():
    events = [
        _checkin(
            "E-01", "S1",
            "2024-03-15T08:00:00+08:00", "2024-03-15T12:00:00+08:00",
        ),
        _correction("E-02", "S1", -1800, academic_day="2024-03-15"),
        _correction("E-03", "S1", -1800, academic_day="2024-03-15"),
        _correction("E-04", "S1", -4 * 3600, academic_day="2024-03-15"),
    ]
    progress = replay(
        events, plan_version="P1", timezone_name="Asia/Shanghai", required_seconds=0
    ).students["S1"]
    assert _daily_map(progress) == {"2024-03-15": 0}
    assert progress.total_seconds == 0
    # The first two corrections fully apply; the third only consumes the
    # remaining three hours and raises an anomaly.
    applied = [a.applied_seconds for a in progress.adjustments]
    assert applied == [-1800, -1800, -10800]
    assert [a.code for a in progress.anomalies] == [
        AnomalyCode.EXCEEDS_TARGET.value
    ]


def test_checkin_linked_deduction_capped_by_that_checkins_duration_with_overlap():
    # A 8-10 and B 9-11 union to 3 hours. Deducting 2h against A and 2h
    # against B: A's -2h removes 8-10; B may only remove its own remaining
    # hour (10-11), the extra hour is an anomaly rather than going negative.
    events = [
        _checkin(
            "E-01", "S1",
            "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00",
        ),
        _checkin(
            "E-02", "S1",
            "2024-03-15T09:00:00+08:00", "2024-03-15T11:00:00+08:00",
        ),
        _correction("E-03", "S1", -2 * 3600, checkin_event_id="E-01"),
        _correction("E-04", "S1", -2 * 3600, checkin_event_id="E-02"),
    ]
    progress = replay(
        events, plan_version="P1", timezone_name="Asia/Shanghai", required_seconds=0
    ).students["S1"]
    assert _daily_map(progress) == {"2024-03-15": 0}
    assert progress.total_seconds == 0
    last = progress.adjustments[-1]
    assert last.applied_seconds == -3600
    assert last.anomaly_codes == [AnomalyCode.EXCEEDS_TARGET.value]


def test_positive_day_linked_correction_creates_that_day():
    events = [
        _checkin(
            "E-01", "S1",
            "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00",
        ),
        _correction("E-02", "S1", 3600, academic_day="2024-03-20"),
    ]
    progress = replay(
        events, plan_version="P1", timezone_name="Asia/Shanghai", required_seconds=0
    ).students["S1"]
    assert _daily_map(progress) == {"2024-03-15": 7200, "2024-03-20": 3600}
    assert progress.total_seconds == 10800


def test_missing_checkin_target_is_audited_and_not_applied():
    events = [
        _checkin(
            "E-01", "S1",
            "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00",
        ),
        _correction("E-02", "S1", -600, checkin_event_id="E-99"),
    ]
    progress = replay(
        events, plan_version="P1", timezone_name="Asia/Shanghai", required_seconds=0
    ).students["S1"]
    assert progress.total_seconds == 7200
    assert _daily_map(progress) == {"2024-03-15": 7200}
    adj = progress.adjustments[0]
    assert adj.applied_seconds == 0
    assert adj.anomaly_codes == [AnomalyCode.TARGET_MISSING.value]
    assert progress.anomalies[0].detail["checkin_event_id"] == "E-99"


def test_pending_internship_target_is_audited_and_not_applied():
    events = [
        _checkin(
            "E-01", "S1",
            "2024-03-15T08:00:00+08:00", "2024-03-15T12:00:00+08:00",
            activity_type="internship",
        ),
        _correction("E-02", "S1", -600, checkin_event_id="E-01"),
    ]
    progress = replay(
        events, plan_version="P1", timezone_name="Asia/Shanghai", required_seconds=0
    ).students["S1"]
    assert progress.total_seconds == 0
    assert progress.daily == []
    assert progress.adjustments[0].applied_seconds == 0
    assert progress.anomalies[0].code == AnomalyCode.TARGET_PENDING.value


def test_legacy_unattributed_correction_is_audited_and_best_effort_applied():
    events = [
        _checkin(
            "E-01", "S1",
            "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00",
        ),
        _checkin(
            "E-02", "S1",
            "2024-03-16T08:00:00+08:00", "2024-03-16T09:00:00+08:00",
        ),
        _correction("E-03", "S1", -3 * 3600),
    ]
    progress = replay(
        events, plan_version="P1", timezone_name="Asia/Shanghai", required_seconds=0
    ).students["S1"]
    # Spread across days in date order, never negative, total floored at zero.
    assert _daily_map(progress) == {"2024-03-15": 0, "2024-03-16": 0}
    assert progress.total_seconds == 0
    assert progress.anomalies[0].code == AnomalyCode.UNATTRIBUTED.value


def test_legacy_unattributed_correction_beyond_total_raises_exceeds_total():
    events = [
        _checkin(
            "E-01", "S1",
            "2024-03-15T08:00:00+08:00", "2024-03-15T09:00:00+08:00",
        ),
        _correction("E-02", "S1", -2 * 3600),
    ]
    progress = replay(
        events, plan_version="P1", timezone_name="Asia/Shanghai", required_seconds=0
    ).students["S1"]
    assert progress.total_seconds == 0
    assert _daily_map(progress) == {"2024-03-15": 0}
    codes = {a.code for a in progress.anomalies}
    assert AnomalyCode.EXCEEDS_TOTAL.value in codes


def test_target_for_other_student_is_treated_as_missing():
    events = [
        _checkin(
            "E-01", "S1",
            "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00",
        ),
        _correction("E-02", "S2", -600, checkin_event_id="E-01"),
    ]
    state = replay(
        events, plan_version="P1", timezone_name="Asia/Shanghai", required_seconds=0
    )
    s2 = state.students["S2"]
    assert s2.total_seconds == 0
    assert s2.anomalies[0].code == AnomalyCode.TARGET_MISSING.value
    assert s2.adjustments[0].applied_seconds == 0


def test_cutoff_before_target_checkin_marks_correction_unresolved():
    events = [
        _checkin(
            "E-10", "S1",
            "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00",
        ),
        _correction("E-05", "S1", -600, checkin_event_id="E-10"),
    ]
    past = replay(
        events,
        plan_version="P1",
        timezone_name="Asia/Shanghai",
        required_seconds=0,
        up_to_event_id="E-05",
    ).students["S1"]
    assert past.total_seconds == 0
    assert past.anomalies[0].code == AnomalyCode.TARGET_MISSING.value
    full = replay(
        events,
        plan_version="P1",
        timezone_name="Asia/Shanghai",
        required_seconds=0,
    ).students["S1"]
    assert full.total_seconds == 6600
    assert not full.anomalies
