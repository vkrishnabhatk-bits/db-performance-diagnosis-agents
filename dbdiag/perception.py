"""Turn a scenario into percepts for the classical agents.

A ``Percept`` describes one time window: that window's metrics, the query
plans (static for the scenario), the logs written during that window, and a
set of named ``symptoms``. All features are computed with the helpers in
``tools.py``, which the LLM's tools also use, so every agent sees the same
evidence.

- ``perceive(scenario)``         latest window only (what a reflex agent sees)
- ``percept_sequence(scenario)`` every window in order (for agents with state)

Symptom names are a shared vocabulary for rules, signatures and explanations.
"""

from __future__ import annotations

from dataclasses import dataclass

from dbdiag import tools
from dbdiag.knowledge import THRESHOLDS, Thresholds
from dbdiag.models import LOG_SEVERITY, Scenario

# Symptoms that indicate a problem. A percept with none of them is "healthy".
PROBLEM_SYMPTOMS: frozenset[str] = frozenset(
    {
        "latency_slo_breach",
        "p99_spike",
        "full_scan_large_table",
        "unindexed_filter",
        "stale_statistics",
        "pool_saturated",
        "pool_wait_slo_breach",
        "pool_timeouts",
        "idle_in_transaction_high",
        "db_conn_high",
        "cpu_high",
        "io_wait_high",
        "cache_hit_low",
        "lock_waits",
        "missing_metrics",
        "error_logs",
        "connection_leak_logs",
    }
)
ALL_SYMPTOMS: frozenset[str] = PROBLEM_SYMPTOMS | {"healthy"}


@dataclass(frozen=True)
class PlanFeatures:
    query_id: str
    table: str | None
    node: str
    calls_per_min: float
    mean_ms: float
    total_ms_per_min: float
    rows_examined: int
    rows_returned: int
    est_rows: int
    selectivity_ratio: float
    estimate_error_ratio: float
    is_full_scan: bool
    is_large_table_scan: bool  # frequent full scan over a big table that returns few rows
    has_stale_estimate: bool
    unindexed_filter_columns: tuple[str, ...]
    table_rows: int | None
    table_last_analyzed: str | None


@dataclass(frozen=True)
class LogFeatures:
    total: int
    warnings: int  # WARNING
    errors: int  # ERROR or FATAL
    category_counts: tuple[tuple[str, int], ...]  # sorted (category, count)
    samples: tuple[str, ...]  # up to 3 most severe messages

    def count(self, category: str) -> int:
        return dict(self.category_counts).get(category, 0)


@dataclass(frozen=True)
class Percept:
    scenario_id: str
    window_index: int
    n_windows: int
    t: str
    slo_p95_ms: float
    slo_pool_wait_ms: float

    qps: float
    p50_ms: float
    p95_ms: float
    p99_ms: float
    cpu_pct: float | None
    mem_pct: float | None
    io_wait_pct: float | None
    cache_hit_ratio: float | None
    missing_metrics: tuple[str, ...]

    pool_size: int
    pool_active: int
    pool_waiting: int
    pool_utilization: float
    pool_avg_wait_ms: float
    pool_timeouts: int
    db_conn_active: int
    db_conn_max: int
    db_conn_utilization: float
    lock_waits: int
    idle_in_transaction: int

    plans: tuple[PlanFeatures, ...]  # highest total time first
    logs: LogFeatures  # this window's logs only
    symptoms: frozenset[str]

    @property
    def top_plan(self) -> PlanFeatures | None:
        return self.plans[0] if self.plans else None

    def has(self, symptom: str) -> bool:
        if symptom not in ALL_SYMPTOMS:
            raise KeyError(f"unknown symptom {symptom!r}")
        return symptom in self.symptoms


# --------------------------------------------------------------------------- #
def _plan_features(scenario: Scenario, th: Thresholds) -> tuple[PlanFeatures, ...]:
    features = []
    for q in tools.ranked_queries(scenario):
        plan = q.plan
        table = tools.table_info(scenario, plan.table) if plan.table else None
        ratio = tools.selectivity_ratio(plan)
        est_err = tools.estimate_error_ratio(plan)
        features.append(
            PlanFeatures(
                query_id=q.query_id,
                table=plan.table,
                node=plan.node,
                calls_per_min=q.calls_per_min,
                mean_ms=q.mean_ms,
                total_ms_per_min=tools.query_impact_ms_per_min(q),
                rows_examined=plan.rows_examined,
                rows_returned=plan.rows_returned,
                est_rows=plan.est_rows,
                selectivity_ratio=ratio,
                estimate_error_ratio=est_err,
                is_full_scan=plan.is_full_scan,
                is_large_table_scan=(
                    plan.is_full_scan
                    and plan.rows_examined >= th.full_scan_min_rows
                    and ratio >= th.full_scan_selectivity_ratio
                    and q.calls_per_min >= th.frequent_query_calls_per_min
                ),
                has_stale_estimate=est_err >= th.stale_stats_ratio,
                unindexed_filter_columns=tuple(tools.unindexed_filter_columns(scenario, q)),
                table_rows=table.rows if table else None,
                table_last_analyzed=table.last_analyzed if table else None,
            )
        )
    return tuple(features)


