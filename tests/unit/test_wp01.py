import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from qs import ids
from qs.clock import SimClock, WallClock
from qs.config import (ConfigError, GovernorConfig, OperatorConfig, PipelineConfig,
                       PolicyConfig, SimConfig, DetectorConfig, from_dict, load_yaml)
from qs.schema import Event, EventType, Status, segment, Level, Recommendation, Action, ActionId

ROOT = Path(__file__).resolve().parents[2]

EXAMPLE = {
    "schema_version": 1,
    "event_id": "run42-0001837",
    "event_type": "ATTEMPT_OUTCOME",
    "payment_id": "p-000912",
    "attempt_id": "p-000912-a1",
    "attempt_no": 1,
    "idempotency_key": "ik-000912",
    "gateway_id": "G1",
    "bank_id": "B3",
    "event_timestamp": 183450,
    "response_time_ms": 3000,
    "status": "CLIENT_TIMEOUT",
    "error_code": "E_CLIENT_TIMEOUT",
    "retry_count": 1,
    "is_retry": True,
    "queue_depth": 311,
    "active_requests": 40,
    "configured_capacity": 40,
}


def test_example_round_trips():
    ev = Event.from_json_dict(EXAMPLE)
    assert ev.event_type is EventType.ATTEMPT_OUTCOME and ev.status is Status.CLIENT_TIMEOUT
    assert ev.to_json_dict() == EXAMPLE
    assert Event.from_json_dict(json.loads(json.dumps(ev.to_json_dict()))) == ev


@pytest.mark.parametrize("field,value", [
    ("event_type", "NOPE"), ("status", "NOPE"), ("gateway_id", "G9"), ("bank_id", "B9"),
    ("schema_version", 2), ("is_retry", False), ("retry_count", -1),
])
def test_invalid_values_rejected(field, value):
    d = dict(EXAMPLE, **{field: value})
    with pytest.raises(ValidationError):
        Event.from_json_dict(d)


@pytest.mark.parametrize("field", ["event_id", "event_type", "payment_id", "attempt_id", "attempt_no",
                                   "gateway_id", "bank_id", "event_timestamp", "retry_count", "is_retry",
                                   "schema_version"])
def test_missing_required_rejected(field):
    d = dict(EXAMPLE)
    del d[field]
    with pytest.raises(ValidationError):
        Event.from_json_dict(d)


def test_unknown_field_rejected():
    with pytest.raises(ValidationError):
        Event.from_json_dict(dict(EXAMPLE, card_number="x"))


def test_optional_fields_omitted():
    d = {k: v for k, v in EXAMPLE.items() if k in (
        "schema_version", "event_id", "event_type", "payment_id", "attempt_id", "attempt_no",
        "gateway_id", "bank_id", "event_timestamp", "retry_count", "is_retry")}
    d["retry_count"], d["is_retry"], d["attempt_no"] = 0, False, 0
    assert Event.from_json_dict(d).to_json_dict() == d


def test_segment_and_recommendation_record():
    assert segment(Level.GATEWAY, "G1") == ("gateway", "G1", "*")
    rec = Recommendation(rec_id="rec-0007", incident_id="inc-0003", segment=segment(Level.GATEWAY, "G1"),
                         severity="CRITICAL", verdict="GATEWAY_SIDE", p_stuck=0.62, confidence=0.81,
                         actions=[Action(id=ActionId.SHED_EXPIRED)], reasons=["TIMEOUT_UP"],
                         model_versions={"detector": "x"}, features_sha256="ab")
    assert Recommendation.model_validate_json(rec.model_dump_json()) == rec


def test_ids():
    assert ids.event_id("run42", 1837) == "run42-0001837"
    assert ids.attempt_id(ids.payment_id(912), 1) == "p-000912-a1"
    assert ids.parse_attempt_id("p-000912-a1") == ("p-000912", 1)
    with pytest.raises(ValueError):
        ids.parse_attempt_id("garbage")
    g1, g2 = ids.IdGenerator("r"), ids.IdGenerator("r")
    assert [g1.next_event_id() for _ in range(3)] == [g2.next_event_id() for _ in range(3)] == ["r-0000001", "r-0000002", "r-0000003"]
    assert g1.next_rec_id() == "rec-0001" and g1.next_incident_id() == "inc-0001"


def test_sim_clock_monotonic():
    c = SimClock()
    c.advance_to(100)
    c.advance_by(50)
    assert c.now_ms() == 150
    with pytest.raises(ValueError):
        c.advance_to(10)
    with pytest.raises(ValueError):
        c.advance_by(-1)
    assert WallClock().now_ms() >= 0


def test_config_defaults_and_yaml():
    sim = load_yaml(SimConfig, ROOT / "configs" / "sim_default.yaml")
    assert sim.workers == 40 and sim.queue_max == 3000 and sim.client_timeout_ms == 3000
    assert sum(sim.bank_shares) == pytest.approx(1.0)
    gov = load_yaml(GovernorConfig, ROOT / "configs" / "governor.yaml")
    assert gov.max_checks_per_payment == 3 and gov.stop_codes == ()
    load_yaml(PolicyConfig, ROOT / "configs" / "policy.yaml")
    load_yaml(OperatorConfig, ROOT / "configs" / "operators.yaml")
    load_yaml(DetectorConfig, ROOT / "configs" / "detectors_frozen.yaml")
    load_yaml(GovernorConfig, ROOT / "configs" / "rails" / "upi_like.yaml")


def test_config_rejects_unknown_and_is_frozen():
    with pytest.raises(ConfigError):
        from_dict(SimConfig, {"wrokers": 1})
    cfg = from_dict(PipelineConfig, {"governor": {"max_checks_per_payment": 5}})
    assert cfg.governor.max_checks_per_payment == 5 and cfg.features.ewma_alpha == 0.02
    with pytest.raises(Exception):
        cfg.tick_ms = 1  # type: ignore[misc]


def test_pipeline_stub_importable_and_raises():
    from qs.core.pipeline import Pipeline
    with pytest.raises(NotImplementedError):
        Pipeline(PipelineConfig())
