"""服务端业务模块。"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class PlanIn(BaseModel):
    plan_version: str = Field(..., min_length=1, max_length=128)
    iana_timezone: str = Field(..., min_length=1, max_length=64)
    required_seconds: int = Field(0, ge=0)


class PlanOut(BaseModel):
    plan_version: str
    iana_timezone: str
    required_seconds: int


class CheckinPayload(BaseModel):
    activity_id: str = ""
    activity_type: str = "regular"
    check_in_at: datetime
    check_out_at: datetime

    @model_validator(mode="after")
    def _check_order(self) -> "CheckinPayload":
        if self.check_out_at <= self.check_in_at:
            raise ValueError("check_out_at must be after check_in_at")
        return self

    @field_validator("check_in_at", "check_out_at")
    @classmethod
    def _ensure_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware (RFC 3339)")
        return v


class MentorConfirmPayload(BaseModel):
    checkin_event_id: str


class LeaveCorrectionPayload(BaseModel):
    adjustment_seconds: int
    reason: str = ""
    checkin_event_id: str | None = Field(default=None, min_length=1, max_length=128)
    academic_day: date | None = None

    @model_validator(mode="after")
    def _exactly_one_target(self) -> "LeaveCorrectionPayload":
        if self.checkin_event_id is not None and self.academic_day is not None:
            raise ValueError(
                "checkin_event_id and academic_day are mutually exclusive"
            )
        if self.checkin_event_id is None and self.academic_day is None:
            raise ValueError(
                "leave_correction must reference checkin_event_id or academic_day"
            )
        return self


_PAYLOAD_MODELS = {
    "checkin": CheckinPayload,
    "mentor_confirm": MentorConfirmPayload,
    "leave_correction": LeaveCorrectionPayload,
}


class EventIn(BaseModel):
    event_id: str = Field(..., min_length=1, max_length=128)
    event_type: Literal["checkin", "mentor_confirm", "leave_correction"]
    student_id: str = Field(..., min_length=1, max_length=128)
    payload: dict[str, Any]

    @model_validator(mode="after")
    def _validate_typed_payload(self) -> "EventIn":
        model = _PAYLOAD_MODELS[self.event_type]
        validated = model.model_validate(self.payload)
        object.__setattr__(self, "payload", validated.model_dump(mode="json"))
        return self


class EventBatchIn(BaseModel):
    events: list[EventIn]


class EventOut(BaseModel):
    event_id: str
    plan_version: str
    event_type: str
    student_id: str
    payload: dict[str, Any]
    created_at: datetime

    model_config = {"from_attributes": True}


class ImportResult(BaseModel):
    accepted: int
    duplicates: list[str]
    rejected: list[dict[str, Any]]


class DailyTotal(BaseModel):
    academic_day: str | None
    seconds: int


class CheckinExplanation(BaseModel):
    event_id: str
    activity_id: str
    activity_type: str
    status: str
    counts: bool
    check_in_at_utc: str
    check_out_at_utc: str
    raw_seconds: int
    academic_days: list[dict[str, Any]]


class DayAllocationOut(BaseModel):
    academic_day: str | None
    seconds: int


class AdjustmentOut(BaseModel):
    event_id: str
    seconds: int
    applied_seconds: int
    reason: str
    target_type: str
    checkin_event_id: str | None
    academic_day: str | None
    allocations: list[DayAllocationOut]
    anomaly_codes: list[str]


class AnomalyOut(BaseModel):
    event_id: str
    student_id: str
    code: str
    message: str
    attempted_seconds: int
    applied_seconds: int
    detail: dict[str, Any]


class StudentProgressOut(BaseModel):
    student_id: str
    confirmed_seconds: int
    pending_seconds: int
    adjustment_seconds: int
    allocated_adjustment_seconds: int
    total_seconds: int
    lesson_units: int
    pending_lesson_units: int
    meets_requirement: bool
    daily: list[DailyTotal]
    checkins: list[CheckinExplanation]
    adjustments: list[AdjustmentOut]
    anomalies: list[AnomalyOut]


class SnapshotOut(BaseModel):
    plan_version: str
    freeze_id: str | None
    timezone: str
    required_seconds: int
    generated_at: str
    event_cutoff_id: str | None
    students: list[dict[str, Any]]


class FreezeIn(BaseModel):
    pass


class DiffOut(BaseModel):
    plan_version: str
    old_freeze_id: str | None
    new_freeze_id: str | None
    old_generated_at: str
    new_generated_at: str
    old_event_cutoff_id: str | None
    new_event_cutoff_id: str | None
    student_changes: list[dict[str, Any]]
    students_affected: int
