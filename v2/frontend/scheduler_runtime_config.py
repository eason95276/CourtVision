"""Initial, configurable local scheduler limits; not measured service guarantees."""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class RuntimeConfig:
    tick_seconds: float = 5
    planner_interval_seconds: float = 60
    max_concurrency: int = 3
    final_reserved: int = 2
    lease_seconds: int = 120
    heartbeat_seconds: float = 30
    final_min_budget_seconds: float = 180
    publish_margin_seconds: float = 15
    shutdown_grace_seconds: float = 5
    max_attempts: int = 3
    leader_lease_seconds: float = 30

    def __post_init__(self):
        for name in ("tick_seconds", "planner_interval_seconds", "heartbeat_seconds", "final_min_budget_seconds",
                     "publish_margin_seconds", "shutdown_grace_seconds", "leader_lease_seconds"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value < 86400:
                raise ValueError(f"invalid_scheduler_{name}")
        for name in ("max_concurrency", "final_reserved", "lease_seconds", "max_attempts"):
            if isinstance(getattr(self, name), bool) or not isinstance(getattr(self, name), int):
                raise ValueError(f"invalid_scheduler_{name}")
        if not 2 <= self.max_concurrency <= 8 or not 1 <= self.final_reserved < self.max_concurrency:
            raise ValueError("scheduler_requires_reserved_final_and_low_capacity")
        if self.lease_seconds < 3 or self.heartbeat_seconds >= self.lease_seconds / 2:
            raise ValueError("scheduler_heartbeat_must_precede_lease_expiry")
        if not 1 <= self.max_attempts <= 10 or self.leader_lease_seconds <= self.tick_seconds * 2:
            raise ValueError("invalid_scheduler_retry_or_leader_limits")

    @classmethod
    def from_env(cls, environ=None):
        env = os.environ if environ is None else environ
        result = {}
        integer = {"max_concurrency", "final_reserved", "lease_seconds", "max_attempts"}
        for name in cls.__dataclass_fields__:
            raw = env.get("COURTVISION_SCHEDULER_" + name.upper())
            if raw is not None and str(raw).strip():
                result[name] = (int if name in integer else float)(raw)
        return cls(**result)

    def public(self):
        return asdict(self)
