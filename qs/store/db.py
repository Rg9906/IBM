"""SQLite store (WAL) with batched writes: up to batch_rows rows or batch_ms, whichever comes first (blueprint 8.6).

Callers on the hot path only append to an in-memory buffer; flushes happen when a batch is due.
"""
from __future__ import annotations

import os
import sqlite3
import time
from collections import defaultdict
from pathlib import Path
from typing import Callable, Optional

from qs.config import LifecycleConfig

SCHEMA = Path(__file__).with_name("schema.sql")

# table -> (column order, conflict clause). INSERT OR IGNORE keeps the unique event_id index as the dedupe backstop.
TABLES = {
    "events": (("event_id", "event_type", "payment_id", "event_ts", "received_ts", "payload_json"), "OR IGNORE"),
    "payments": (("payment_id", "state", "attempts", "status_checks", "updated_ts"), "OR REPLACE"),
    "segment_features": (("tick_ts", "segment_key", "features_json", "anomaly_score", "p_stuck"), "OR REPLACE"),
    "incidents": (("incident_id", "segment_key", "state", "opened_ts", "closed_ts", "severity"), "OR REPLACE"),
    "recommendations": (("rec_id", "incident_id", "action", "params_json", "reasons_json", "confidence", "status"),
                        "OR REPLACE"),
    "decisions": (("rec_id", "decision", "operator", "decided_ts", "note"), ""),
    "actions_applied": (("rec_id", "applied_ts", "params_json", "result"), ""),
    "model_registry": (("model_name", "version", "sha256", "trained_on_seed_set", "created_ts"), "OR REPLACE"),
    "runs": (("run_id", "scenario", "seed", "arm", "config_json"), "OR REPLACE"),
}


class Store:
    def __init__(self, path: str, cfg: Optional[LifecycleConfig] = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.cfg = cfg or LifecycleConfig()
        self._clock = clock
        new = path != ":memory:" and not os.path.exists(path)
        self.db = sqlite3.connect(path, check_same_thread=False)
        if path != ":memory:":
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=NORMAL")
            if new:
                try:
                    os.chmod(path, 0o600)          # owner-only (no-op semantics on Windows)
                except OSError:
                    pass
        self.db.executescript(SCHEMA.read_text())
        self._buf: dict = defaultdict(list)
        self._n = 0
        self._first_ts: Optional[float] = None
        self.flushes = 0

    def append(self, table: str, row: tuple) -> None:
        if table not in TABLES:
            raise KeyError(table)
        self._buf[table].append(row)
        self._n += 1
        if self._first_ts is None:
            self._first_ts = self._clock()
        self.flush_if_due()

    def append_many(self, table: str, rows: list) -> None:
        for r in rows:
            self._buf[table].append(r)
        self._n += len(rows)
        if rows and self._first_ts is None:
            self._first_ts = self._clock()
        self.flush_if_due()

    def flush_if_due(self) -> bool:
        if self._n and (self._n >= self.cfg.batch_rows
                        or (self._clock() - self._first_ts) * 1000 >= self.cfg.batch_ms):
            self.flush()
            return True
        return False

    def flush(self) -> None:
        if not self._n:
            return
        with self.db:
            for table, rows in self._buf.items():
                if not rows:
                    continue
                cols, conflict = TABLES[table]
                sql = f"INSERT {conflict} INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})"
                self.db.executemany(sql, rows)
        self._buf = defaultdict(list)
        self._n, self._first_ts = 0, None
        self.flushes += 1

    def query(self, sql: str, args: tuple = ()) -> list:
        self.flush()
        return self.db.execute(sql, args).fetchall()

    def close(self) -> None:
        self.flush()
        self.db.close()
