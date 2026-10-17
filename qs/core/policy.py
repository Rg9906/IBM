"""Deterministic policy engine (blueprint 15): turn incident, verdict, forecast and monitoring state into a ranked,
auditable list of bounded actions. Models detect and forecast; these rules decide what is eligible."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Optional

from qs.config import PolicyConfig
from qs.core.localizer import eligibility
from qs.schema import Action, ActionId, ForecastState, Localization, OperatingState, Recommendation, Verdict

AID = ActionId
LIGHT_THROTTLE = 0.50           # "light setting" for WARNING (share of the fresh rate still allowed for retries)
BLOCKED_REASONS = {"blocked": "verdict forbids it", "not_recommended": "insufficient evidence"}


@dataclass
class GatewayInfo:
    arrival_rate: float
    est_capacity: float
    forecast: ForecastState = ForecastState.CALM
    open_incident: bool = False
    configured: bool = True
    traffic_share: float = 1.0       # current routing weight, for the 5 percent source floor


@dataclass
class PolicyContext:
    incident_id: str
    segment: tuple
    severity: OperatingState
    localization: Localization
    confidence: float
    degraded: bool = False
    surge: bool = False
    p_stuck: Optional[float] = None
    countdown_s: Optional[float] = None
    reasons: list = field(default_factory=list)
    model_versions: dict = field(default_factory=dict)
    features_sha256: str = ""
    gateways: dict = field(default_factory=dict)            # gateway id -> GatewayInfo
    bank_healthy_elsewhere: Optional[bool] = None            # for ROUTE_SPECIFIC: bank fine via the other gateways
    last_shift_ts: Optional[int] = None
    now_ms: int = 0
    shift_spacing_ms: int = 10_000


def features_hash(rows: list) -> str:
    """SHA-256 over the feature dicts that fed a recommendation (SI-6)."""
    blob = json.dumps([[list(r.segment), {k: (None if v != v else round(float(v), 6)) for k, v in r.f.items()}]
                       for r in rows], sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


def shift_checks(ctx: PolicyContext, cfg: PolicyConfig, source: str, fraction: float) -> Optional[str]:
    """Return None if every 15.3 check passes, else the name of the first failing check."""
    v = ctx.localization.verdict
    if v in (Verdict.BANK_SIDE, Verdict.MULTI_FAULT, Verdict.PLATFORM_WIDE, Verdict.INSUFFICIENT_EVIDENCE):
        return "verdict " + v.value + (" (shared bank)" if v is Verdict.BANK_SIDE else "")
    if v is Verdict.ROUTE_SPECIFIC and not ctx.bank_healthy_elsewhere:
        return "bank not healthy via the other gateways"
    src = ctx.gateways.get(source)
    targets = {g: i for g, i in ctx.gateways.items() if g != source and i.configured}
    if src is None or not targets:
        return "no configured target gateway"
    if fraction > cfg.shift_max_fraction + 1e-9:
        return "step above max fraction"
    if src.traffic_share - fraction < cfg.shift_min_source_share - 1e-9:
        return "source would drop below 5 percent trickle"
    if ctx.last_shift_ts is not None and ctx.now_ms - ctx.last_shift_ts < ctx.shift_spacing_ms:
        return "previous step less than 10 s ago"
    moved = src.arrival_rate * fraction / len(targets)
    for g, info in targets.items():
        if info.open_incident:
            return f"target {g} has an open incident"
        if info.forecast is not ForecastState.CALM:
            return f"target {g} forecast {info.forecast.value}"
        spare = 1.0 - (info.arrival_rate + moved) / max(info.est_capacity, 1e-9)
        if spare < cfg.shift_min_spare:
            return f"target {g} spare capacity {spare:.0%} below {cfg.shift_min_spare:.0%}"
    return None


class PolicyEngine:
    def __init__(self, cfg: Optional[PolicyConfig] = None) -> None:
        self.cfg = cfg or PolicyConfig()
        self._n = 0

    # ------------------------------------------------------------------ scopes
    @staticmethod
    def _scope_gateway(seg: tuple) -> tuple:
        return ("gateway", seg[1], "*") if seg[1] != "*" else ("platform", "*", "*")

    def _throttle(self, scope: tuple, light: bool = False) -> Action:
        frac = LIGHT_THROTTLE if light else self.cfg.throttle_rate_fraction
        return Action(id=AID.THROTTLE_RETRIES, scope=scope,
                      params={"rate_fraction": frac, "retry_after_ms": self.cfg.throttle_retry_after_ms})

    def _simple(self, aid: ActionId, scope: tuple, params: Optional[dict] = None) -> Action:
        return Action(id=aid, scope=scope, params=params or {})

    def _shift(self, ctx: PolicyContext) -> Action:
        src = ctx.segment[1]
        frac = self.cfg.shift_max_fraction
        why = shift_checks(ctx, self.cfg, src, frac)
        scope = ctx.segment if ctx.localization.verdict is Verdict.ROUTE_SPECIFIC else ("gateway", src, "*")
        return Action(id=AID.SHIFT_TRAFFIC, scope=scope, eligible=why is None, blocked_by=why,
                      params={"from_gateway": src, "fraction": frac, "ramp_ms": self.cfg.shift_ramp_ms})

    # ------------------------------------------------------------------ rules (15.2)
    def recommend(self, ctx: PolicyContext) -> Recommendation:
        v, sev = ctx.localization.verdict, ctx.severity
        plat = ("platform", "*", "*")
        seg = ctx.segment
        gscope = self._scope_gateway(seg)
        el = eligibility(v)
        acts: list = []
        note = None
        if ctx.degraded:                                                                    # SI-4
            acts = [self._simple(AID.HOLD_MONITOR, seg), self._simple(AID.ESCALATE_HUMAN, seg, {"reason": "monitoring impaired"})]
        elif ctx.surge and sev is not OperatingState.CRITICAL:
            acts = [self._simple(AID.HOLD_MONITOR, seg)]
            note = "demand surge, queue draining, no action"
        elif v is Verdict.ROUTE_SPECIFIC:
            acts = [self._throttle(seg), self._simple(AID.VERIFY_PENDING, seg), self._shift(ctx)]
        elif v in (Verdict.MULTI_FAULT, Verdict.PLATFORM_WIDE):
            acts = [self._throttle(plat), self._simple(AID.VERIFY_PENDING, plat),
                    self._simple(AID.ESCALATE_HUMAN, seg, {"reason": v.value})]
        elif sev is OperatingState.CRITICAL and v is Verdict.GATEWAY_SIDE:
            acts = [self._simple(AID.SHED_EXPIRED, gscope), self._throttle(gscope),
                    self._simple(AID.VERIFY_PENDING, gscope), self._simple(AID.PRIORITIZE_FRESH, gscope),
                    self._shift(ctx)]
        elif sev is OperatingState.CRITICAL and v is Verdict.BANK_SIDE:
            bank = ctx.localization.primary or seg
            blocked = Action(id=AID.SHIFT_TRAFFIC, scope=bank, eligible=False, blocked_by="shared bank",
                             params={"from_gateway": "*", "fraction": self.cfg.shift_max_fraction})
            acts = [self._throttle(bank), self._simple(AID.SHED_EXPIRED, plat), self._simple(AID.VERIFY_PENDING, bank),
                    self._simple(AID.PRIORITIZE_FRESH, plat), blocked]
        elif sev is OperatingState.WARNING or v is Verdict.INSUFFICIENT_EVIDENCE:
            scope = seg if seg[0] != "platform" else plat
            acts = [self._simple(AID.VERIFY_PENDING, scope), self._throttle(scope, light=True),
                    self._simple(AID.HOLD_MONITOR, seg)]
        else:                                                    # CRITICAL with no specific rule: be conservative
            acts = [self._throttle(plat), self._simple(AID.VERIFY_PENDING, plat),
                    self._simple(AID.ESCALATE_HUMAN, seg, {"reason": "no specific rule"})]
        acts = [self._gate(a, el, ctx) for a in acts]
        self._n += 1
        rid = f"rec-{self._n:04d}"
        for a in acts:
            a.rec_id = rid
        reasons = list(ctx.reasons) + ([note] if note else [])
        if v is not Verdict.INSUFFICIENT_EVIDENCE:
            reasons.append(f"verdict {v.value}" + (f" at {'/'.join(ctx.localization.primary)}" if ctx.localization.primary else ""))
        return Recommendation(rec_id=rid, incident_id=ctx.incident_id, segment=seg, severity=sev, verdict=v,
                              p_stuck=ctx.p_stuck, confidence=ctx.confidence, countdown_s_estimate=ctx.countdown_s,
                              actions=acts, reasons=reasons, model_versions=ctx.model_versions,
                              features_sha256=ctx.features_sha256)

    @staticmethod
    def _gate(a: Action, el: dict, ctx: PolicyContext) -> Action:
        """Apply the verdict eligibility table (14.5) on top of the rule's own flags. Never upgrades a block."""
        if ctx.degraded and a.id not in (AID.HOLD_MONITOR, AID.ESCALATE_HUMAN):
            a.eligible, a.blocked_by = False, "degraded monitoring"
            return a
        status = el.get(a.id, "eligible")
        if status in BLOCKED_REASONS and a.eligible:
            a.eligible, a.blocked_by = False, BLOCKED_REASONS[status]
        return a


def safe_to_shift(rec: Recommendation) -> bool:
    """SI-2 helper for tests: a SHIFT_TRAFFIC marked eligible implies its verdict allows it."""
    for a in rec.actions:
        if a.id is AID.SHIFT_TRAFFIC and a.eligible:
            if rec.verdict not in (Verdict.GATEWAY_SIDE, Verdict.ROUTE_SPECIFIC):
                return False
    return True

