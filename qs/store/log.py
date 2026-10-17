"""Typed helpers that turn domain objects into store rows. Nothing here touches SQLite directly."""
from __future__ import annotations

import json

from qs.core.lifecycle import Incident
from qs.schema import Event


def segment_key(segment: tuple) -> str:
    return "/".join(str(p) for p in segment)


def log_event(store, e: Event) -> None:
    store.append("events", (e.event_id, e.event_type.value, e.payment_id, e.event_timestamp, e.received_timestamp,
                            e.model_dump_json()))


def log_incident(store, inc: Incident) -> None:
    store.append("incidents", (inc.incident_id, segment_key(inc.segment), inc.state.value, inc.opened_ts,
                               inc.closed_ts, inc.severity.value))


def log_features(store, tick_ts: int, rows: list, scores: dict | None = None, p_stuck: dict | None = None) -> None:
    scores, p_stuck = scores or {}, p_stuck or {}
    store.append_many("segment_features", [
        (tick_ts, segment_key(r.segment), json.dumps(r.f, default=float), scores.get(r.segment),
         p_stuck.get(r.segment)) for r in rows])
