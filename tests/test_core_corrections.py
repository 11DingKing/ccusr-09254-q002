"""请假更正归属与每日明细一致性的核心重放测试。"""

from __future__ import annotations

from datetime import datetime, timezone

from app.core.replay import (
    AnomalyKind,
    Event,
    EventType,
    replay,
)


def _event(
    event_id: str,
    event_type: EventType,
    student_id: str,
    payload: dict,
    plan_version: str = "P1",
    created_at: datetime | None = None,
) -> Event:
    return Event(
        event_id=event_id,
        plan_version=plan_version,
        event_type=event_type,
        student_id=student_id,
        payload=payload,
        created_at=created_at or datetime(2024, 3, 20, 12, 0, tzinfo=timezone.utc),
    )


def _checkin(
    eid: str,
    student: str,
    start: str,
    end: str,
    *,
    activity_type: str = "regular",
    plan_version: str = "P1",
) -> Event:
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
        plan_version=plan_version,
    )


def _correction(
    eid: str,
    student: str,
    seconds: int,
    *,
    reason: str = "leave",
    business_date: str | None = None,
    checkin_event_id: str | None = None,
    plan_version: str = "P1",
) -> Event:
    payload: dict = {"adjustment_seconds": seconds, "reason": reason}
    if business_date is not None:
        payload["business_date"] = business_date
    if checkin_event_id is not None:
        payload["checkin_event_id"] = checkin_event_id
    return _event(
        eid, EventType.LEAVE_CORRECTION, student, payload, plan_version=plan_version
    )


def _replay(events, tz: str = "Asia/Shanghai", required: int = 0):
    return replay(
        events, plan_version="P1", timezone_name=tz, required_seconds=required
    )


def _daily_map(progress) -> dict[str, int]:
    return {d.academic_day: d.seconds for d in progress.daily}


def _assert_consistent(progress) -> None:
    """全局不变量：每日不为负，且日明细合计等于总量。"""
    assert all(d.seconds >= 0 for d in progress.daily)
    assert sum(d.seconds for d in progress.daily) == progress.total_seconds


def test_correction_with_business_date_deducts_that_day():
    events = [
        _checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00"),
        _checkin("E-02", "S1", "2024-03-16T08:00:00+08:00", "2024-03-16T10:00:00+08:00"),
        _correction("E-03", "S1", -3600, business_date="2024-03-15"),
    ]
    progress = _replay(events).students["S1"]
    assert progress.total_seconds == 4 * 3600 - 3600
    assert _daily_map(progress) == {
        "2024-03-15": 3600,
        "2024-03-16": 7200,
    }
    assert progress.anomalies == []
    adjustment = progress.adjustments[0]
    assert adjustment.attributed is True
    assert adjustment.business_date == "2024-03-15"
    _assert_consistent(progress)


def test_correction_linked_to_cross_midnight_checkin_splits_proportionally():
    # 22:00-02:00 跨午夜签到，两日各 7200 秒；扣 3600 按占比各半。
    events = [
        _checkin("E-01", "S1", "2024-03-15T22:00:00+08:00", "2024-03-16T02:00:00+08:00"),
        _correction("E-02", "S1", -3600, checkin_event_id="E-01"),
    ]
    progress = _replay(events).students["S1"]
    assert _daily_map(progress) == {
        "2024-03-15": 7200 - 1800,
        "2024-03-16": 7200 - 1800,
    }
    assert progress.total_seconds == 4 * 3600 - 3600
    assert progress.anomalies == []
    _assert_consistent(progress)


def test_correction_linked_to_checkin_uses_academic_days_in_plan_timezone():
    # 纽约时区跨午夜签到：当地 22:00 -> 次日 02:00。
    events = [
        _checkin(
            "E-01",
            "S1",
            "2024-03-15T22:00:00-04:00",
            "2024-03-16T02:00:00-04:00",
        ),
        _correction("E-02", "S1", -1800, checkin_event_id="E-01"),
    ]
    progress = _replay(events, tz="America/New_York").students["S1"]
    assert _daily_map(progress) == {
        "2024-03-15": 7200 - 900,
        "2024-03-16": 7200 - 900,
    }
    _assert_consistent(progress)


def test_excess_deduction_clamps_day_to_zero_and_spills_to_other_days():
    # 指定日期只有 3600 秒，扣 5400：当天扣到 0，剩余溢出到另一天。
    events = [
        _checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T09:00:00+08:00"),
        _checkin("E-02", "S1", "2024-03-16T08:00:00+08:00", "2024-03-16T10:00:00+08:00"),
        _correction("E-03", "S1", -5400, business_date="2024-03-15"),
    ]
    progress = _replay(events).students["S1"]
    assert _daily_map(progress) == {
        "2024-03-15": 0,
        "2024-03-16": 7200 - 1800,
    }
    assert progress.total_seconds == 3 * 3600 - 5400
    # 总量足够覆盖扣减，没有丢弃，不产生超额异常。
    assert progress.anomalies == []
    _assert_consistent(progress)


