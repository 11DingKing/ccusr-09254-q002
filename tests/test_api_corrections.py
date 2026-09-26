"""请假更正的 API 端到端测试：冻结前后、时区边界、重复导入与重启恢复。"""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import services
from tests.conftest import NY_PLAN, SHANGHAI_PLAN


def _create_plan(client, plan=SHANGHAI_PLAN):
    resp = client.post("/api/plans", json=plan)
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


def _correction(eid, student, seconds, **extra):
    payload = {"adjustment_seconds": seconds, "reason": extra.pop("reason", "leave")}
    payload.update(extra)
    return {
        "event_id": eid,
        "event_type": "leave_correction",
        "student_id": student,
        "payload": payload,
    }


def _post_events(client, plan_version, events):
    resp = client.post(f"/api/plans/{plan_version}/events", json={"events": events})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _daily_map(student_body):
    return {d["academic_day"]: d["seconds"] for d in student_body["daily"]}


def _assert_student_consistent(student_body):
    assert all(d["seconds"] >= 0 for d in student_body["daily"])
    assert sum(d["seconds"] for d in student_body["daily"]) == (
        student_body["total_seconds"]
    )


def test_freeze_before_and_after_correction_keeps_daily_consistent(client):
    """冻结前的快照不受迟到更正影响；新冻结的日明细与总量一致。"""
    _create_plan(client)
    pv = SHANGHAI_PLAN["plan_version"]
    _post_events(
        client,
        pv,
        [
            _checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00"),
            _checkin("E-02", "S1", "2024-03-16T08:00:00+08:00", "2024-03-16T10:00:00+08:00"),
        ],
    )

    f1 = client.post(f"/api/plans/{pv}/freezes/F-01", json={}).json()
    student = f1["students"][0]
    assert student["total_seconds"] == 4 * 3600
    assert _daily_map(student) == {"2024-03-15": 7200, "2024-03-16": 7200}

    # 冻结后到达的负向更正，明确归属 2024-03-15。
    _post_events(
        client,
        pv,
        [_correction("E-03", "S1", -3600, business_date="2024-03-15")],
    )

    # 旧冻结保持原值，包括每日明细。
    f1_again = client.get(f"/api/plans/{pv}/freezes/F-01").json()
    student = f1_again["students"][0]
    assert student["total_seconds"] == 4 * 3600
    assert _daily_map(student) == {"2024-03-15": 7200, "2024-03-16": 7200}

    # 实时快照：总量与日明细同步下降。
    live = client.get(f"/api/plans/{pv}/snapshot").json()
    student = live["students"][0]
    assert student["total_seconds"] == 3 * 3600
    assert _daily_map(student) == {"2024-03-15": 3600, "2024-03-16": 7200}
    _assert_student_consistent(student)

    # 新冻结反映更正，diff 能解释变化。
    f2 = client.post(f"/api/plans/{pv}/freezes/F-02", json={}).json()
    student = f2["students"][0]
    assert student["total_seconds"] == 3 * 3600
    assert _daily_map(student) == {"2024-03-15": 3600, "2024-03-16": 7200}
    _assert_student_consistent(student)

    diff = client.get(f"/api/plans/{pv}/freezes/F-01/diff/F-02").json()
    change = diff["student_changes"][0]
    assert change["fields"]["total_seconds"] == {"before": 14400, "after": 10800}
    assert change["fields"]["adjustment_seconds"] == {"before": 0, "after": -3600}


def test_duplicate_correction_import_is_idempotent(client):
    """重复导入同一更正事件只应用一次，日明细与总量保持一致。"""
    _create_plan(client)
    pv = SHANGHAI_PLAN["plan_version"]
    events = [
        _checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00"),
        _correction("E-02", "S1", -1800, business_date="2024-03-15"),
    ]
    first = _post_events(client, pv, events)
    assert first["accepted"] == 2

    second = _post_events(client, pv, events)
    assert second["accepted"] == 0
    assert set(second["duplicates"]) == {"E-01", "E-02"}

    progress = client.get(f"/api/plans/{pv}/students/S1/progress").json()
    assert progress["adjustment_seconds"] == -1800
    assert progress["total_seconds"] == 7200 - 1800
    assert _daily_map(progress) == {"2024-03-15": 7200 - 1800}
    assert len(progress["adjustments"]) == 1
    _assert_student_consistent(progress)


