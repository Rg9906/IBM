"""HTTP API v1 (blueprint 19.5). One Pipeline behind a lock; write endpoints need the X-QS-Key header.

The key comes from QS_API_KEY. If it is unset a random key is generated at start-up and logged once, so
there is never an unauthenticated write endpoint.
"""
from __future__ import annotations

import asyncio
import json
import math
import logging
import os
import secrets
import threading
import time
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from pathlib import Path

from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from qs.api.live import LiveRun
from qs.config import OperatorConfig, PipelineConfig
from qs.core.pipeline import Pipeline
from qs.eval.arena import SimulatedOperator
from qs.schema import Decision

log = logging.getLogger("qs.api")
MAX_BATCH = 500
MAX_BODY = 1_000_000                     # bytes per request
MAX_AHEAD_MS = 60_000                    # events further ahead of the stream clock are rejected
MAX_FIRST_TS_MS = 7 * 86_400_000         # absolute cap for the first event of a stream
UI_DIR = Path(__file__).resolve().parent.parent / "ui" / "static"


def detector_mode() -> Optional[str]:
    """QS_DETECTOR_MODE: R (rules only), RM (rules + Isolation Forest, default). R raised fewer false alerts on the
    confirmation set (docs/EVAL_D.md) but the frozen evaluation results were produced with RM."""
    m = os.environ.get("QS_DETECTOR_MODE")
    if m and m not in ("R", "RM"):
        raise ValueError("QS_DETECTOR_MODE must be R or RM")
    return m or None


class State:
    def __init__(self, models_dir: str, key: Optional[str], store=None) -> None:
        self.models_dir = models_dir
        self.key = key or os.environ.get("QS_API_KEY") or secrets.token_urlsafe(16)
        if not (key or os.environ.get("QS_API_KEY")):
            log.warning("QS_API_KEY not set; generated key: %s", self.key)
        self.lock = threading.RLock()
        self.pipe = Pipeline(PipelineConfig(), store=store, models_dir=models_dir, detector_mode=detector_mode())
        self.events: list = []                 # SSE journal of dicts
        self.seq = 0                           # monotonic SSE id; never reset, so reconnecting clients stay consistent
        self.error: Optional[str] = None
        self.run: Optional[LiveRun] = None
        self.thread: Optional[threading.Thread] = None
        self.stop = threading.Event()
        self.paused = False
        self.scenario = None
        self.ingest_ts: list = []
        self.applied_reports: list = []

    def emit(self, kind: str, data: dict) -> None:
        self.events.append({"id": self.seq, "kind": kind, "data": data})
        self.seq += 1
        if len(self.events) > 5000:
            del self.events[:1000]


def num(v):
    """JSON-safe number: NaN and infinity become null (Starlette refuses to serialise them)."""
    return None if v is None or (isinstance(v, float) and not math.isfinite(v)) else v