def test_deduction_beyond_all_confirmed_seconds_is_discarded_with_anomaly():
    events = [
        _checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T09:00:00+08:00"),
        _correction("E-02", "S1", -99999, business_date="2024-03-15"),
    ]
    progress = _replay(events).students["S1"]
    assert progress.total_seconds == 0
    assert _daily_map(progress) == {"2024-03-15": 0}
    overflow = [a for a in progress.anomalies if a.kind == AnomalyKind.CORRECTION_OVERFLOW]
    assert len(overflow) == 1
    assert overflow[0].event_id == "E-02"
    assert overflow[0].seconds == 99999 - 3600
    _assert_consistent(progress)


def test_unattributed_legacy_correction_deducts_from_earliest_day():
    # 历史记录没有归属字段：从最早有量日期开始扣，并记入可审计异常。
    events = [
        _checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T09:00:00+08:00"),
        _checkin("E-02", "S1", "2024-03-16T08:00:00+08:00", "2024-03-16T09:00:00+08:00"),
        _correction("E-03", "S1", -2700),
    ]
    progress = _replay(events).students["S1"]
    assert _daily_map(progress) == {
        "2024-03-15": 3600 - 2700,
        "2024-03-16": 3600,
    }
    unattributed = [
        a for a in progress.anomalies if a.kind == AnomalyKind.UNATTRIBUTED_CORRECTION
    ]
    assert len(unattributed) == 1
    assert unattributed[0].event_id == "E-03"
    assert unattributed[0].student_id == "S1"
    assert progress.adjustments[0].attributed is False
    _assert_consistent(progress)


def test_unattributed_positive_correction_lands_on_latest_confirmed_day():
    events = [
        _checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T09:00:00+08:00"),
        _checkin("E-02", "S1", "2024-03-16T08:00:00+08:00", "2024-03-16T09:00:00+08:00"),
        _correction("E-03", "S1", 1800, reason="make-up"),
    ]
    progress = _replay(events).students["S1"]
    assert _daily_map(progress) == {
        "2024-03-15": 3600,
        "2024-03-16": 3600 + 1800,
    }
    assert progress.adjustments[0].attributed is False
    assert any(
        a.kind == AnomalyKind.UNATTRIBUTED_CORRECTION for a in progress.anomalies
    )
    _assert_consistent(progress)


def test_unattributed_positive_correction_without_any_checkin_uses_fallback_day():
    # 学生没有任何确认签到：按事件创建日期入账，保证明细与总量一致。
    events = [
        _correction("E-01", "S1", 3600, reason="make-up"),
    ]
    progress = _replay(events).students["S1"]
    assert progress.total_seconds == 3600
    # created_at = 2024-03-20T12:00:00Z -> 上海时间 2024-03-20 20:00。
    assert _daily_map(progress) == {"2024-03-20": 3600}
    assert progress.adjustments[0].fallback_day == "2024-03-20"
    _assert_consistent(progress)


def test_correction_referencing_missing_checkin_falls_back_with_anomaly():
    events = [
        _checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T09:00:00+08:00"),
        _correction("E-02", "S1", -1800, checkin_event_id="E-99"),
    ]
    progress = _replay(events).students["S1"]
    assert _daily_map(progress) == {"2024-03-15": 3600 - 1800}
    assert progress.adjustments[0].attributed is False
    unattributed = [
        a for a in progress.anomalies if a.kind == AnomalyKind.UNATTRIBUTED_CORRECTION
    ]
    assert len(unattributed) == 1
    assert "E-99" in unattributed[0].detail
    _assert_consistent(progress)


