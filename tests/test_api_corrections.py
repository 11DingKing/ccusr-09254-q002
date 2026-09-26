"""更正归属贯通 API/冻结/重启恢复的端到端测试。"""

from __future__ import annotations

from tests.conftest import SHANGHAI_PLAN, TestSessionLocal
from app import services
from app.core.snapshot import Snapshot


PV = SHANGHAI_PLAN["plan_version"]


def _create_plan(client):
    resp = client.post("/api/plans", json=SHANGHAI_PLAN)
    assert resp.status_code == 201, resp.text


def _checkin(eid, student, start, end, activity_type="regular"):
    return {
        "event_id": eid,
        "event_type": "checkin",
        "student_id": student,
        "payload": {
            "activity_id": "A1",
            "activity_type": activity_type,
            "check_in_at": start,
            "check_out_at": end,
        },
    }


def _correction(eid, student, seconds, **target):
    return {
        "event_id": eid,
        "event_type": "leave_correction",
        "student_id": student,
        "payload": {"adjustment_seconds": seconds, "reason": "r", **target},
    }


def _post(client, events):
    resp = client.post(f"/api/plans/{PV}/events", json={"events": events})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _daily(progress):
    return {d["academic_day"]: d["seconds"] for d in progress["daily"]}


def test_checkin_linked_correction_flows_through_progress_api(client):
    _create_plan(client)
    _post(
        client,
        [
            _checkin(
                "E-01", "S1",
                "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00",
            ),
            _correction("E-02", "S1", -1800, checkin_event_id="E-01"),
        ],
    )
    progress = client.get(f"/api/plans/{PV}/students/S1/progress").json()
    assert progress["total_seconds"] == 5400
    assert _daily(progress) == {"2024-03-15": 5400}
    assert sum(d["seconds"] for d in progress["daily"]) == progress["total_seconds"]
    adj = progress["adjustments"][0]
    assert adj["target_type"] == "checkin"
    assert adj["checkin_event_id"] == "E-01"
    assert adj["allocations"] == [
        {"academic_day": "2024-03-15", "seconds": -1800}
    ]
    assert progress["anomalies"] == []


def test_cross_midnight_correction_breaks_down_across_days_via_api(client):
    _create_plan(client)
    _post(
        client,
        [
            _checkin(
                "E-01", "S1",
                "2024-03-15T22:00:00+08:00", "2024-03-16T02:00:00+08:00",
            ),
            _correction("E-02", "S1", -3 * 3600, checkin_event_id="E-01"),
        ],
    )
    progress = client.get(f"/api/plans/{PV}/students/S1/progress").json()
    assert _daily(progress) == {"2024-03-15": 0, "2024-03-16": 3600}
    assert progress["total_seconds"] == 3600
    snapshot = client.get(f"/api/plans/{PV}/snapshot").json()
    student = snapshot["students"][0]
    assert sum(d["seconds"] for d in student["daily"]) == student["total_seconds"]


def test_over_deduction_is_audited_in_api_and_day_never_negative(client):
    _create_plan(client)
    _post(
        client,
        [
            _checkin(
                "E-01", "S1",
                "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00",
            ),
            _correction("E-02", "S1", -5 * 3600, academic_day="2024-03-15"),
        ],
    )
    progress = client.get(f"/api/plans/{PV}/students/S1/progress").json()
    assert progress["total_seconds"] == 0
    assert _daily(progress) == {"2024-03-15": 0}
    anomalies = progress["anomalies"]
    assert len(anomalies) == 1
    assert anomalies[0]["code"] == "correction_exceeds_target"
    assert anomalies[0]["event_id"] == "E-02"
    assert anomalies[0]["applied_seconds"] == -7200
    assert anomalies[0]["detail"]["unapplied_seconds"] == 3 * 3600


def test_freeze_before_and_after_correction_keeps_old_daily_intact(client):
    _create_plan(client)
    _post(
        client,
        [
            _checkin(
                "E-01", "S1",
                "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00",
            )
        ],
    )
    f1 = client.post(f"/api/plans/{PV}/freezes/F-01", json={}).json()
    assert f1["students"][0]["daily"] == [
        {"academic_day": "2024-03-15", "seconds": 7200}
    ]

    _post(
        client,
        [_correction("E-09", "S1", -1800, checkin_event_id="E-01")],
    )

    # F-01 remains byte-for-byte stable, including the daily breakdown.
    f1_again = client.get(f"/api/plans/{PV}/freezes/F-01").json()
    assert f1_again["students"][0]["daily"] == [
        {"academic_day": "2024-03-15", "seconds": 7200}
    ]
    assert f1_again["students"][0]["total_seconds"] == 7200

    # F-02 reflects the correction in both total and daily.
    f2 = client.post(f"/api/plans/{PV}/freezes/F-02", json={}).json()
    s2 = f2["students"][0]
    assert s2["total_seconds"] == 5400
    assert s2["daily"] == [{"academic_day": "2024-03-15", "seconds": 5400}]
    assert s2["allocated_adjustment_seconds"] == -1800

    diff = client.get(f"/api/plans/{PV}/freezes/F-01/diff/F-02").json()
    fields = diff["student_changes"][0]["fields"]
    assert fields["allocated_adjustment_seconds"]["after"] == -1800
    assert fields["total_seconds"]["after"] == 5400


