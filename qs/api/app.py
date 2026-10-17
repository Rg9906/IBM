"""HTTP API v1 (blueprint 19.5). One Pipeline behind a lock; write endpoints need the X-QS-Key header.

The key comes from QS_API_KEY. If it is unset a random key is generated at start-up and logged once, so
there is never an unauthenticated write endpoint.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import threading
import time
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from qs.api.live import LiveRun
from qs.config import OperatorConfig, PipelineConfig
from qs.core.pipeline import Pipeline
from qs.eval.arena import SimulatedOperator
from qs.schema import Decision

log = logging.getLogger("qs.api")
MAX_BATCH = 500


class State:
    def __init__(self, models_dir: str, key: Optional[str], store=None) -> None:
        self.models_dir = models_dir
        self.key = key or os.environ.get("QS_API_KEY") or secrets.token_urlsafe(16)
        if not (key or os.environ.get("QS_API_KEY")):
            log.warning("QS_API_KEY not set; generated key: %s", self.key)
        self.lock = threading.RLock()
        self.pipe = Pipeline(PipelineConfig(), store=store, models_dir=models_dir)
        self.events: list = []                 # SSE journal of dicts
        self.run: Optional[LiveRun] = None
        self.thread: Optional[threading.Thread] = None
        self.stop = threading.Event()
        self.ingest_ts: list = []
        self.applied_reports: list = []

    def emit(self, kind: str, data: dict) -> None:
        self.events.append({"id": len(self.events), "kind": kind, "data": data})
        if len(self.events) > 5000:
            del self.events[:1000]


def err(status: int, error: str, detail: str = "") -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": error, "detail": detail})


def create_app(models_dir: str = "artifacts/models", api_key: Optional[str] = None, store=None) -> FastAPI:
    st = State(models_dir, api_key, store)
    app = FastAPI(title="QueueShield", version="1")
    app.state.qs = st

    def need_key(x_qs_key: Optional[str] = Header(default=None)) -> None:
        if not x_qs_key or not secrets.compare_digest(x_qs_key, st.key):
            raise HTTPException(status_code=401, detail="missing or wrong X-QS-Key")

    @app.exception_handler(HTTPException)
    async def _http(_: Request, exc: HTTPException):
        return err(exc.status_code, "http_error", str(exc.detail))

    def after_tick(r) -> None:
        for a in r.alerts:
            st.emit("alert", {"ts": a.ts, "detector": a.detector, "segment": a.segment, "severity": a.severity.value})
        for i in r.incidents:
            st.emit("incident", {"id": i.incident_id, "segment": i.segment, "state": i.state.value,
                                 "severity": i.severity.value})
        for rec in r.recs:
            st.emit("recommendation", json.loads(rec.model_dump_json()))
        st.emit("snapshot", {"tick_ts": r.tick_ts, "state": r.state.value, "degraded": r.degraded,
                             "p_stuck": {"/".join(k): v for k, v in r.p_stuck.items()}})

    @app.post("/v1/events", status_code=202, dependencies=[Depends(need_key)])
    async def post_events(request: Request):
        try:
            body = await request.json()
        except ValueError:
            return err(400, "bad_json", "body is not valid JSON")
        if not isinstance(body, list):
            return err(400, "bad_body", "expected a JSON list of events")
        if len(body) > MAX_BATCH:
            return err(413, "batch_too_large", f"at most {MAX_BATCH} events per request")
        with st.lock:
            res = st.pipe.ingest(body)
            if st.run is None:                       # external simulator: ticks follow event time
                for r in st.pipe.advance():
                    after_tick(r)
        return res.model_dump()

    @app.get("/v1/state")
    def get_state():
        with st.lock:
            last = st.pipe.last
            segs = {}
            if last:
                for r in last.rows:
                    segs["/".join(r.segment)] = {"eligible": r.eligible, "flags": r.flags,
                                                 "f": {k: (None if v != v else v) for k, v in r.f.items()},
                                                 "score": last.scores.get(r.segment), "p_stuck": last.p_stuck.get(r.segment)}
            return {"tick_ts": last.tick_ts if last else None, "state": last.state.value if last else "MONITORING",
                    "degraded": last.degraded if last else [], "segments": segs,
                    "verdict": last.localization.model_dump(mode="json") if last and last.localization else None}

    @app.get("/v1/incidents")
    def incidents(state: Optional[str] = None, limit: int = 100):
        with st.lock:
            out = [{"id": i.incident_id, "segment": i.segment, "state": i.state.value, "severity": i.severity.value,
                    "opened_ts": i.opened_ts, "closed_ts": i.closed_ts}
                   for i in st.pipe.lifecycle.incidents.values() if state is None or i.state.value == state]
        return out[-limit:]

    @app.get("/v1/recommendations")
    def recommendations(status: Optional[str] = None):
        with st.lock:
            return [json.loads(r.model_dump_json()) for r in st.pipe.recommendations.values()
                    if status is None or r.status == status]

    @app.post("/v1/recommendations/{rec_id}/decision", dependencies=[Depends(need_key)])
    async def decision(rec_id: str, request: Request):
        try:
            dec = Decision.model_validate(await request.json())
        except Exception as exc:
            return err(422, "bad_decision", str(exc)[:200])
        with st.lock:
            if rec_id not in st.pipe.actions.recs:
                return err(404, "unknown_recommendation", rec_id)
            try:
                acts = st.pipe.decide(rec_id, dec)
            except ValueError as exc:
                return err(409, "decision_rejected", str(exc))
            st.emit("decision", {"rec_id": rec_id, "operator": dec.operator, "decision": dec.decision.value})
            return {"actions": [json.loads(a.model_dump_json()) for a in acts]}

    @app.get("/v1/actions/pending", dependencies=[Depends(need_key)])
    def pending(since: int = 0):
        with st.lock:
            return [json.loads(a.model_dump_json()) for a in st.pipe.pending_actions()]

    @app.post("/v1/actions/{action_id}/applied", dependencies=[Depends(need_key)])
    async def applied(action_id: str, request: Request):
        body = await request.json()
        with st.lock:
            st.applied_reports.append({"action": action_id, **body})
            st.emit("applied", {"action": action_id, **body})
        return {"ok": True}

    @app.post("/v1/sim/scenario", status_code=202, dependencies=[Depends(need_key)])
    async def scenario(request: Request):
        body = await request.json()
        sc, seed, speed = body.get("scenario", "S4"), int(body.get("seed", 1)), float(body.get("speed", 20.0))
        if sc not in {"S0", "S1", "S2", "S3", "S4", "S5", "S6", "S7"}:
            return err(422, "bad_scenario", sc)
        with st.lock:
            if st.thread and st.thread.is_alive():
                st.stop.set()
        if st.thread:
            st.thread.join(timeout=5)
        st.stop = threading.Event()
        with st.lock:
            st.pipe = Pipeline(PipelineConfig(), store=store, models_dir=models_dir)
            op = SimulatedOperator(OperatorConfig(approval_delay_ms=int(body.get("operator_delay_ms", 20000)))) \
                if body.get("auto_operator", False) else None
            st.run = LiveRun(sc, seed, body.get("arm", "U3"), pipeline=st.pipe, operator=op, on_tick=after_tick)
            st.events.clear()
        stop = st.stop

        def loop() -> None:
            while not st.run.done() and not stop.is_set():
                t0 = time.monotonic()
                with st.lock:
                    st.run.step(1000)
                time.sleep(max(0.0, 1.0 / max(speed, 0.1) - (time.monotonic() - t0)))
        st.thread = threading.Thread(target=loop, daemon=True)
        st.thread.start()
        return {"scenario": sc, "seed": seed, "speed": speed}

    @app.get("/v1/stream")
    async def stream(request: Request, last_id: int = -1):
        async def gen():
            cur = last_id
            while not await request.is_disconnected():
                with st.lock:
                    new = [e for e in st.events if e["id"] > cur]
                for e in new:
                    cur = e["id"]
                    yield f"id: {e['id']}\nevent: {e['kind']}\ndata: {json.dumps(e['data'], default=str)}\n\n"
                await asyncio.sleep(0.25)
        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.get("/v1/metrics")
    def metrics():
        with st.lock:
            m = dict(st.pipe.ing.metrics)
        return {"ingest": m, "pending_in_buffer": st.pipe.ing.pending(),
                "models": st.pipe.model_versions, "detector_mode": st.pipe.d3.mode}

    @app.get("/v1/health")
    def health():
        with st.lock:
            last = st.pipe.last
        return {"status": "ok", "global_state": last.state.value if last else "MONITORING"}

    return app