def test_multiple_corrections_via_api_never_negative_and_overflow_audited(client):
    """多次更正叠加：任何一天不扣成负数，超额部分进入可审计异常。"""
    _create_plan(client)
    pv = SHANGHAI_PLAN["plan_version"]
    _post_events(
        client,
        pv,
        [
            _checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00"),
            _correction("E-02", "S1", -3600, business_date="2024-03-15"),
            _correction("E-03", "S1", -3600, business_date="2024-03-15"),
            _correction("E-04", "S1", -3600, business_date="2024-03-15"),
        ],
    )
    progress = client.get(f"/api/plans/{pv}/students/S1/progress").json()
    assert progress["total_seconds"] == 0
    assert _daily_map(progress) == {"2024-03-15": 0}
    overflow = [a for a in progress["anomalies"] if a["kind"] == "correction_overflow"]
    assert len(overflow) == 1
    assert overflow[0]["event_id"] == "E-04"
    assert overflow[0]["seconds"] == 3600
    _assert_student_consistent(progress)


def test_unattributed_legacy_correction_is_auditable_via_api(client):
    """无归属的历史更正：总量与日明细仍一致，异常在 API 响应中可见。"""
    _create_plan(client)
    pv = SHANGHAI_PLAN["plan_version"]
    _post_events(
        client,
        pv,
        [
            _checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T09:00:00+08:00"),
            _checkin("E-02", "S1", "2024-03-16T08:00:00+08:00", "2024-03-16T09:00:00+08:00"),
            _correction("E-03", "S1", -2700),
        ],
    )
    progress = client.get(f"/api/plans/{pv}/students/S1/progress").json()
    assert progress["total_seconds"] == 2 * 3600 - 2700
    assert _daily_map(progress) == {"2024-03-15": 900, "2024-03-16": 3600}
    assert progress["adjustments"][0]["attributed"] is False
    anomalies = progress["anomalies"]
    assert len(anomalies) == 1
    assert anomalies[0]["kind"] == "unattributed_correction"
    assert anomalies[0]["event_id"] == "E-03"
    _assert_student_consistent(progress)


def test_cross_midnight_correction_via_api_splits_by_plan_timezone(client):
    """纽约时区跨午夜签到：关联更正按学术日占比分摊到两日。"""
    plan = dict(NY_PLAN)
    client.post("/api/plans", json=plan)
    pv = plan["plan_version"]
    _post_events(
        client,
        pv,
        [
            _checkin(
                "E-01",
                "S2",
                "2024-03-15T22:00:00-04:00",
                "2024-03-16T02:00:00-04:00",
            ),
            _correction("E-02", "S2", -3600, checkin_event_id="E-01"),
        ],
    )
    progress = client.get(f"/api/plans/{pv}/students/S2/progress").json()
    assert _daily_map(progress) == {
        "2024-03-15": 7200 - 1800,
        "2024-03-16": 7200 - 1800,
    }
    assert progress["total_seconds"] == 4 * 3600 - 3600
    assert progress["anomalies"] == []
    _assert_student_consistent(progress)


def test_dst_boundary_correction_via_api(client):
    """DST 回拨夜的跨午夜签到：按真实经过时间分摊扣减。"""
    plan = dict(NY_PLAN)
    client.post("/api/plans", json=plan)
    pv = plan["plan_version"]
    _post_events(
        client,
        pv,
        [
            _checkin(
                "E-01",
                "S2",
                "2024-11-02T22:00:00-04:00",
                "2024-11-03T01:00:00-05:00",
            ),
            _correction("E-02", "S2", -3600, checkin_event_id="E-01"),
        ],
    )
    progress = client.get(f"/api/plans/{pv}/students/S2/progress").json()
    assert _daily_map(progress) == {
        "2024-11-02": 5400,
        "2024-11-03": 5400,
    }
    _assert_student_consistent(progress)


