"""Status inquiry answers (blueprint 12.4). Uses ground truth, which the platform only sees via the answer."""
from __future__ import annotations

from qs.schema import Status
from qs.sim.entities import ENDED, Payment


def answer(p: Payment) -> Status:
    """CONFIRMED_SUCCESS if any attempt committed; UNKNOWN if any attempt is not definitively
    ended; CONFIRMED_FAILED only if every attempt ended without a commit."""
    if p.commits > 0:
        return Status.CONFIRMED_SUCCESS
    for a in p.attempts:
        if a.state != ENDED:
            return Status.UNKNOWN
    return Status.CONFIRMED_FAILED
