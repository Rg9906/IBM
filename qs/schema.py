"""Shared types: the only place enums and record types are defined (blueprint 8, 10.6, 14.7, 15.5)."""
from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, model_validator

SCHEMA_VERSION = 1
GATEWAYS = ("G1", "G2", "G3")
BANKS = ("B1", "B2", "B3", "B4", "B5")


class EventType(str, Enum):
    PAYMENT_INITIATED = "PAYMENT_INITIATED"
    ATTEMPT_SENT = "ATTEMPT_SENT"
    ATTEMPT_OUTCOME = "ATTEMPT_OUTCOME"
    RETRY_SCHEDULED = "RETRY_SCHEDULED"
    STATUS_CHECK_SENT = "STATUS_CHECK_SENT"
    STATUS_CHECK_RESULT = "STATUS_CHECK_RESULT"
    SERVICE_SNAPSHOT = "SERVICE_SNAPSHOT"
    ACTION_APPLIED = "ACTION_APPLIED"


class Status(str, Enum):
    SUCCESS = "SUCCESS"
    DECLINED = "DECLINED"
    REJECTED_OVERLOAD = "REJECTED_OVERLOAD"
    CLIENT_TIMEOUT = "CLIENT_TIMEOUT"
    PENDING = "PENDING"
    UNKNOWN = "UNKNOWN"
    CONFIRMED_SUCCESS = "CONFIRMED_SUCCESS"
    CONFIRMED_FAILED = "CONFIRMED_FAILED"


class PaymentState(str, Enum):
    INITIATED = "INITIATED"
    IN_FLIGHT = "IN_FLIGHT"
    PENDING_UNKNOWN = "PENDING_UNKNOWN"
    STATUS_CHECKING = "STATUS_CHECKING"
    CONFIRMED_FAILED = "CONFIRMED_FAILED"
    CONFIRMED_SUCCESS = "CONFIRMED_SUCCESS"
    ESCALATED_RECONCILE = "ESCALATED_RECONCILE"
    ABANDONED = "ABANDONED"


class Level(str, Enum):
    PLATFORM = "platform"
    GATEWAY = "gateway"
    BANK = "bank"
    ROUTE = "route"


class OperatingState(str, Enum):
    MONITORING = "MONITORING"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"
    RECOVERING = "RECOVERING"
    DEGRADED_MONITORING = "DEGRADED_MONITORING"


class IncidentState(str, Enum):
    OPEN = "OPEN"
    MITIGATING = "MITIGATING"
    RECOVERING = "RECOVERING"
    CLOSED = "CLOSED"


class Verdict(str, Enum):
    GATEWAY_SIDE = "GATEWAY_SIDE"
    BANK_SIDE = "BANK_SIDE"
    ROUTE_SPECIFIC = "ROUTE_SPECIFIC"
    MULTI_FAULT = "MULTI_FAULT"
    PLATFORM_WIDE = "PLATFORM_WIDE"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


class ActionId(str, Enum):
    THROTTLE_RETRIES = "THROTTLE_RETRIES"
    SHED_EXPIRED = "SHED_EXPIRED"
    PRIORITIZE_FRESH = "PRIORITIZE_FRESH"
    VERIFY_PENDING = "VERIFY_PENDING"
    SHIFT_TRAFFIC = "SHIFT_TRAFFIC"
    HOLD_MONITOR = "HOLD_MONITOR"
    ESCALATE_HUMAN = "ESCALATE_HUMAN"


class DecisionKind(str, Enum):
    APPROVE_ALL = "approve_all"
    APPROVE_SELECTED = "approve_selected"
    REJECT = "reject"


class ClientProfile(str, Enum):
    NAIVE = "naive"
    DISCIPLINED = "disciplined"
    NAIVE_VERIFY = "naive_verify"


class ScenarioId(str, Enum):
    S0 = "S0"
    S1 = "S1"
    S2 = "S2"
    S3 = "S3"
    S4 = "S4"
    S5 = "S5"
    S6 = "S6"
    S7 = "S7"


class ForecastState(str, Enum):
    CALM = "CALM"
    WATCH = "WATCH"
    CRITICAL = "CRITICAL"


