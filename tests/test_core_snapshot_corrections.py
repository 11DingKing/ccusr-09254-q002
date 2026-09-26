"""更正分摊结果的快照序列化与旧格式兼容测试。"""

from __future__ import annotations

from datetime import datetime, timezone

from app.core.replay import Event, EventType
from app.core.snapshot import Snapshot, build_snapshot, explain_student


def _event(event_id, event_type, student_id, payload):
    return Event(
        event_id=event_id,
        plan_version="P1",
        event_type=event_type,
        student_id=student_id,
        payload=payload,
        created_at=datetime.now(timezone.utc),
    )


def _events_with_allocated_correction():
    return [
        _event(
            "E-01",
            EventType.CHECKIN,
            "S1",
            {
                "activity_id": "A1",
                "activity_type": "regular",
                "check_in_at": "2024-03-15T08:00:00+08:00",
                "check_out_at": "2024-03-15T10:00:00+08:00",
            },
        ),
        _event(
            "E-02",
            EventType.LEAVE_CORRECTION,
            "S1",
            {
                "adjustment_seconds": -1800,
                "reason": "late",
                "checkin_event_id": "E-01",
            },
        ),
        _event(
            "E-03",
            EventType.LEAVE_CORRECTION,
            "S1",
            {
                "adjustment_seconds": -5 * 3600,
                "reason": "absence",
                "academic_day": "2024-03-15",
            },
        ),
    ]


def test_snapshot_serializes_targets_allocations_and_anomalies():
    snap = build_snapshot(
        _events_with_allocated_correction(),
        plan_version="P1",
        timezone_name="Asia/Shanghai",
        required_seconds=0,
        freeze_id="F-01",
    )
    raw = snap.to_dict()
    student = raw["students"][0]
    assert student["total_seconds"] == sum(d["seconds"] for d in student["daily"])
    # E-02 fully applied; E-03 over-deducts and lands in anomalies.
    adjustments = {a["event_id"]: a for a in student["adjustments"]}
    assert adjustments["E-02"]["target_type"] == "checkin"
    assert adjustments["E-02"]["checkin_event_id"] == "E-01"
    assert adjustments["E-02"]["allocations"] == [
        {"academic_day": "2024-03-15", "seconds": -1800}
    ]
    assert adjustments["E-02"]["applied_seconds"] == -1800
    assert adjustments["E-03"]["target_type"] == "academic_day"
    assert adjustments["E-03"]["academic_day"] == "2024-03-15"
    assert adjustments["E-03"]["applied_seconds"] == -5400
    assert student["allocated_adjustment_seconds"] == -1800 + -5400
    assert student["adjustment_seconds"] == -1800 + -5 * 3600
    codes = {a["code"] for a in student["anomalies"]}
    assert "correction_exceeds_target" in codes


def test_snapshot_round_trip_preserves_correction_detail():
    snap = build_snapshot(
        _events_with_allocated_correction(),
        plan_version="P1",
        timezone_name="Asia/Shanghai",
        required_seconds=0,
        freeze_id="F-01",
    )
    restored = Snapshot.from_dict(snap.to_dict())
    before = explain_student(snap, "S1")
    after = explain_student(restored, "S1")
    assert before is not None and after is not None
    for key in (
        "total_seconds",
        "confirmed_seconds",
        "adjustment_seconds",
        "allocated_adjustment_seconds",
        "daily",
        "anomalies",
    ):
        assert before[key] == after[key]
    assert before["adjustments"] == after["adjustments"]


def test_legacy_snapshot_without_correction_fields_still_loads():
    # Simulates a freeze written by the previous version: adjustments have
    # no target/allocation fields and there is no anomalies section.
    legacy = {
        "plan_version": "P1",
        "freeze_id": "F-OLD",
        "timezone": "Asia/Shanghai",
        "required_seconds": 0,
        "generated_at": "2024-03-15T00:00:00Z",
        "event_cutoff_id": "E-02",
        "students": [
            {
                "student_id": "S1",
                "confirmed_seconds": 7200,
                "pending_seconds": 0,
                "adjustment_seconds": -900,
                "total_seconds": 6300,
                "lesson_units": 2,
                "pending_lesson_units": 0,
                "meets_requirement": True,
                "daily": [{"academic_day": "2024-03-15", "seconds": 6300}],
                "checkins": [],
                "adjustments": [
                    {"event_id": "E-02", "seconds": -900, "reason": "late"}
                ],
            }
        ],
    }
    snap = Snapshot.from_dict(legacy)
    student = explain_student(snap, "S1")
    assert student is not None
    assert student["total_seconds"] == 6300
    assert student["allocated_adjustment_seconds"] == -900
    assert student["anomalies"] == []
    adj = student["adjustments"][0]
    assert adj["target_type"] == "unattributed"
    assert adj["checkin_event_id"] is None
    assert adj["academic_day"] is None
    assert adj["allocations"] == []
    assert adj["applied_seconds"] == -900
