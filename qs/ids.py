"""Deterministic identifier helpers (blueprint 8.1). Counters only, no randomness."""
from __future__ import annotations


def event_id(run_id: str, counter: int) -> str:
    return f"{run_id}-{counter:07d}"


def payment_id(n: int) -> str:
    return f"p-{n:06d}"


def attempt_id(pay_id: str, attempt_no: int) -> str:
    return f"{pay_id}-a{attempt_no}"


def idempotency_key(payment_n: int) -> str:
    return f"ik-{payment_n:06d}"


def parse_attempt_id(att_id: str) -> tuple[str, int]:
    pay, sep, n = att_id.rpartition("-a")
    if not sep or not pay or not n.isdigit():
        raise ValueError(f"malformed attempt_id {att_id!r}")
    return pay, int(n)


class IdGenerator:
    """Per-run counters for events, payments, recommendations and incidents."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self._ev = 0
        self._pay = 0
        self._rec = 0
        self._inc = 0

    def next_event_id(self) -> str:
        self._ev += 1
        return event_id(self.run_id, self._ev)

    def next_payment_n(self) -> int:
        self._pay += 1
        return self._pay

    def next_rec_id(self) -> str:
        self._rec += 1
        return f"rec-{self._rec:04d}"

    def next_incident_id(self) -> str:
        self._inc += 1
        return f"inc-{self._inc:04d}"
