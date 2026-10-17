"""Persistence and the QueueShield detector D3 with its ablation arms (blueprint 11.2, 11.8)."""
from __future__ import annotations

import math
from collections import deque
from typing import Optional

import numpy as np

from qs.config import DetectorConfig
from qs.core.detectors import rules
from qs.core.detectors.iforest import ews_features, model_vector
from qs.schema import Alert, OperatingState

MODES = ("R", "M", "RM", "RM-RRCF", "RM-EWS")
CLEAR_TICKS = 3        # quiet ticks before a segment can raise a new alert


class Persistence:
    """k of the last n ticks. A single noisy tick never satisfies k >= 2."""

    def __init__(self, k: int = 3, n: int = 4) -> None:
        self.k, self.n = k, n
        self.hist: deque = deque(maxlen=n)

    def push(self, hit: bool) -> bool:
        self.hist.append(bool(hit))
        return sum(self.hist) >= self.k

    @property
    def fraction(self) -> float:
        return sum(self.hist) / self.n


class QueueShield:
    """D3. Modes: R rules only; M model only; RM rules + Isolation Forest (default); RM-RRCF; RM-EWS."""

    def __init__(self, cfg: DetectorConfig, model=None, mode: str = "RM", name: Optional[str] = None) -> None:
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        if mode != "R" and model is None:
            raise ValueError(f"mode {mode} needs a model")
        self.cfg, self.model, self.mode = cfg, model, mode
        self.name = name or ("D3" if mode == "RM" else f"D3:{mode}")
        self.gate = rules.SurgeGate(cfg)
        self._pers: dict = {}
        self._fast: dict = {}
        self._quiet: dict = {}
        self._qhist: dict = {}
        self.active: set = set()
        self.labels: dict = {}
        self.scores: dict = {}
        self.model_degraded: set = set()

    def _model_score(self, row) -> float:
        v = model_vector(row, self.cfg.client_timeout_s)
        if v is None:
            return math.nan
        if self.mode == "RM-EWS":
            h = self._qhist.setdefault(row.segment, deque(maxlen=self.cfg.ews_window))
            h.append(row.f["queue_depth"])
            v = np.concatenate([v, ews_features(list(h))])
        return float(self.model.score(v[None, :])[0])

    def update(self, now_ms: int, rows: list, p_stuck: Optional[dict] = None) -> list:
        cfg, out = self.cfg, []
        self.labels = {}
        for r in rows:
            seg = r.segment
            if not r.eligible:
                continue
            sigs = rules.signals(r, cfg)
            use_gate = self.mode != "M"
            label = self.gate.label(r, len(sigs)) if use_gate else None
            if label:
                self.labels[seg] = label
            score = math.nan
            if self.mode != "R":
                score = self._model_score(r)
                self.scores[seg] = score
                if math.isnan(score):
                    self.model_degraded.add(seg)
                else:
                    self.model_degraded.discard(seg)
            if self.mode == "R" or (self.mode != "M" and math.isnan(score)):
                hit = len(sigs) >= cfg.signals_required       # rule signals only (also the model-failure fallback)
            else:
                hit = score >= cfg.tau
            pers = self._pers.setdefault(seg, Persistence(cfg.persistence_k, cfg.persistence_n))
            persisted = pers.push(hit)
            fast = self._fast.get(seg, 0) + 1 if (not math.isnan(score) and score >= cfg.tau_hi
                                                  and len(sigs) >= cfg.fast_path_signals) else 0
            self._fast[seg] = fast
            warn = persisted or fast >= cfg.fast_path_ticks
            if self.mode in ("RM", "RM-RRCF", "RM-EWS") and not math.isnan(score):
                warn = warn and len(sigs) >= cfg.signals_required
            if label == "SURGE":
                warn = False
            if warn:
                self._quiet[seg] = 0
                if seg not in self.active:
                    self.active.add(seg)
                    out.append(self._alert(now_ms, r, sigs, score, pers, p_stuck))
            else:
                q = self._quiet.get(seg, 0) + 1
                self._quiet[seg] = q
                if q >= CLEAR_TICKS:
                    self.active.discard(seg)
        return out

    def _alert(self, now_ms, r, sigs, score, pers, p_stuck) -> Alert:
        arr = r.f["arrival_rate"]
        samples = 0.0 if math.isnan(arr) else arr * 10.0      # attempts in the short window
        s = 0.0 if math.isnan(score) else score
        conf = 0.4 * s + 0.3 * (len(sigs) / 5.0) + 0.2 * pers.fraction + 0.1 * min(1.0, samples / 200.0)
        crit = bool(p_stuck and p_stuck.get(r.segment, 0.0) >= self.cfg.forecast_critical)
        return Alert(ts=now_ms, detector=self.name, segment=r.segment,
                     severity=OperatingState.CRITICAL if crit else OperatingState.WARNING,
                     score=None if math.isnan(score) else score, signals=sigs, reasons=rules.reason_codes(r),
                     confidence=round(conf, 4))
