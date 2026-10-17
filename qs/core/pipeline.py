"""Pipeline stub (WP-01). Real implementation is wired later; the signatures follow blueprint 19.4."""
from __future__ import annotations

from typing import Optional

from qs.config import PipelineConfig
from qs.schema import Action, Decision, Event, IngestResult


class Pipeline:
    def __init__(self, cfg: PipelineConfig, store: Optional[object] = None) -> None:
        raise NotImplementedError("Pipeline is implemented by WP-04 to WP-10")

    def ingest(self, events: list[Event]) -> IngestResult:
        raise NotImplementedError

    def tick(self, now_ms: int) -> object:
        raise NotImplementedError

    def decide(self, rec_id: str, decision: Decision) -> list[Action]:
        raise NotImplementedError

    def pending_actions(self) -> list[Action]:
        raise NotImplementedError
