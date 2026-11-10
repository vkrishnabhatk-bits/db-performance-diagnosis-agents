"""Data models shared by every agent.

Scenario models are frozen (immutable) so that one loaded scenario object can be
handed to all six agents without any agent being able to change what the others
see. Collections are tuples for the same reason.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

LogLevel = Literal["DEBUG", "INFO", "LOG", "WARNING", "ERROR", "FATAL"]
LOG_SEVERITY: dict[str, int] = {
    "DEBUG": 0,
    "INFO": 1,
    "LOG": 2,
    "WARNING": 3,
    "ERROR": 4,
    "FATAL": 5,
}

_HHMM = r"^\d{2}:\d{2}$"
_HHMMSS = r"^\d{2}:\d{2}:\d{2}$"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Bottleneck(StrEnum):
    """Diagnoses an agent can reach."""

    MISSING_INDEX_FULL_SCAN = "MISSING_INDEX_FULL_SCAN"
    CONNECTION_POOL_SATURATION = "CONNECTION_POOL_SATURATION"
    RESOURCE_SATURATION_PEAK_LOAD = "RESOURCE_SATURATION_PEAK_LOAD"
    LOCK_CONTENTION = "LOCK_CONTENTION"
    STALE_STATISTICS = "STALE_STATISTICS"
    HEALTHY = "HEALTHY"
    AMBIGUOUS = "AMBIGUOUS"  # evidence conflicts or is incomplete


# --------------------------------------------------------------------------- #
# Scenario building blocks
# --------------------------------------------------------------------------- #
class SLO(_Frozen):
    p95_latency_ms: float = Field(gt=0)
    pool_wait_ms: float = Field(gt=0)


class PoolStatus(_Frozen):
    """Application-side connection pool in one time window."""

    size: int = Field(gt=0)
    active: int = Field(ge=0)
    idle: int = Field(ge=0)
    waiting: int = Field(ge=0)
    avg_wait_ms: float = Field(ge=0)
    timeouts: int = Field(ge=0, description="Requests that gave up waiting in this window")

    @model_validator(mode="after")
    def _check_counts(self) -> PoolStatus:
        if self.active + self.idle > self.size:
            raise ValueError(f"pool active+idle ({self.active}+{self.idle}) exceeds size {self.size}")
        return self

    @property
    def utilization(self) -> float:
        return self.active / self.size


class DBConnections(_Frozen):
    """Server-side connections across all clients."""

    active: int = Field(ge=0)
    max: int = Field(gt=0)

    @property
    def utilization(self) -> float:
        return self.active / self.max


class Snapshot(_Frozen):
    """Metrics for one time window (one minute).

    Optional metrics are ``None`` when telemetry is missing for that window.
    """

    t: str = Field(pattern=_HHMM)
    qps: float = Field(ge=0)
    p50_ms: float = Field(ge=0)
    p95_ms: float = Field(ge=0)
    p99_ms: float = Field(ge=0)
    cpu_pct: float | None = Field(default=None, ge=0, le=100)
    mem_pct: float | None = Field(default=None, ge=0, le=100)
    io_wait_pct: float | None = Field(default=None, ge=0, le=100)
    cache_hit_ratio: float | None = Field(default=None, ge=0, le=1)
    pool: PoolStatus
    db_connections: DBConnections
    lock_waits: int = Field(default=0, ge=0)
    idle_in_transaction: int = Field(default=0, ge=0)


OPTIONAL_METRICS: tuple[str, ...] = ("cpu_pct", "mem_pct", "io_wait_pct", "cache_hit_ratio")


class QueryPlan(_Frozen):
    node: str = Field(description='Top plan node, e.g. "Seq Scan", "Index Scan", "Index Only Scan"')
    table: str | None = None
    filter_columns: tuple[str, ...] = ()
    rows_examined: int = Field(ge=0)
    rows_returned: int = Field(ge=0, description="Actual rows returned (EXPLAIN ANALYZE)")
    est_rows: int = Field(ge=0, description="Planner's estimated rows")
    cost: float = Field(ge=0)
    index_used: str | None = None

    @property
    def is_full_scan(self) -> bool:
        return self.node.lower() == "seq scan"


class QueryStat(_Frozen):
    query_id: str
    sql: str
    calls_per_min: float = Field(ge=0)
    mean_ms: float = Field(ge=0)
    plan: QueryPlan


class IndexInfo(_Frozen):
    table: str
    name: str
    columns: tuple[str, ...] = Field(min_length=1)
    unique: bool = False
    size_mb: float = Field(ge=0)
    scans: int = Field(ge=0)


class TableInfo(_Frozen):
    name: str
    rows: int = Field(ge=0)
    last_analyzed: str | None = None  # ISO date


class LogEntry(_Frozen):
    ts: str = Field(pattern=_HHMMSS)
    level: LogLevel
    source: str
    message: str


class GroundTruth(_Frozen):
    """Expected answer. Read only by tests and the learning agent's critic."""

    bottleneck: Bottleneck
    acceptable_actions: tuple[str, ...] = Field(min_length=1)
    notes: str = ""


# --------------------------------------------------------------------------- #
# Scenario
# --------------------------------------------------------------------------- #
class Scenario(_Frozen):
    id: str
    title: str
    category: Literal["normal", "critical", "edge"]
    description: str
    slo: SLO
    snapshots: tuple[Snapshot, ...] = Field(min_length=1)
    queries: tuple[QueryStat, ...] = ()
    indexes: tuple[IndexInfo, ...] = ()
    tables: tuple[TableInfo, ...] = ()
    logs: tuple[LogEntry, ...] = ()
    ground_truth: GroundTruth | None = None

    @model_validator(mode="after")
    def _check_consistency(self) -> Scenario:
        times = [s.t for s in self.snapshots]
        if times != sorted(times) or len(set(times)) != len(times):
            raise ValueError(f"snapshot times must be strictly increasing: {times}")

        ids = [q.query_id for q in self.queries]
        if len(set(ids)) != len(ids):
            raise ValueError(f"duplicate query_id in {ids}")

        table_names = {t.name for t in self.tables}
        for q in self.queries:
            if q.plan.table is not None and q.plan.table not in table_names:
                raise ValueError(f"{q.query_id} plan references unknown table {q.plan.table!r}")
        for ix in self.indexes:
            if ix.table not in table_names:
                raise ValueError(f"index {ix.name} references unknown table {ix.table!r}")
        return self

    @property
    def latest(self) -> Snapshot:
        return self.snapshots[-1]

    def agent_view(self) -> Scenario:
        """Copy without the ground truth: this is what agents receive."""
        return self.model_copy(update={"ground_truth": None})


# --------------------------------------------------------------------------- #
# Agent output
# --------------------------------------------------------------------------- #
class AgentDecision(BaseModel):
    """Common output of all six agents (the assignment's 'expected output')."""

    model_config = ConfigDict(extra="forbid")

    agent: str
    scenario_id: str
    scenario_fingerprint: str
    symptom_summary: str
    evidence: list[str]
    bottleneck: Bottleneck
    recommended_steps: list[str] = Field(min_length=1)
    verification_metric: str
    confidence: float = Field(ge=0, le=1)
    reasoning_trace: list[str] = Field(default_factory=list)
    extras: dict[str, Any] = Field(default_factory=dict)