def test_frozen_snapshot_with_corrections_survives_restart(client):
    """模拟服务重启：新建引擎与会话后，冻结快照的明细、总量与异常完整恢复。"""
    _create_plan(client)
    pv = SHANGHAI_PLAN["plan_version"]
    _post_events(
        client,
        pv,
        [
            _checkin("E-01", "S1", "2024-03-15T22:00:00+08:00", "2024-03-16T02:00:00+08:00"),
            _correction("E-02", "S1", -3600, checkin_event_id="E-01"),
            _correction("E-03", "S1", -900),
        ],
    )
    created = client.post(f"/api/plans/{pv}/freezes/F-RESTART", json={}).json()
    assert created["freeze_id"] == "F-RESTART"

    # 模拟重启：全新的引擎与会话连接同一个数据库文件。
    engine2 = create_engine(
        "sqlite:///./practice_hours_test.db",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    session_local = sessionmaker(
        bind=engine2, autoflush=False, autocommit=False, future=True
    )
    session = session_local()
    try:
        snap = services.get_frozen_snapshot(session, pv, "F-RESTART")
    finally:
        session.close()
        engine2.dispose()

    student = snap.students[0]
    assert student["total_seconds"] == 4 * 3600 - 3600 - 900
    assert _daily_map(student) == {
        "2024-03-15": 7200 - 1800 - 900,
        "2024-03-16": 7200 - 1800,
    }
    _assert_student_consistent(student)
    # 无归属更正的异常被持久化并在重启后仍可审计。
    kinds = {a["kind"] for a in student["anomalies"]}
    assert kinds == {"unattributed_correction"}
    unattributed = student["anomalies"][0]
    assert unattributed["event_id"] == "E-03"
    # 归属信息同样完整恢复。
    by_id = {a["event_id"]: a for a in student["adjustments"]}
    assert by_id["E-02"]["checkin_event_id"] == "E-01"
    assert by_id["E-02"]["attributed"] is True
    assert by_id["E-03"]["attributed"] is False


def test_live_snapshot_matches_replay_after_restart(client):
    """重启后实时快照由事件重放重建，与重启前完全一致。"""
    _create_plan(client)
    pv = SHANGHAI_PLAN["plan_version"]
    _post_events(
        client,
        pv,
        [
            _checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00"),
            _correction("E-02", "S1", -1800, business_date="2024-03-15"),
        ],
    )
    before = client.get(f"/api/plans/{pv}/snapshot").json()

    engine2 = create_engine(
        "sqlite:///./practice_hours_test.db",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    session_local = sessionmaker(
        bind=engine2, autoflush=False, autocommit=False, future=True
    )
    session = session_local()
    try:
        rebuilt = services.current_snapshot(session, pv)
    finally:
        session.close()
        engine2.dispose()

    student = rebuilt.students[0]
    assert student["total_seconds"] == before["students"][0]["total_seconds"]
    assert _daily_map(student) == _daily_map(before["students"][0])
    assert student["anomalies"] == before["students"][0]["anomalies"]
    _assert_student_consistent(student)


def test_legacy_frozen_snapshot_without_anomalies_still_readable(client, db):
    """旧格式冻结快照（无异常与归属字段）在新 API 下仍可正常读取。"""
    _create_plan(client)
    pv = SHANGHAI_PLAN["plan_version"]
    legacy_snapshot = {
        "plan_version": pv,
        "freeze_id": "F-LEGACY",
        "timezone": SHANGHAI_PLAN["iana_timezone"],
        "required_seconds": SHANGHAI_PLAN["required_seconds"],
        "generated_at": "2024-03-20T00:00:00Z",
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
                "meets_requirement": False,
                "daily": [{"academic_day": "2024-03-15", "seconds": 6300}],
                "checkins": [],
                "adjustments": [
                    {"event_id": "E-02", "seconds": -900, "reason": "late"}
                ],
            }
        ],
    }
    from app.repository import insert_freeze

    row = insert_freeze(
        db,
        plan_version=pv,
        freeze_id="F-LEGACY",
        snapshot=legacy_snapshot,
        event_cutoff_id="E-02",
    )
    assert row is not None

    explanation = client.get(f"/api/plans/{pv}/freezes/F-LEGACY/explain/S1").json()
    assert explanation["student_id"] == "S1"
    assert explanation["total_seconds"] == 6300
    assert explanation["anomalies"] == []
    assert explanation["adjustments"][0]["seconds"] == -900
    assert explanation["adjustments"][0]["attributed"] is True
