"""Client profiles (blueprint 9.3). Pure functions of tape draws; no state."""
from __future__ import annotations

from dataclasses import dataclass

from qs.schema import ClientProfile


@dataclass(frozen=True)
class ProfileSpec:
    name: str
    cap: int
    uses_ik: bool
    verify: str          # "none" | "self_1s" | "self_backoff"
    max_checks: int


PROFILES = {
    ClientProfile.NAIVE.value: ProfileSpec("naive", 5, False, "none", 0),
    ClientProfile.DISCIPLINED.value: ProfileSpec("disciplined", 3, True, "self_backoff", 3),
    ClientProfile.NAIVE_VERIFY.value: ProfileSpec("naive_verify", 5, False, "self_1s", 60),
}


def retry_delay_ms(profile: str, k: int, u: float) -> int:
    """Delay before attempt k+1, given the draw u in [0, 1)."""
    if profile == ClientProfile.DISCIPLINED.value:
        return int(u * min(16000, 1000 * (2 ** k))) + 1   # full-jitter exponential
    return 500 + int(u * 1500)                            # uniform 500 to 2000 ms


def check_delay_ms(profile: str, n_checks: int, u: float) -> int:
    if profile == ClientProfile.NAIVE_VERIFY.value:
        return 1000
    return int(u * min(16000, 1000 * (2 ** n_checks))) + 1000


def pick_profile(mix: dict, u: float) -> str:
    acc = 0.0
    last = None
    for name, w in mix.items():
        acc += w
        last = name
        if u < acc:
            return name
    return last