async def body_json(request: Request):
    """Parsed JSON body, or None if it is missing, too large or not valid JSON."""
    raw = await request.body()
    if len(raw) > MAX_BODY:
        return None
    try:
        return json.loads(raw or b"null")
    except ValueError:
        return None


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
        for ap in r.rollbacks:
            st.emit("rollback_proposal", {"key": f"{ap.rec_id}:{ap.action.id.value}", "rec_id": ap.rec_id,
                                          "action": ap.action.id.value})
        st.emit("snapshot", {"tick_ts": r.tick_ts, "state": r.state.value, "degraded": r.degraded,
                             "p_stuck": {"/".join(k): v for k, v in r.p_stuck.items()}})

    @app.post("/v1/events", status_code=202, dependencies=[Depends(need_key)])
    async def post_events(request: Request):
        body = await body_json(request)
        if body is None:
            return err(400, "bad_json", "body is missing, too large or not valid JSON")
        if not isinstance(body, list):
            return err(400, "bad_body", "expected a JSON list of events")
        if len(body) > MAX_BATCH:
            return err(413, "batch_too_large", f"at most {MAX_BATCH} events per request")

        def work():
            with st.lock:
                cap = (st.pipe.ing.max_ts + MAX_AHEAD_MS) if st.pipe.ing.max_ts > 0 else MAX_FIRST_TS_MS
                for e in body:
                    ts = e.get("event_timestamp") if isinstance(e, dict) else None
                    if isinstance(ts, (int, float)) and ts > cap:
                        return err(422, "timestamp_too_far_ahead",
                                   f"event_timestamp {ts} is more than {MAX_AHEAD_MS} ms ahead of the stream clock")
                res = st.pipe.ingest(body)
                if st.run is None:                   # external simulator: ticks follow event time
                    for r in st.pipe.advance():
                        after_tick(r)
                return res.model_dump()
        return await run_in_threadpool(work)

    @app.get("/v1/state")
    def get_state():
        with st.lock:
            last = st.pipe.last
            segs = {}
            if last:
                for r in last.rows:
                    segs["/".join(r.segment)] = {"eligible": r.eligible, "flags": r.flags,
                                                 "f": {k: (None if v != v else v) for k, v in r.f.items()},
                                                 "score": num(last.scores.get(r.segment)), "p_stuck": num(last.p_stuck.get(r.segment))}
            return {"scenario": st.scenario, "paused": st.paused, "error": st.error,
                    "sim_t_ms": st.run.t if st.run else None, "events": st.run.events if st.run else 0,
                    "tick_ts": last.tick_ts if last else None, "state": last.state.value if last else "MONITORING",
                    "degraded": last.degraded if last else [], "segments": segs,
                    "verdict": last.localization.model_dump(mode="json") if last and last.localization else None}

    @app.get("/v1/incidents")
    def incidents(state: Optional[str] = None, limit: int = Query(100, ge=1, le=1000)):
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
        body = await body_json(request)
        try:
            dec = Decision.model_validate(body)
        except Exception as exc:
            return err(422, "bad_decision", str(exc)[:200])
        dec = dec.model_copy(update={"decided_ts": None})        # the decision time is the server's, never the client's

        def work():
            with st.lock:
                if rec_id not in st.pipe.actions.recs:
                    return err(404, "unknown_recommendation", rec_id)
                try:
                    acts = st.pipe.decide(rec_id, dec)
                except ValueError as exc:
                    return err(409, "decision_rejected", str(exc))
                st.emit("decision", {"rec_id": rec_id, "operator": dec.operator, "decision": dec.decision.value})
                return {"actions": [json.loads(a.model_dump_json()) for a in acts]}
        return await run_in_threadpool(work)

    @app.get("/v1/rollbacks")
    def rollbacks():
        """Rollback proposals not yet approved (SI-8: a rollback needs an operator like any other action)."""
        with st.lock:
            return [{"key": k, "rec_id": ap.rec_id, "action": ap.action.id.value, "scope": ap.action.scope}
                    for k, ap in st.pipe.actions.rollback_proposals.items() if not ap.rolled_back]

    @app.post("/v1/rollbacks/{key}/approve", dependencies=[Depends(need_key)])
    async def approve_rollback(key: str, request: Request):
        body = await body_json(request)
        op = body.get("operator") if isinstance(body, dict) else None
        if not isinstance(op, str) or not op.strip():
            return err(422, "bad_operator", "a rollback needs an operator name")

        def work():
            with st.lock:
                ap = st.pipe.actions.rollback_proposals.get(key)
                if ap is None:
                    return err(404, "unknown_rollback", key)
                try:
                    st.pipe.approve_rollback(ap, op.strip())
                except ValueError as exc:
                    return err(409, "rollback_rejected", str(exc))
                st.emit("rollback", {"key": key, "operator": op.strip()})
                return {"rolled_back": key}
        return await run_in_threadpool(work)

    @app.get("/v1/actions/pending", dependencies=[Depends(need_key)])
    def pending(since: int = 0):
        with st.lock:
            if st.run is not None:          # the live runner owns the action queue; draining it here would lose actions
                return []
            return [json.loads(a.model_dump_json()) for a in st.pipe.pending_actions()]

    @app.post("/v1/actions/{action_id}/applied", dependencies=[Depends(need_key)])
    async def applied(action_id: str, request: Request):
        body = await body_json(request)
        if not isinstance(body, dict):
            return err(400, "bad_body", "expected a JSON object")
        rep_ = {**{k: v for k, v in body.items() if k != "action"}, "action": action_id}
        with st.lock:
            st.applied_reports.append(rep_)
            del st.applied_reports[:-1000]
            st.emit("applied", rep_)
        return {"ok": True}

    @app.post("/v1/sim/scenario", status_code=202, dependencies=[Depends(need_key)])
    async def scenario(request: Request):
        body = await body_json(request)
        if not isinstance(body, dict):
            return err(400, "bad_body", "expected a JSON object")
        try:
            sc, seed, speed = body.get("scenario", "S4"), int(body.get("seed", 1)), float(body.get("speed", 20.0))
            delay_ms = int(body.get("operator_delay_ms", 20000))
        except (TypeError, ValueError):
            return err(422, "bad_parameters", "seed, speed and operator_delay_ms must be numbers")
        if sc not in {"S0", "S1", "S2", "S3", "S4", "S5", "S6", "S7"}:
            return err(422, "bad_scenario", str(sc))
        if not math.isfinite(speed) or not 0.1 <= speed <= 1000 or not 0 <= delay_ms <= 600_000:
            return err(422, "bad_parameters", "speed must be 0.1 to 1000 and operator_delay_ms 0 to 600000")

        def stop_old() -> None:
            with st.lock:
                if st.thread and st.thread.is_alive():
                    st.stop.set()
            if st.thread:
                st.thread.join(timeout=5)
        await run_in_threadpool(stop_old)
        st.stop = threading.Event()
        with st.lock:
            st.error = None
            st.pipe = Pipeline(PipelineConfig(), store=store, models_dir=models_dir, detector_mode=detector_mode())
            op = SimulatedOperator(OperatorConfig(approval_delay_ms=delay_ms)) \
                if body.get("auto_operator", False) else None
            st.run = LiveRun(sc, seed, body.get("arm", "U3"), pipeline=st.pipe, operator=op, on_tick=after_tick)
            run = st.run
            st.events.clear()
            st.paused = False
            st.scenario = {"scenario": sc, "seed": seed, "speed": speed}
        stop = st.stop

        def loop() -> None:
            try:
                while not run.done() and not stop.is_set():
                    t0 = time.monotonic()
                    if st.paused:
                        time.sleep(0.1)
                        continue
                    with st.lock:
                        run.step(1000)
                    time.sleep(max(0.0, 1.0 / max(speed, 0.1) - (time.monotonic() - t0)))
            except Exception as exc:                 # a dead runner must be visible, not a silent stall
                log.exception("scenario runner failed")
                with st.lock:
                    st.error = f"{type(exc).__name__}: {exc}"[:200]
                    st.emit("error", {"detail": st.error})
        st.thread = threading.Thread(target=loop, daemon=True)
        st.thread.start()
        return {"scenario": sc, "seed": seed, "speed": speed}

    @app.get("/v1/stream")
    async def stream(request: Request, last_id: int = -1):
        async def gen():
            cur = last_id
            hdr = request.headers.get("last-event-id")
            if hdr is not None and hdr.lstrip("-").isdigit():
                cur = max(cur, int(hdr))

            def fresh():
                with st.lock:
                    return [e for e in st.events if e["id"] > cur]
            while not await request.is_disconnected():
                new = await run_in_threadpool(fresh)
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

    @app.post("/v1/sim/control", dependencies=[Depends(need_key)])
    async def control(request: Request):
        body = await body_json(request)
        act = body.get("action") if isinstance(body, dict) else None
        if act not in {"pause", "resume", "stop"}:
            return err(422, "bad_action", str(act))
        if act == "stop":
            st.stop.set()
        else:
            st.paused = act == "pause"
        return {"paused": st.paused}

    @app.get("/v1/sim/truth")
    def truth():
        """Injected ground truth of the running scenario. For the demo console only; qs/core never reads it."""
        with st.lock:
            return list(st.run.truth) if st.run else []

    @app.get("/v1/decisions")
    def decisions():
        with st.lock:
            return [{"rec_id": d.rec_id, "decision": d.decision.value, "operator": d.operator, "ts": d.decided_ts,
                     "note": d.note, "actions": d.applied} for d in st.pipe.actions.log]

    @app.get("/v1/decisions.csv")
    def decisions_csv():
        with st.lock:
            rows = []
            for d in st.pipe.actions.log:
                rec = st.pipe.recommendations.get(d.rec_id)
                rows.append([d.decided_ts, d.rec_id, rec.incident_id if rec else "", d.operator, d.decision.value,
                             "|".join(d.applied), d.note or "",
                             json.dumps(rec.model_versions) if rec else "", rec.features_sha256 if rec else ""])
        head = "ts,rec_id,incident_id,operator,decision,actions_approved,note,model_versions,features_sha256"
        body = "\n".join(",".join('"%s"' % str(c).replace('"', '""') for c in r) for r in rows)
        return PlainTextResponse(head + "\n" + body + "\n", media_type="text/csv")

    @app.get("/v1/arena")
    def arena():
        """Committed policy-arena runs (results/arena.json) for the dashboard's arena tab."""
        f = Path("results/arena.json")
        return json.loads(f.read_text())["runs"] if f.exists() else []

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(UI_DIR / "index.html")

    if UI_DIR.exists():
        app.mount("/static", StaticFiles(directory=UI_DIR), name="static")

    @app.on_event("shutdown")
    def _shutdown() -> None:
        st.stop.set()
        if st.thread:
            st.thread.join(timeout=5)
        with st.lock:
            if store is not None:
                for fn in ("flush", "close"):
                    getattr(store, fn, lambda: None)()

    @app.get("/v1/health")
    def health():
        with st.lock:
            last = st.pipe.last
        return {"status": "ok", "global_state": last.state.value if last else "MONITORING"}

    return app