def _log_features(scenario: Scenario, index: int) -> LogFeatures:
    entries = tools.logs_in_window(scenario, index)
    counts: dict[str, int] = {}
    for entry in entries:
        for category in tools.classify_log(entry):
            counts[category] = counts.get(category, 0) + 1
    by_severity = sorted(entries, key=lambda e: (-LOG_SEVERITY[e.level], e.ts))
    return LogFeatures(
        total=len(entries),
        warnings=sum(e.level == "WARNING" for e in entries),
        errors=sum(e.level in ("ERROR", "FATAL") for e in entries),
        category_counts=tuple(sorted(counts.items())),
        samples=tuple(f"[{e.ts} {e.level} {e.source}] {e.message}" for e in by_severity[:3]),
    )


def _symptoms(p: Percept, th: Thresholds) -> frozenset[str]:
    s: set[str] = set()
    if p.p95_ms > p.slo_p95_ms:
        s.add("latency_slo_breach")
    if p.p95_ms > 0 and p.p99_ms / p.p95_ms >= th.p99_spike_ratio:
        s.add("p99_spike")

    if any(pl.is_large_table_scan for pl in p.plans):
        s.add("full_scan_large_table")
    if any(pl.is_large_table_scan and pl.unindexed_filter_columns for pl in p.plans):
        s.add("unindexed_filter")
    if any(pl.has_stale_estimate for pl in p.plans):
        s.add("stale_statistics")

    if p.pool_utilization >= th.pool_utilization_high and p.pool_waiting > 0:
        s.add("pool_saturated")
    if p.pool_avg_wait_ms > p.slo_pool_wait_ms:
        s.add("pool_wait_slo_breach")
    if p.pool_timeouts > 0:
        s.add("pool_timeouts")
    if p.idle_in_transaction >= th.idle_in_transaction_high:
        s.add("idle_in_transaction_high")
    if p.db_conn_utilization >= th.db_conn_utilization_high:
        s.add("db_conn_high")

    if p.cpu_pct is not None and p.cpu_pct >= th.cpu_high_pct:
        s.add("cpu_high")
    if p.io_wait_pct is not None and p.io_wait_pct >= th.io_wait_high_pct:
        s.add("io_wait_high")
    if p.cache_hit_ratio is not None and p.cache_hit_ratio < th.cache_hit_low:
        s.add("cache_hit_low")
    if p.lock_waits > 0 or p.logs.count("lock_wait") > 0:
        s.add("lock_waits")
    if p.missing_metrics:
        s.add("missing_metrics")

    if p.logs.errors > 0:
        s.add("error_logs")
    if p.logs.count("connection_leak") > 0:
        s.add("connection_leak_logs")

    if not s:
        s.add("healthy")
    return frozenset(s)


def _build(scenario: Scenario, index: int, plans: tuple[PlanFeatures, ...], th: Thresholds) -> Percept:
    n = len(scenario.snapshots)
    i = index % n
    snap = scenario.snapshots[i]
    percept = Percept(
        scenario_id=scenario.id,
        window_index=i,
        n_windows=n,
        t=snap.t,
        slo_p95_ms=scenario.slo.p95_latency_ms,
        slo_pool_wait_ms=scenario.slo.pool_wait_ms,
        qps=snap.qps,
        p50_ms=snap.p50_ms,
        p95_ms=snap.p95_ms,
        p99_ms=snap.p99_ms,
        cpu_pct=snap.cpu_pct,
        mem_pct=snap.mem_pct,
        io_wait_pct=snap.io_wait_pct,
        cache_hit_ratio=snap.cache_hit_ratio,
        missing_metrics=tuple(tools.missing_metrics(snap)),
        pool_size=snap.pool.size,
        pool_active=snap.pool.active,
        pool_waiting=snap.pool.waiting,
        pool_utilization=snap.pool.utilization,
        pool_avg_wait_ms=snap.pool.avg_wait_ms,
        pool_timeouts=snap.pool.timeouts,
        db_conn_active=snap.db_connections.active,
        db_conn_max=snap.db_connections.max,
        db_conn_utilization=snap.db_connections.utilization,
        lock_waits=snap.lock_waits,
        idle_in_transaction=snap.idle_in_transaction,
        plans=plans,
        logs=_log_features(scenario, i),
        symptoms=frozenset(),
    )
    # Percept is frozen; set the derived symptoms once.
    object.__setattr__(percept, "symptoms", _symptoms(percept, th))
    return percept


def perceive(scenario: Scenario, window: int = -1, thresholds: Thresholds = THRESHOLDS) -> Percept:
    """Percept for one window (default: the latest)."""
    return _build(scenario, window, _plan_features(scenario, thresholds), thresholds)


def percept_sequence(scenario: Scenario, thresholds: Thresholds = THRESHOLDS) -> list[Percept]:
    """Percepts for every window, oldest first."""
    plans = _plan_features(scenario, thresholds)
    return [_build(scenario, i, plans, thresholds) for i in range(len(scenario.snapshots))]
