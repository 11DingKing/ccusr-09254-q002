"""服务端业务模块。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import StrEnum
from typing import Any, Iterable

from .clock import (
    elapsed_seconds,
    merge_intervals,
    split_by_academic_day,
    to_utc,
    union_seconds,
)


class EventType(StrEnum):
    CHECKIN = "checkin"
    MENTOR_CONFIRM = "mentor_confirm"
    LEAVE_CORRECTION = "leave_correction"


class CheckinStatus(StrEnum):
    CONFIRMED = "CONFIRMED"
    PENDING = "PENDING"


class AdjustmentTarget(StrEnum):
    CHECKIN = "checkin"
    ACADEMIC_DAY = "academic_day"
    UNATTRIBUTED = "unattributed"


class AnomalyCode(StrEnum):
    INVALID_TARGET = "correction_invalid_target"
    TARGET_MISSING = "correction_target_missing"
    TARGET_PENDING = "correction_target_pending"
    EXCEEDS_TARGET = "correction_exceeds_target"
    UNATTRIBUTED = "correction_unattributed"
    EXCEEDS_TOTAL = "correction_exceeds_student_total"


INTERNSHIP_TYPE = "internship"


@dataclass(frozen=True)
class Event:
    """封装领域状态与业务约束。"""

    event_id: str
    plan_version: str
    event_type: EventType
    student_id: str
    payload: dict[str, Any]
    created_at: datetime


@dataclass
class CheckinRecord:
    event_id: str
    student_id: str
    activity_id: str
    activity_type: str
    start_utc: datetime
    end_utc: datetime
    status: CheckinStatus

    @property
    def seconds(self) -> int:
        return elapsed_seconds(self.start_utc, self.end_utc)

    @property
    def counts(self) -> bool:
        return self.status == CheckinStatus.CONFIRMED

    def day_weights(self, tz_name: str) -> list[tuple[str, int]]:
        """该签到在各教学日上的原始秒数权重（按日期排序）。"""
        weights: dict[str, int] = {}
        for day, seg_start, seg_end in split_by_academic_day(
            self.start_utc, self.end_utc, tz_name
        ):
            key = day.isoformat()
            weights[key] = weights.get(key, 0) + elapsed_seconds(seg_start, seg_end)
        return sorted(weights.items())


@dataclass(frozen=True)
class DayAllocation:
    """更正到具体教学日的有符号分摊结果；academic_day 为 None 表示未归属桶。"""

    academic_day: str | None
    seconds: int


@dataclass(frozen=True)
class AuditAnomaly:
    """无法完整归属或超额扣减的可审计异常。"""

    event_id: str
    student_id: str
    code: str
    message: str
    attempted_seconds: int
    applied_seconds: int
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class Adjustment:
    event_id: str
    student_id: str
    seconds: int
    reason: str
    target_type: AdjustmentTarget = AdjustmentTarget.UNATTRIBUTED
    checkin_event_id: str | None = None
    academic_day: str | None = None
    allocations: list[DayAllocation] = field(default_factory=list)
    applied_seconds: int = 0
    anomaly_codes: list[str] = field(default_factory=list)

    @property
    def is_fully_applied(self) -> bool:
        return self.applied_seconds == self.seconds


@dataclass
class DayTotal:
    academic_day: str | None
    seconds: int


@dataclass
class StudentProgress:
    student_id: str
    confirmed_seconds: int
    pending_seconds: int
    adjustment_seconds: int
    allocated_adjustment_seconds: int
    total_seconds: int
    lesson_units: int
    pending_lesson_units: int
    meets_requirement: bool
    daily: list[DayTotal] = field(default_factory=list)
    checkins: list[CheckinRecord] = field(default_factory=list)
    adjustments: list[Adjustment] = field(default_factory=list)
    anomalies: list[AuditAnomaly] = field(default_factory=list)


@dataclass
class ReplayState:
    plan_version: str
    timezone: str
    required_seconds: int
    students: dict[str, StudentProgress]


def _parse_checkin(
    event: Event, tz_name: str
) -> CheckinRecord:
    start = to_utc(datetime.fromisoformat(event.payload["check_in_at"]))
    end = to_utc(datetime.fromisoformat(event.payload["check_out_at"]))
    activity_type = event.payload.get("activity_type", "regular")
    requires_confirmation = activity_type == INTERNSHIP_TYPE
    status = (
        CheckinStatus.PENDING if requires_confirmation else CheckinStatus.CONFIRMED
    )
    return CheckinRecord(
        event_id=event.event_id,
        student_id=event.student_id,
        activity_id=event.payload.get("activity_id", ""),
        activity_type=activity_type,
        start_utc=start,
        end_utc=end,
        status=status,
    )


def _parse_correction_target(
    event: Event,
) -> tuple[AdjustmentTarget, str | None, str | None, str | None]:
    """解析更正的归属目标，返回 (目标类型, 签到ID, 教学日, 异常代码)。"""
    target_checkin = event.payload.get("checkin_event_id")
    raw_day = event.payload.get("academic_day")

    if target_checkin is not None and raw_day is not None:
        return AdjustmentTarget.UNATTRIBUTED, None, None, AnomalyCode.INVALID_TARGET

    if target_checkin is not None:
        if not isinstance(target_checkin, str) or not target_checkin:
            return AdjustmentTarget.UNATTRIBUTED, None, None, AnomalyCode.INVALID_TARGET
        return AdjustmentTarget.CHECKIN, target_checkin, None, None

    if raw_day is not None:
        if not isinstance(raw_day, str):
            return AdjustmentTarget.UNATTRIBUTED, None, None, AnomalyCode.INVALID_TARGET
        try:
            day = date.fromisoformat(raw_day)
        except ValueError:
            return AdjustmentTarget.UNATTRIBUTED, None, None, AnomalyCode.INVALID_TARGET
        return AdjustmentTarget.ACADEMIC_DAY, None, day.isoformat(), None

    return AdjustmentTarget.UNATTRIBUTED, None, None, None


def _distribute_positive(
    amount: int, weights: list[tuple[str, int]]
) -> list[tuple[str, int]]:
    """按权重把正向秒数整数分摊（Hamilton 最大余数法，平局取较早日期）。"""
    total_weight = sum(w for _, w in weights)
    if amount <= 0 or total_weight <= 0:
        return []
    floors: list[tuple[str, int, float]] = []
    allocated = 0
    for day, weight in weights:
        exact = amount * weight / total_weight
        whole = int(exact)
        floors.append((day, whole, exact - whole))
        allocated += whole
    leftover = amount - allocated
    # 余数按小数部分降序、日期升序补发。
    for day, _, _ in sorted(floors, key=lambda item: (-item[2], item[0]))[:leftover]:
        for i, (d, whole, frac) in enumerate(floors):
            if d == day:
                floors[i] = (d, whole + 1, frac)
                break
    return [(day, whole) for day, whole, _ in floors if whole]


def replay(
    events: Iterable[Event],
    *,
    plan_version: str,
    timezone_name: str,
    required_seconds: int,
    up_to_event_id: str | None = None,
) -> ReplayState:
    """执行确定性的业务处理。"""
    sorted_events = sorted(
        (e for e in events if e.plan_version == plan_version),
        key=lambda e: e.event_id,
    )
    if up_to_event_id is not None:
        sorted_events = [e for e in sorted_events if e.event_id <= up_to_event_id]

    checkins_by_student: dict[str, list[CheckinRecord]] = {}
    checkin_index: dict[str, CheckinRecord] = {}
    corrections: list[Event] = []

    # 第一遍：收集签到与导师确认。更正对原签到的引用必须无视事件 ID
    # 先后顺序都能解析，因此放到第二遍处理。
    for event in sorted_events:
        if event.event_type == EventType.CHECKIN:
            record = _parse_checkin(event, timezone_name)
            checkins_by_student.setdefault(event.student_id, []).append(record)
            checkin_index[event.event_id] = record
        elif event.event_type == EventType.MENTOR_CONFIRM:
            target_id = event.payload.get("checkin_event_id")
            target = checkin_index.get(target_id)
            if target is not None and target.student_id == event.student_id:
                target.status = CheckinStatus.CONFIRMED
        elif event.event_type == EventType.LEAVE_CORRECTION:
            corrections.append(event)

    adjustments_by_student: dict[str, list[Adjustment]] = {}
    for event in corrections:
        target_type, target_checkin, target_day, invalid_code = (
            _parse_correction_target(event)
        )
        adjustments_by_student.setdefault(event.student_id, []).append(
            Adjustment(
                event_id=event.event_id,
                student_id=event.student_id,
                seconds=int(event.payload.get("adjustment_seconds", 0)),
                reason=str(event.payload.get("reason", "")),
                target_type=target_type,
                checkin_event_id=target_checkin,
                academic_day=target_day,
                anomaly_codes=[invalid_code] if invalid_code else [],
            )
        )

    all_students = set(checkins_by_student) | set(adjustments_by_student)
    students: dict[str, StudentProgress] = {}
    for student_id in all_students:
        records = checkins_by_student.get(student_id, [])
        adjustments = sorted(
            adjustments_by_student.get(student_id, []), key=lambda a: a.event_id
        )

        confirmed_intervals = [
            (r.start_utc, r.end_utc) for r in records if r.counts
        ]
        pending_intervals = [
            (r.start_utc, r.end_utc)
            for r in records
            if r.status == CheckinStatus.PENDING
        ]

        confirmed_seconds = union_seconds(confirmed_intervals)
        pending_seconds = union_seconds(pending_intervals)

        # 各教学日的已确认秒数余额；更正只能在余额范围内扣减。
        day_balance: dict[str, int] = {}
        for start, end in merge_intervals(confirmed_intervals):
            for day, seg_start, seg_end in split_by_academic_day(
                start, end, timezone_name
            ):
                key = day.isoformat()
                day_balance[key] = day_balance.get(key, 0) + elapsed_seconds(
                    seg_start, seg_end
                )

        unallocated_seconds = 0
        anomalies: list[AuditAnomaly] = []

        def _anomaly(
            adjustment: Adjustment,
            code: AnomalyCode,
            message: str,
            *,
            attempted: int | None = None,
            applied: int | None = None,
            detail: dict[str, Any] | None = None,
        ) -> None:
            if code not in adjustment.anomaly_codes:
                adjustment.anomaly_codes.append(code)
            anomalies.append(
                AuditAnomaly(
                    event_id=adjustment.event_id,
                    student_id=student_id,
                    code=code.value,
                    message=message,
                    attempted_seconds=(
                        adjustment.seconds if attempted is None else attempted
                    ),
                    applied_seconds=(
                        adjustment.applied_seconds if applied is None else applied
                    ),
                    detail=detail or {},
                )
            )

        for adjustment in adjustments:
            seconds = adjustment.seconds

            # 解析归属目标并构造候选分摊日（带权重）。显式目标无法解析时，
            # 该更正不生效，只进入可审计异常；完全没有目标的历史记录则
            # 走尽力冲抵逻辑。
            candidates: list[tuple[str, int]] = []
            skip_application = False
            if AnomalyCode.INVALID_TARGET in adjustment.anomaly_codes:
                skip_application = True
            elif adjustment.target_type == AdjustmentTarget.CHECKIN:
                target = checkin_index.get(adjustment.checkin_event_id)
                if target is None or target.student_id != student_id:
                    _anomaly(
                        adjustment,
                        AnomalyCode.TARGET_MISSING,
                        "correction references a check-in that does not exist",
                        applied=0,
                        detail={"checkin_event_id": adjustment.checkin_event_id},
                    )
                    skip_application = True
                elif not target.counts:
                    _anomaly(
                        adjustment,
                        AnomalyCode.TARGET_PENDING,
                        "correction targets a check-in that is not confirmed yet",
                        applied=0,
                        detail={"checkin_event_id": adjustment.checkin_event_id},
                    )
                    skip_application = True
                else:
                    candidates = target.day_weights(timezone_name)
            elif adjustment.target_type == AdjustmentTarget.ACADEMIC_DAY:
                assert adjustment.academic_day is not None
                candidates = [(adjustment.academic_day, 0)]
            elif seconds != 0:
                _anomaly(
                    adjustment,
                    AnomalyCode.UNATTRIBUTED,
                    "correction is not linked to a business day or check-in",
                    applied=0,
                )

            if skip_application:
                continue

            if seconds > 0 and candidates:
                # 正向更正按权重归属到候选日，不设上限。
                weights = [
                    (day, max(weight, 1)) for day, weight in candidates
                ]
                for day, part in _distribute_positive(seconds, weights):
                    day_balance[day] = day_balance.get(day, 0) + part
                    adjustment.allocations.append(
                        DayAllocation(academic_day=day, seconds=part)
                    )
                adjustment.applied_seconds = seconds
            elif seconds < 0 and candidates:
                # 负向更正按候选日时间顺序扣减；每日扣减额不得超过当日
                # 余额，关联签到时还不得超过该签到在当日的原始时长，
                # 任何一天都不能扣成负数。
                remaining = -seconds
                for day, weight in candidates:
                    if remaining == 0:
                        break
                    current = day_balance.get(day, 0)
                    available = current
                    if adjustment.target_type == AdjustmentTarget.CHECKIN:
                        available = min(available, weight)
                    if available <= 0:
                        continue
                    taken = min(remaining, available)
                    day_balance[day] = current - taken
                    remaining -= taken
                    adjustment.allocations.append(
                        DayAllocation(academic_day=day, seconds=-taken)
                    )
                adjustment.applied_seconds = -(-seconds - remaining)
                if remaining > 0:
                    _anomaly(
                        adjustment,
                        AnomalyCode.EXCEEDS_TARGET,
                        "correction exceeds confirmed seconds on the target day(s)",
                        attempted=seconds,
                        applied=adjustment.applied_seconds,
                        detail={"unapplied_seconds": remaining},
                    )
            elif seconds > 0:
                # 无有效目标的正向更正进入未归属桶。
                unallocated_seconds += seconds
                adjustment.applied_seconds = seconds
                adjustment.allocations.append(
                    DayAllocation(academic_day=None, seconds=seconds)
                )
            elif seconds < 0:
                # 无有效目标的负向更正：先冲抵未归属正向余额，再按日期
                # 顺序在各教学日已确认余额内扣减，任何一天都不得为负，
                # 总量也不得为负；无法冲抵的部分进入可审计异常。
                remaining = -seconds
                if unallocated_seconds > 0:
                    taken = min(remaining, unallocated_seconds)
                    unallocated_seconds -= taken
                    remaining -= taken
                    adjustment.allocations.append(
                        DayAllocation(academic_day=None, seconds=-taken)
                    )
                for day in sorted(day_balance):
                    if remaining == 0:
                        break
                    available = day_balance[day]
                    if available <= 0:
                        continue
                    taken = min(remaining, available)
                    day_balance[day] = available - taken
                    remaining -= taken
                    adjustment.allocations.append(
                        DayAllocation(academic_day=day, seconds=-taken)
                    )
                adjustment.applied_seconds = -(-seconds - remaining)
                if remaining > 0:
                    _anomaly(
                        adjustment,
                        AnomalyCode.EXCEEDS_TOTAL,
                        "correction exceeds the student's confirmed total",
                        attempted=seconds,
                        applied=adjustment.applied_seconds,
                        detail={"unapplied_seconds": remaining},
                    )

        daily = [
            DayTotal(academic_day=day, seconds=secs)
            for day, secs in sorted(day_balance.items())
        ]
        if unallocated_seconds:
            daily.append(
                DayTotal(academic_day=None, seconds=unallocated_seconds)
            )

        raw_adjustment_seconds = sum(a.seconds for a in adjustments)
        allocated_adjustment_seconds = sum(a.applied_seconds for a in adjustments)
        total_seconds = confirmed_seconds + allocated_adjustment_seconds

        # 核心不变量：总量恒等于各日合计，任何一天都不为负。
        assert total_seconds == sum(d.seconds for d in daily)
        assert total_seconds >= 0
        assert all(d.seconds >= 0 for d in daily if d.academic_day is not None)

        students[student_id] = StudentProgress(
            student_id=student_id,
            confirmed_seconds=confirmed_seconds,
            pending_seconds=pending_seconds,
            adjustment_seconds=raw_adjustment_seconds,
            allocated_adjustment_seconds=allocated_adjustment_seconds,
            total_seconds=total_seconds,
            lesson_units=total_seconds // (45 * 60),
            pending_lesson_units=pending_seconds // (45 * 60),
            meets_requirement=total_seconds >= required_seconds,
            daily=daily,
            checkins=sorted(records, key=lambda r: r.start_utc),
            adjustments=adjustments,
            anomalies=anomalies,
        )

    return ReplayState(
        plan_version=plan_version,
        timezone=timezone_name,
        required_seconds=required_seconds,
        students=students,
    )


def explain_checkin(record: CheckinRecord, tz_name: str) -> dict[str, Any]:
    """执行确定性的业务处理。"""
    segments = split_by_academic_day(record.start_utc, record.end_utc, tz_name)
    return {
        "event_id": record.event_id,
        "activity_id": record.activity_id,
        "activity_type": record.activity_type,
        "status": record.status.value,
        "counts": record.counts,
        "check_in_at_utc": record.start_utc.astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "check_out_at_utc": record.end_utc.astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "raw_seconds": record.seconds,
        "academic_days": [
            {
                "day": day.isoformat(),
                "start_utc": seg_start.isoformat().replace("+00:00", "Z"),
                "end_utc": seg_end.isoformat().replace("+00:00", "Z"),
                "seconds": elapsed_seconds(seg_start, seg_end),
            }
            for day, seg_start, seg_end in segments
        ],
    }