# Segment key: (level, gateway_id, bank_id) with "*" as the wildcard dimension.
SegmentKey = tuple


def segment(level: Level, gateway_id: str = "*", bank_id: str = "*") -> SegmentKey:
    return (level.value, gateway_id, bank_id)


class Event(BaseModel):
    """Event envelope, schema version 1 (blueprint 8.1). No personal data fields exist."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int
    event_id: str
    event_type: EventType
    payment_id: str
    attempt_id: str
    attempt_no: int
    idempotency_key: Optional[str] = None
    gateway_id: str
    bank_id: str
    event_timestamp: int
    received_timestamp: Optional[int] = None
    response_time_ms: Optional[int] = None
    status: Optional[Status] = None
    error_code: Optional[str] = None
    retry_count: int
    is_retry: bool
    queue_depth: Optional[int] = None
    active_requests: Optional[int] = None
    configured_capacity: Optional[int] = None
    client_timeout_ms: Optional[int] = None

    @model_validator(mode="after")
    def _check(self) -> "Event":
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {SCHEMA_VERSION}")
        if self.gateway_id not in GATEWAYS:
            raise ValueError(f"unknown gateway_id {self.gateway_id!r}")
        if self.bank_id not in BANKS:
            raise ValueError(f"unknown bank_id {self.bank_id!r}")
        if self.attempt_no < 0 or self.retry_count < 0:
            raise ValueError("attempt_no and retry_count must be >= 0")
        if self.is_retry != (self.retry_count > 0):
            raise ValueError("is_retry must equal retry_count > 0")
        if not self.event_id or not self.payment_id or not self.attempt_id:
            raise ValueError("ids must be non-empty")
        return self

    def to_json_dict(self) -> dict[str, Any]:
        """Dict with enums as strings and None fields omitted (matches the 8.1 example)."""
        return self.model_dump(mode="json", exclude_none=True)

    @classmethod
    def from_json_dict(cls, d: dict[str, Any]) -> "Event":
        return cls.model_validate(d)


class FeatureRow(BaseModel):
    """One feature row per segment per tick (blueprint 10.6)."""

    tick_ts: int
    segment: tuple
    eligible: bool
    flags: list[str] = []
    f: dict[str, float] = {}
    z: dict[str, float] = {}
    base: dict[str, float] = {}   # baseline mean per feature before this tick (for reason codes)


class Localization(BaseModel):
    """Fault Localizer output (blueprint 14.7)."""

    verdict: Verdict
    primary: Optional[tuple] = None
    candidates: list[dict[str, Any]] = []
    confidence: float = 0.0
    total_excess: float = 0.0


class Action(BaseModel):
    """An action in a recommendation (blueprint 15.5)."""

    id: ActionId
    params: dict[str, Any] = {}
    eligible: bool = True
    blocked_by: Optional[str] = None
    scope: Optional[tuple] = None
    rec_id: Optional[str] = None


class Recommendation(BaseModel):
    """Recommendation record (blueprint 15.5)."""

    rec_id: str
    incident_id: str
    segment: tuple
    severity: OperatingState
    verdict: Verdict
    p_stuck: Optional[float] = None
    confidence: float
    countdown_s_estimate: Optional[float] = None
    actions: list[Action]
    reasons: list[str]
    model_versions: dict[str, str]
    features_sha256: str
    status: str = "pending"


class Decision(BaseModel):
    """Operator decision (blueprint 16.6, 19.5)."""

    decision: DecisionKind
    operator: str
    note: str = ""
    actions_selected: list[str] = []
    decided_ts: Optional[int] = None


class IngestResult(BaseModel):
    accepted: int = 0
    duplicates: int = 0
    rejected: int = 0
    errors: list[str] = []


class Alert(BaseModel):
    """A detector alert on a segment (blueprint 11). Raised on the rising edge of the alert condition."""

    ts: int
    detector: str                     # D0, D1, D2, D3 or an ablation arm name
    segment: tuple
    severity: OperatingState = OperatingState.WARNING
    score: Optional[float] = None
    signals: list[str] = []
    reasons: list[str] = []
    confidence: float = 0.0
    kind: str = "segment"             # "platform" for D0 (reported as unlocalised, blueprint 11.4)
