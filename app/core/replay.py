"""服务端业务模块。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import StrEnum
from typing import Any, Iterable

from .clock import (
    academic_day,
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


class AnomalyKind(StrEnum):
    UNATTRIBUTED_CORRECTION = "unattributed_correction"
    CORRECTION_OVERFLOW = "correction_overflow"


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


@dataclass
class Adjustment:
    """一次请假更正。归属优先级：business_date > checkin_event_id > 兜底规则。"""

    event_id: str
    student_id: str
    seconds: int
    reason: str
    business_date: str | None = None
    checkin_event_id: str | None = None
    attributed: bool = True
    fallback_day: str | None = None


@dataclass
class Anomaly:
    """可审计异常：无法归属的更正或被丢弃的超额扣减。"""

    anomaly_id: str
    kind: AnomalyKind
    event_id: str
    student_id: str
    seconds: int
    detail: str


@dataclass
class DayTotal:
    academic_day: str
    seconds: int


@dataclass
class StudentProgress:
    student_id: str
    confirmed_seconds: int
    pending_seconds: int
    adjustment_seconds: int
    total_seconds: int
    lesson_units: int
    pending_lesson_units: int
    meets_requirement: bool
    daily: list[DayTotal] = field(default_factory=list)
    checkins: list[CheckinRecord] = field(default_factory=list)
    adjustments: list[Adjustment] = field(default_factory=list)
    anomalies: list[Anomaly] = field(default_factory=list)


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


def _aware_utc(value: datetime) -> datetime:
    """历史库行可能读出 naive 时间戳，按 UTC 解释。"""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return to_utc(value)


def _parse_business_date(raw: Any) -> str | None:
    """规范化业务日期；缺失或非法值一律视为未提供。"""
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return None


def _parse_adjustment(event: Event, tz_name: str) -> Adjustment:
    payload = event.payload
    checkin_ref = payload.get("checkin_event_id")
    if checkin_ref is not None:
        checkin_ref = str(checkin_ref).strip() or None
    return Adjustment(
        event_id=event.event_id,
        student_id=event.student_id,
        seconds=int(payload.get("adjustment_seconds", 0)),
        reason=str(payload.get("reason", "")),
        business_date=_parse_business_date(payload.get("business_date")),
        checkin_event_id=checkin_ref,
        fallback_day=academic_day(
            _aware_utc(event.created_at), tz_name
        ).isoformat(),
    )


def _distribute(seconds: int, weights: dict[str, int]) -> dict[str, int]:
    """按权重把秒数分配到各学术日（最大余数法，并列时日期升序优先）。"""
    total_weight = sum(w for w in weights.values() if w > 0)
    if seconds <= 0 or total_weight <= 0:
        return {}
    shares: dict[str, int] = {}
    remainders: list[tuple[int, str]] = []
    allocated = 0
    for day in sorted(weights):
        weight = weights[day]
        if weight <= 0:
            continue
        exact = seconds * weight
        share = exact // total_weight
        shares[day] = share
        allocated += share
        remainders.append((exact - share * total_weight, day))
    leftover = seconds - allocated
    remainders.sort(key=lambda item: (-item[0], item[1]))
    for _, day in remainders[:leftover]:
        shares[day] += 1
    return shares


def _resolve_weights(
    adjustment: Adjustment,
    checkin_index: dict[str, CheckinRecord],
    tz_name: str,
) -> tuple[dict[str, int] | None, str | None]:
    """把更正解析到目标学术日权重；无法归属时返回原因说明。"""
    if adjustment.business_date is not None:
        return {adjustment.business_date: 1}, None
    if adjustment.checkin_event_id:
        target = checkin_index.get(adjustment.checkin_event_id)
        if target is not None and target.student_id == adjustment.student_id:
            weights: dict[str, int] = {}
            for day, seg_start, seg_end in split_by_academic_day(
                target.start_utc, target.end_utc, tz_name
            ):
                key = day.isoformat()
                weights[key] = weights.get(key, 0) + elapsed_seconds(
                    seg_start, seg_end
                )
            if weights:
                return weights, None
        return (
            None,
            f"referenced check-in '{adjustment.checkin_event_id}' "
            "was not found for this student",
        )
    return None, "correction provides neither business_date nor checkin_event_id"


def _unattributed_anomaly(
    adjustment: Adjustment, reason: str | None
) -> Anomaly:
    cause = reason or "correction could not be attributed"
    return Anomaly(
        anomaly_id=f"{adjustment.event_id}:unattributed",
        kind=AnomalyKind.UNATTRIBUTED_CORRECTION,
        event_id=adjustment.event_id,
        student_id=adjustment.student_id,
        seconds=adjustment.seconds,
        detail=f"{cause}; applied deterministic fallback allocation",
    )


def _deduct(
    day_totals: dict[str, int],
    amount: int,
    preferred: dict[str, int] | None,
) -> int:
    """从每日明细扣减 amount；任何一天不扣成负数，返回无法扣除的剩余量。"""
    remaining = amount
    if preferred:
        # 先按归属权重在目标日期之间分配扣减。
        for day, share in sorted(_distribute(amount, preferred).items()):
            take = min(share, day_totals.get(day, 0))
            if take:
                day_totals[day] -= take
                remaining -= take
        # 目标日期内兜底（某些目标日可扣量不足其份额时）。
        if remaining:
            for day in sorted(preferred):
                if remaining <= 0:
                    break
                take = min(remaining, day_totals.get(day, 0))
                if take:
                    day_totals[day] -= take
                    remaining -= take
    # 全局兜底：从最早有量的日期继续扣，保持总量与明细一致。
    if remaining:
        for day in sorted(day_totals):
            if remaining <= 0:
                break
            take = min(remaining, day_totals[day])
            if take:
                day_totals[day] -= take
                remaining -= take
    return remaining


def _apply_adjustments(
    adjustments: list[Adjustment],
    day_totals: dict[str, int],
    checkin_index: dict[str, CheckinRecord],
    tz_name: str,
) -> list[Anomaly]:
    """把更正分配到学术日明细。

    不变量：每日明细不为负，且 sum(daily) == max(0, confirmed + Σadjustment)。
    为满足该不变量，总是先应用全部正向调整、再应用负向调整（各自按
    event_id 排序保证确定性），负向扣减不足时溢出到其他日期，最终仍不足
    的部分记为可审计的超额异常。
    """
    anomalies: list[Anomaly] = []
    ordered = sorted(adjustments, key=lambda a: a.event_id)

    for adjustment in ordered:
        if adjustment.seconds <= 0:
            continue
        weights, reason = _resolve_weights(adjustment, checkin_index, tz_name)
        if weights is None:
            adjustment.attributed = False
            anomalies.append(_unattributed_anomaly(adjustment, reason))
            if day_totals:
                # 无归属正向调整入账到最近一个有确认量的日期。
                weights = {max(day_totals): 1}
            else:
                # 没有任何确认签到时按事件创建日期入账，保证明细可追溯。
                assert adjustment.fallback_day is not None
                weights = {adjustment.fallback_day: 1}
        for day, share in _distribute(adjustment.seconds, weights).items():
            day_totals[day] = day_totals.get(day, 0) + share

    for adjustment in ordered:
        if adjustment.seconds >= 0:
            continue
        weights, reason = _resolve_weights(adjustment, checkin_index, tz_name)
        if weights is None:
            adjustment.attributed = False
            anomalies.append(_unattributed_anomaly(adjustment, reason))
        remaining = _deduct(day_totals, -adjustment.seconds, weights)
        if remaining > 0:
            anomalies.append(
                Anomaly(
                    anomaly_id=f"{adjustment.event_id}:overflow",
                    kind=AnomalyKind.CORRECTION_OVERFLOW,
                    event_id=adjustment.event_id,
                    student_id=adjustment.student_id,
                    seconds=remaining,
                    detail=(
                        "deduction exceeds available confirmed seconds; "
                        "surplus discarded to keep totals non-negative"
                    ),
                )
            )
    return anomalies


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
    adjustments_by_student: dict[str, list[Adjustment]] = {}

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
            adjustments_by_student.setdefault(event.student_id, []).append(
                _parse_adjustment(event, timezone_name)
            )

    all_students = set(checkins_by_student) | set(adjustments_by_student)
    students: dict[str, StudentProgress] = {}
    for student_id in all_students:
        records = checkins_by_student.get(student_id, [])
        adjustments = adjustments_by_student.get(student_id, [])

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
        adjustment_seconds = sum(a.seconds for a in adjustments)
        total_seconds = confirmed_seconds + adjustment_seconds
        if total_seconds < 0:
            total_seconds = 0

        day_totals: dict[str, int] = {}
        for start, end in merge_intervals(confirmed_intervals):
            for day, seg_start, seg_end in split_by_academic_day(
                start, end, timezone_name
            ):
                key = day.isoformat()
                day_totals[key] = day_totals.get(key, 0) + elapsed_seconds(
                    seg_start, seg_end
                )

        anomalies = _apply_adjustments(
            adjustments, day_totals, checkin_index, timezone_name
        )

        daily = [
            DayTotal(academic_day=day, seconds=secs)
            for day, secs in sorted(day_totals.items())
        ]

        students[student_id] = StudentProgress(
            student_id=student_id,
            confirmed_seconds=confirmed_seconds,
            pending_seconds=pending_seconds,
            adjustment_seconds=adjustment_seconds,
            total_seconds=total_seconds,
            lesson_units=total_seconds // (45 * 60),
            pending_lesson_units=pending_seconds // (45 * 60),
            meets_requirement=total_seconds >= required_seconds,
            daily=daily,
            checkins=sorted(records, key=lambda r: r.start_utc),
            adjustments=sorted(adjustments, key=lambda a: a.event_id),
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
