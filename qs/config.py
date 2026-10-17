"""Frozen configuration dataclasses loaded from configs/*.yaml (blueprint 19.6).

Defaults mirror the blueprint tables (9.6, 10, 11, 12.2, 15). YAML files override them;
unknown keys are rejected so a typo in a config cannot pass silently.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Type, TypeVar

import yaml

T = TypeVar("T")


@dataclass(frozen=True)
class SimConfig:
    n_gateways: int = 3
    n_banks: int = 5
    bank_shares: tuple = (0.30, 0.25, 0.20, 0.15, 0.10)
    workers: int = 40
    queue_max: int = 3000
    gateway_service_mean_ms: float = 100.0
    gateway_service_cv: float = 0.5
    bank_service_mean_ms: float = 300.0
    bank_service_cv: float = 0.6
    fresh_rate_per_s: float = 165.0
    client_timeout_ms: int = 3000
    p_retry: float = 0.9
    hard_decline_rate: float = 0.02
    overload_reject_ms: int = 5
    status_check_cost_frac: float = 0.25
    run_length_s: int = 600
    warmup_s: int = 120
    tail_s: int = 120
    tick_ms: int = 5000
    snapshot_interval_ms: int = 1000
    retry_throttle_honour_frac: float = 0.70
    starvation_guard_ms: int = 2000


@dataclass(frozen=True)
class FeatureConfig:
    short_window_s: int = 10
    long_window_s: int = 60
    ring_buckets: int = 120
    latency_bins: int = 64
    latency_min_ms: float = 10.0
    latency_max_ms: float = 60000.0
    watermark_ms: int = 2000
    ewma_alpha: float = 0.02
    warmup_s: int = 120
    z_clip: float = 6.0
    route_min_attempts: int = 30
    min_latency_samples: int = 20
    queue_growth_window_s: int = 30
    orbit_window_s: int = 15
    stale_after_ms: int = 10000
    est_service_ms: float = 400.0
    prior_weight: int = 3


@dataclass(frozen=True)
class DetectorConfig:
    theta0: float = 0.90
    sensitivity: float = 1.0
    cusum_h: float = 5.0
    tau: float = 0.99
    tau_hi: float = 0.999
    persistence_k: int = 3
    persistence_n: int = 4
    fast_path_ticks: int = 2
    forecast_critical: float = 0.5
    iforest_trees: int = 200
    iforest_max_samples: int = 256
    # D3 signal thresholds (blueprint 11.2)
    z_lat: float = 2.5
    z_to: float = 3.0
    to_abs: float = 0.02
    raf_mult: float = 1.25
    z_rej: float = 3.0
    rej_abs: float = 0.01
    q_mult: float = 1.5
    q_min_delay_s: float = 0.05
    surge_z: float = 2.5
    surge_max_signals: int = 1
    queue_growth_eps: float = 0.5   # items/s below which the queue counts as not growing
    signals_required: int = 2
    fast_path_signals: int = 3
    client_timeout_s: float = 3.0
    # D1 and D2 (blueprint 11.1)
    d1_timeout_a: float = 0.05
    d1_fail_b: float = 0.10
    d1_consecutive: int = 2
    d2_k: float = 0.5
    rrcf_trees: int = 20
    rrcf_tree_size: int = 128
    ews_window: int = 12


@dataclass(frozen=True)
class LifecycleConfig:
    merge_window_ms: int = 30000
    reopen_window_ms: int = 60000
    clean_ticks_to_close: int = 6
    improving_ticks: int = 3
    model_budget_ms: int = 50
    batch_rows: int = 500
    batch_ms: int = 250


@dataclass(frozen=True)
class GovernorConfig:
    first_check_delay_ms: int = 10000
    check_backoff_factor: float = 1.5
    jitter: float = 0.20
    max_checks_per_payment: int = 3
    horizon_ms: int = 120000
    segment_budget_fraction: float = 0.10
    segment_budget_min_per_s: float = 1.0
    segment_budget_max_per_s: float = 10.0
    segment_budget_burst_s: float = 2.0
    breaker_open_ms: int = 15000
    stop_codes: tuple = ()


@dataclass(frozen=True)
class PolicyConfig:
    throttle_rate_fraction: float = 0.10
    throttle_retry_after_ms: int = 2000
    throttle_retry_after_max_ms: int = 8000
    starvation_guard_ms: int = 2000
    shift_max_fraction: float = 0.25
    shift_ramp_ms: int = 10000
    shift_min_source_share: float = 0.05
    shift_min_spare: float = 0.30
    rollback_clean_ticks: int = 6
    localizer_hi: float = 0.67
    localizer_lo: float = 0.50
    approve_min_confidence: float = 0.6


@dataclass(frozen=True)
class OperatorConfig:
    approval_delay_ms: int = 20000
    sweep_delays_ms: tuple = (5000, 15000, 30000, 60000, 120000)
    min_confidence: float = 0.6


@dataclass(frozen=True)
class PipelineConfig:
    features: FeatureConfig = field(default_factory=FeatureConfig)
    detectors: DetectorConfig = field(default_factory=DetectorConfig)
    lifecycle: LifecycleConfig = field(default_factory=LifecycleConfig)
    governor: GovernorConfig = field(default_factory=GovernorConfig)
    policy: PolicyConfig = field(default_factory=PolicyConfig)
    tick_ms: int = 5000


class ConfigError(ValueError):
    pass


def _coerce(tp: Any, value: Any) -> Any:
    # YAML lists become tuples for tuple-typed fields so frozen configs stay hashable.
    if isinstance(value, list):
        return tuple(value)
    return value


def from_dict(cls: Type[T], data: Optional[dict]) -> T:
    """Build a frozen dataclass from a mapping, rejecting unknown keys, recursing into nested configs."""
    data = dict(data or {})
    fields = {f.name: f for f in dataclasses.fields(cls)}  # type: ignore[arg-type]
    unknown = set(data) - set(fields)
    if unknown:
        raise ConfigError(f"{cls.__name__}: unknown keys {sorted(unknown)}")
    kwargs: dict[str, Any] = {}
    for name, value in data.items():
        default = fields[name].default_factory() if fields[name].default_factory is not dataclasses.MISSING else None  # type: ignore[misc]
        if dataclasses.is_dataclass(default):
            kwargs[name] = from_dict(type(default), value)
        else:
            kwargs[name] = _coerce(fields[name].type, value)
    return cls(**kwargs)


def load_yaml(cls: Type[T], path: "str | Path") -> T:
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if data is not None and not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    return from_dict(cls, data)
