"""Incident lifecycle and degraded-monitoring state (blueprint 11.4, 11.5).

Segments are (level, gateway, bank) tuples with "*" as a wildcard. Two segments overlap when their gateway
and bank parts are compatible. D0 platform alerts are kept apart as unlocalised and never merged.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from qs.config import LifecycleConfig
from qs.schema import Alert, IncidentState, OperatingState

_SEV = {OperatingState.WARNING: 1, OperatingState.CRITICAL: 2}


def overlaps(a: tuple, b: tuple) -> bool:
    return all(x == y or x == "*" or y == "*" for x, y in zip(a[1:], b[1:]))


@dataclass
class Incident:
    incident_id: str
    segment: tuple
    state: IncidentState
    opened_ts: int
    severity: OperatingState = OperatingState.WARNING
    closed_ts: Optional[int] = None
    last_warning_ts: int = 0
    clean_ticks: int = 0
    improving_ticks: int = 0
    reopened: int = 0
    merged_segments: tuple = ()


class Lifecycle:
    def __init__(self, cfg: Optional[LifecycleConfig] = None, prefix: str = "inc") -> None:
        self.cfg = cfg or LifecycleConfig()
        self.prefix = prefix
        self.incidents: dict = {}
        self.unlocalised: list = []
        self._n = 0
        self.changed: list = []          # incidents touched by the last call, for the store

    def _live(self) -> list:
        return [i for i in self.incidents.values() if i.state != IncidentState.CLOSED]

    def on_alert(self, alert: Alert, primary: Optional[tuple] = None) -> Incident | None:
        """Route one alert. `primary` is the Fault Localizer's segment (WP-09); defaults to the alert segment."""
        if alert.kind == "platform":
            self.unlocalised.append(alert)
            return None
        now, seg = alert.ts, primary or alert.segment
        inc = next((i for i in self._live()
                    if overlaps(i.segment, seg) and now - i.last_warning_ts <= self.cfg.merge_window_ms), None)
        if inc is None:
            inc = next((i for i in self.incidents.values()
                        if i.state == IncidentState.CLOSED and i.segment == seg
                        and now - i.closed_ts <= self.cfg.reopen_window_ms), None)
            if inc is not None:
                inc.state, inc.closed_ts, inc.reopened = IncidentState.OPEN, None, inc.reopened + 1
        if inc is None:
            self._n += 1
            inc = Incident(f"{self.prefix}-{self._n:04d}", seg, IncidentState.OPEN, now, alert.severity)
            self.incidents[inc.incident_id] = inc
        else:
            if seg not in inc.merged_segments and seg != inc.segment:
                inc.merged_segments += (seg,)
            if _SEV.get(alert.severity, 0) > _SEV.get(inc.severity, 0):
                inc.severity = alert.severity      # CRITICAL raises severity; the state stays OPEN until a decision
            if inc.state == IncidentState.RECOVERING:
                inc.state = IncidentState.MITIGATING          # deterioration
        inc.last_warning_ts, inc.clean_ticks, inc.improving_ticks = now, 0, 0
        self.changed.append(inc)
        return inc

    def approve(self, incident_id: str) -> None:
        """An operator approved at least one action: OPEN -> MITIGATING."""
        inc = self.incidents[incident_id]
        if inc.state == IncidentState.OPEN:
            inc.state = IncidentState.MITIGATING
            self.changed.append(inc)

    def tick(self, now_ms: int, active_segments: set) -> list:
        """Advance every live incident. A tick is clean when no overlapping segment is still alerting."""
        for inc in self._live():
            if any(overlaps(inc.segment, s) for s in active_segments):
                inc.clean_ticks = inc.improving_ticks = 0
                continue
            inc.clean_ticks += 1
            inc.improving_ticks += 1
            before = inc.state
            if inc.state == IncidentState.MITIGATING and inc.improving_ticks >= self.cfg.improving_ticks:
                inc.state = IncidentState.RECOVERING
            if inc.clean_ticks >= self.cfg.clean_ticks_to_close:
                inc.state, inc.closed_ts = IncidentState.CLOSED, now_ms
            if inc.state != before:
                self.changed.append(inc)
        out, self.changed = self.changed, []
        return out


class DegradedMonitor:
    """Returns the reasons monitoring is impaired. The system never reports healthy when it cannot see."""

    def __init__(self, cfg: Optional[LifecycleConfig] = None, silence_ms: int = 10_000) -> None:
        self.cfg = cfg or LifecycleConfig()
        self.silence_ms = silence_ms
        self._last_tick: Optional[int] = None
        self._slow_run = 0

    def model_call(self, elapsed_ms: float, failed: bool = False) -> None:
        bad = failed or (isinstance(elapsed_ms, float) and math.isnan(elapsed_ms)) \
            or elapsed_ms > self.cfg.model_budget_ms
        self._slow_run = self._slow_run + 1 if bad else 0

    def check(self, tick_ts: int, last_event_ts: Optional[int], rows: list, vector_ok: Optional[dict] = None) -> list:
        """rows are FeatureRows; vector_ok maps segment -> False when over half its model inputs are NaN."""
        reasons = []
        if last_event_ts is None or tick_ts - last_event_ts >= self.silence_ms:
            reasons.append("NO_EVENTS")
        quiet = [r.segment for r in rows if "no_data" in r.flags and "warming_up" not in r.flags
                 and r.segment[0] != "platform" and r.base.get("arrival_rate", 0) and r.base["arrival_rate"] > 0]
        if quiet:
            reasons.append("SEGMENT_SILENT")
        if vector_ok and any(not ok for ok in vector_ok.values()):
            reasons.append("MODEL_INPUTS_NAN")
        if self._slow_run >= 2:
            reasons.append("MODEL_FAILURE")
        if self._last_tick is not None and tick_ts <= self._last_tick:
            reasons.append("CLOCK_STALL_OR_BACKWARDS")
        if self._last_tick is None or tick_ts > self._last_tick:
            self._last_tick = tick_ts
        return reasons

    @staticmethod
    def state(reasons: list) -> OperatingState:
        return OperatingState.DEGRADED_MONITORING if reasons else OperatingState.MONITORING