def test_correction_referencing_other_students_checkin_falls_back():
    events = [
        _checkin("E-01", "S2", "2024-03-15T08:00:00+08:00", "2024-03-15T09:00:00+08:00"),
        _checkin("E-02", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T09:00:00+08:00"),
        _correction("E-03", "S1", -1800, checkin_event_id="E-01"),
    ]
    state = _replay(events)
    progress = state.students["S1"]
    assert _daily_map(progress) == {"2024-03-15": 1800}
    assert progress.adjustments[0].attributed is False
    # 被引用的学生 S2 不受任何影响。
    assert _daily_map(state.students["S2"]) == {"2024-03-15": 3600}
    _assert_consistent(progress)


def test_multiple_corrections_stack_and_never_drive_a_day_negative():
    events = [
        _checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00"),
        _correction("E-02", "S1", -3600, business_date="2024-03-15"),
        _correction("E-03", "S1", -3600, business_date="2024-03-15"),
        _correction("E-04", "S1", -3600, business_date="2024-03-15"),
    ]
    progress = _replay(events).students["S1"]
    # 当天只有 7200 秒，三次扣减共 10800：当天扣到 0，超额 3600 被丢弃。
    assert _daily_map(progress) == {"2024-03-15": 0}
    assert progress.total_seconds == 0
    overflow = [a for a in progress.anomalies if a.kind == AnomalyKind.CORRECTION_OVERFLOW]
    assert len(overflow) == 1
    assert overflow[0].event_id == "E-04"
    assert overflow[0].seconds == 3600
    _assert_consistent(progress)


def test_mixed_sign_corrections_stay_consistent_regardless_of_event_order():
    # 负向事件 id 小于正向事件 id：应用顺序必须是先正后负，与事件顺序无关。
    checkins = [
        _checkin("E-10", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T09:00:00+08:00"),
    ]
    corrections = [
        _correction("E-01", "S1", -7200, business_date="2024-03-15"),
        _correction("E-02", "S1", 3600, business_date="2024-03-15"),
    ]
    forward = _replay(checkins + corrections).students["S1"]
    reversed_state = _replay(list(reversed(checkins + corrections))).students["S1"]
    for progress in (forward, reversed_state):
        assert progress.total_seconds == 0
        assert _daily_map(progress) == {"2024-03-15": 0}
        _assert_consistent(progress)
    # 正向先入 3600 再被负向一并扣掉，没有可丢弃的超额。
    assert forward.anomalies == []


def test_attributed_correction_replay_is_deterministic_under_shuffle():
    import random

    events = [
        _checkin("E-01", "S1", "2024-03-15T22:00:00+08:00", "2024-03-16T02:00:00+08:00"),
        _checkin("E-02", "S1", "2024-03-17T08:00:00+08:00", "2024-03-17T10:00:00+08:00"),
        _correction("E-03", "S1", -3600, checkin_event_id="E-01"),
        _correction("E-04", "S1", -900, business_date="2024-03-17"),
        _correction("E-05", "S1", 1200),
    ]
    baseline = _replay(events).students["S1"]
    rng = random.Random(7)
    for _ in range(5):
        shuffled = list(events)
        rng.shuffle(shuffled)
        current = _replay(shuffled).students["S1"]
        assert _daily_map(current) == _daily_map(baseline)
        assert current.total_seconds == baseline.total_seconds
        assert [a.anomaly_id for a in current.anomalies] == [
            a.anomaly_id for a in baseline.anomalies
        ]


def test_dst_fallback_night_correction_uses_real_elapsed_weights():
    # 纽约 DST 回拨夜跨午夜：11-02 22:00 EDT -> 11-03 01:00 EST，
    # 真实 4 小时，两日各占 7200 秒。
    events = [
        _checkin(
            "E-01",
            "S1",
            "2024-11-02T22:00:00-04:00",
            "2024-11-03T01:00:00-05:00",
        ),
        _correction("E-02", "S1", -3600, checkin_event_id="E-01"),
    ]
    progress = _replay(events, tz="America/New_York").students["S1"]
    assert _daily_map(progress) == {
        "2024-11-02": 7200 - 1800,
        "2024-11-03": 7200 - 1800,
    }
    assert progress.total_seconds == 4 * 3600 - 3600
    _assert_consistent(progress)


def test_business_date_without_checkin_that_day_creates_new_day_entry():
    # 正向更正指定了一个没有签到的业务日期：该日期出现在明细中。
    events = [
        _checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T09:00:00+08:00"),
        _correction("E-02", "S1", 1800, business_date="2024-03-18", reason="make-up"),
    ]
    progress = _replay(events).students["S1"]
    assert _daily_map(progress) == {
        "2024-03-15": 3600,
        "2024-03-18": 1800,
    }
    assert progress.anomalies == []
    _assert_consistent(progress)


def test_invalid_business_date_is_treated_as_unattributed():
    events = [
        _checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T09:00:00+08:00"),
        _correction("E-02", "S1", -1800, business_date="not-a-date"),
    ]
    progress = _replay(events).students["S1"]
    assert progress.adjustments[0].business_date is None
    assert progress.adjustments[0].attributed is False
    assert _daily_map(progress) == {"2024-03-15": 1800}
    assert any(
        a.kind == AnomalyKind.UNATTRIBUTED_CORRECTION for a in progress.anomalies
    )
    _assert_consistent(progress)


def test_correction_only_student_without_checkins_clamps_to_zero():
    events = [
        _correction("E-01", "S1", -7200),
    ]
    progress = _replay(events).students["S1"]
    assert progress.total_seconds == 0
    assert progress.daily == []
    kinds = {a.kind for a in progress.anomalies}
    assert AnomalyKind.UNATTRIBUTED_CORRECTION in kinds
    assert AnomalyKind.CORRECTION_OVERFLOW in kinds
    _assert_consistent(progress)
