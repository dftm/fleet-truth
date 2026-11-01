"""CheckResult and status helpers shared by all checks."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .. import secrets

STATUSES = ("ok", "info", "warn", "crit", "unknown")
# severity order for "worst": unknown sits between info and warn so it is never shown green
RANK = {"ok": 0, "info": 1, "unknown": 2, "warn": 3, "crit": 4}
MAX_DETAIL = 2000


def _clean(v: Any) -> Any:
    if isinstance(v, str):
        return secrets.scrub(v)
    if isinstance(v, dict):
        return {str(k): _clean(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_clean(x) for x in v]
    return v


@dataclass(frozen=True)
class CheckResult:
    status: str
    verdict: str
    detail: str = ""
    data: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise ValueError(f"bad status {self.status!r}")
        object.__setattr__(self, "detail", secrets.scrub(self.detail)[:MAX_DETAIL])
        object.__setattr__(self, "data", _clean(self.data))


def worst(statuses) -> str:
    out = "ok"
    for s in statuses:
        if RANK[s] > RANK[out]:
            out = s
    return out