def test_duplicate_correction_import_does_not_double_deduct(client):
    _create_plan(client)
    checkin = _checkin(
        "E-01", "S1",
        "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00",
    )
    correction = _correction("E-02", "S1", -1800, checkin_event_id="E-01")
    _post(client, [checkin, correction])
    result = _post(client, [correction])
    assert result["accepted"] == 0
    assert result["duplicates"] == ["E-02"]

    # Repeated imports converge to the same state.
    progress_a = client.get(f"/api/plans/{PV}/students/S1/progress").json()
    _post(client, [correction])
    progress_b = client.get(f"/api/plans/{PV}/students/S1/progress").json()
    assert progress_a["total_seconds"] == progress_b["total_seconds"] == 5400
    assert _daily(progress_a) == _daily(progress_b) == {"2024-03-15": 5400}


def test_repeated_corrections_converge_and_clamp(client):
    _create_plan(client)
    events = [
        _checkin(
            "E-01", "S1",
            "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00",
        )
    ]
    # Five distinct 30-minute deductions against a two-hour check-in.
    for i in range(5):
        events.append(
            _correction(
                f"E-{i + 2:02d}", "S1", -1800, checkin_event_id="E-01"
            )
        )
    _post(client, events)
    progress = client.get(f"/api/plans/{PV}/students/S1/progress").json()
    assert progress["total_seconds"] == 0
    assert _daily(progress) == {"2024-03-15": 0}
    # Exactly the last correction overflows.
    overflow = [a for a in progress["anomalies"]]
    assert [a["event_id"] for a in overflow] == ["E-06"]
    applied = {a["event_id"]: a["applied_seconds"] for a in progress["adjustments"]}
    assert applied["E-02"] == -1800
    assert applied["E-06"] == 0


def test_freeze_survives_restart_with_new_session(client):
    _create_plan(client)
    _post(
        client,
        [
            _checkin(
                "E-01", "S1",
                "2024-03-15T22:00:00+08:00", "2024-03-16T02:00:00+08:00",
            ),
            _correction("E-02", "S1", -3 * 3600, checkin_event_id="E-01"),
        ],
    )
    client.post(f"/api/plans/{PV}/freezes/F-R1", json={})

    # Simulate a process restart: a brand new session reads persisted rows.
    session = TestSessionLocal()
    try:
        frozen = services.get_frozen_snapshot(session, PV, "F-R1")
        assert isinstance(frozen, Snapshot)
        student = frozen.students[0]
        assert student["total_seconds"] == 3600
        assert student["daily"] == [
            {"academic_day": "2024-03-15", "seconds": 0},
            {"academic_day": "2024-03-16", "seconds": 3600},
        ]
        adj = student["adjustments"][0]
        assert adj["allocations"] == [
            {"academic_day": "2024-03-15", "seconds": -7200},
            {"academic_day": "2024-03-16", "seconds": -3600},
        ]
        # Live replay from the event log after restart gives the same state.
        live = services.current_snapshot(session, PV)
        live_student = live.students[0]
        assert live_student["total_seconds"] == student["total_seconds"]
        assert live_student["daily"] == student["daily"]
        # Re-freezing the same id is idempotent across the restart.
        snap, created = services.freeze_semester(
            session, plan_version=PV, freeze_id="F-R1"
        )
        assert created is False
        assert snap.students[0]["total_seconds"] == 3600
    finally:
        session.close()


def test_correction_requires_a_target(client):
    _create_plan(client)
    resp = client.post(
        f"/api/plans/{PV}/events",
        json={
            "events": [
                {
                    "event_id": "E-01",
                    "event_type": "leave_correction",
                    "student_id": "S1",
                    "payload": {"adjustment_seconds": -600, "reason": "late"},
                }
            ]
        },
    )
    assert resp.status_code == 422


def test_correction_rejects_both_targets(client):
    _create_plan(client)
    resp = client.post(
        f"/api/plans/{PV}/events",
        json={
            "events": [
                {
                    "event_id": "E-01",
                    "event_type": "leave_correction",
                    "student_id": "S1",
                    "payload": {
                        "adjustment_seconds": -600,
                        "checkin_event_id": "E-00",
                        "academic_day": "2024-03-15",
                    },
                }
            ]
        },
    )
    assert resp.status_code == 422


def test_correction_rejects_malformed_academic_day(client):
    _create_plan(client)
    resp = client.post(
        f"/api/plans/{PV}/events",
        json={
            "events": [
                {
                    "event_id": "E-01",
                    "event_type": "leave_correction",
                    "student_id": "S1",
                    "payload": {
                        "adjustment_seconds": -600,
                        "academic_day": "15/03/2024",
                    },
                }
            ]
        },
    )
    assert resp.status_code == 422
