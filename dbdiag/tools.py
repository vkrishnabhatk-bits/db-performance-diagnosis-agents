"""Read-only data access over a simulated scenario.

Two layers:

1. **Helpers** (``ranked_queries``, ``unindexed_filter_columns``,
   ``logs_in_window``, ``classify_log``, ...): small pure computations shared by
   ``perception.py`` (classical agents) and the tool functions below, so every
   agent derives evidence the same way.
2. **Tool functions** (listed in ``TOOL_FUNCTIONS``): return JSON-serializable
   dicts. The LLM agent wraps exactly these as LangChain tools. Bad input
   returns ``{"error": ...}`` instead of raising, so the model can recover.

Nothing here executes SQL, changes the scenario, or exposes ``ground_truth``.
"""

from __future__ import annotations

import re
from typing import Any

from dbdiag.models import (
    LOG_SEVERITY,
    OPTIONAL_METRICS,
    LogEntry,
    QueryPlan,
    QueryStat,
    Scenario,
    Snapshot,
)

MAX_LOG_RESULTS = 20


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def query_impact_ms_per_min(query: QueryStat) -> float:
    """Total database time the query costs per minute."""
    return query.calls_per_min * query.mean_ms


def ranked_queries(scenario: Scenario) -> list[QueryStat]:
    """Queries ordered by total time per minute (highest impact first)."""
    return sorted(scenario.queries, key=lambda q: (-query_impact_ms_per_min(q), q.query_id))


def selectivity_ratio(plan: QueryPlan) -> float:
    """Rows examined per row returned (large = the query reads far more than it needs)."""
    return plan.rows_examined / max(plan.rows_returned, 1)


def estimate_error_ratio(plan: QueryPlan) -> float:
    """How far the planner's row estimate is from the actual rows (1.0 = exact)."""
    hi = max(plan.est_rows, plan.rows_returned)
    lo = max(min(plan.est_rows, plan.rows_returned), 1)
    return hi / lo


def indexed_columns(scenario: Scenario, table: str) -> set[str]:
    return {col for ix in scenario.indexes if ix.table == table for col in ix.columns}


def unindexed_filter_columns(scenario: Scenario, query: QueryStat) -> list[str]:
    """Filter columns of the query that appear in no index on its table."""
    if query.plan.table is None:
        return []
    covered = indexed_columns(scenario, query.plan.table)
    return [c for c in query.plan.filter_columns if c not in covered]


def table_info(scenario: Scenario, table: str):
    return next((t for t in scenario.tables if t.name == table), None)


def missing_metrics(snapshot: Snapshot) -> list[str]:
    return [name for name in OPTIONAL_METRICS if getattr(snapshot, name) is None]


def _seconds(clock: str) -> int:
    parts = [int(p) for p in clock.split(":")]
    if len(parts) == 2:
        parts.append(0)
    h, m, s = parts
    return h * 3600 + m * 60 + s


def logs_in_window(scenario: Scenario, index: int) -> list[LogEntry]:
    """Log entries that belong to snapshot ``index``.

    Window ``i`` covers ``[t_i, t_{i+1})``. Logs before the first snapshot
    belong to the first window, logs after the last snapshot to the last one.
    """
    snapshots = scenario.snapshots
    i = index % len(snapshots)
    start = _seconds(snapshots[i].t) if i > 0 else float("-inf")
    end = _seconds(snapshots[i + 1].t) if i + 1 < len(snapshots) else float("inf")
    return [e for e in scenario.logs if start <= _seconds(e.ts) < end]


LOG_CATEGORIES: dict[str, re.Pattern[str]] = {
    "slow_query": re.compile(r"duration: \d+(\.\d+)? ms|slow query", re.I),
    "pool_exhausted": re.compile(
        r"pool (is )?exhausted|connection is not available|waiting for (a )?connection"
        r"|unable to acquire (a )?connection",
        re.I,
    ),
    "connection_leak": re.compile(r"connection leak|leak detection", re.I),
    "idle_in_transaction": re.compile(r"idle.in.transaction", re.I),
    "lock_wait": re.compile(
        r"lock wait timeout|lock timeout|deadlock|still waiting for \w*Lock|lock not available",
        re.I,
    ),
    "cpu_pressure": re.compile(r"\bcpu\b|load average", re.I),
    "checkpoint_pressure": re.compile(r"checkpoints are occurring too frequently", re.I),
    "connection_limit": re.compile(
        r"too many clients|too many connections|remaining connection slots|connections \d+/\d+",
        re.I,
    ),
    "missing_telemetry": re.compile(r"not responding|metrics unavailable|exporter.*(down|unavailable)", re.I),
}


def classify_log(entry: LogEntry) -> list[str]:
    """Categories (keys of ``LOG_CATEGORIES``) a log message falls into."""
    return [name for name, pattern in LOG_CATEGORIES.items() if pattern.search(entry.message)]


def _select_windows(scenario: Scenario, window: str) -> list[int] | None:
    """Snapshot indices for ``window`` ('all', 'latest' or a time like '10:03')."""
    window = (window or "all").strip().lower()
    n = len(scenario.snapshots)
    if window == "all":
        return list(range(n))
    if window == "latest":
        return [n - 1]
    for i, snap in enumerate(scenario.snapshots):
        if snap.t == window:
            return [i]
    return None


def _round(value: float | None, digits: int = 3) -> float | None:
    return None if value is None else round(value, digits)


# --------------------------------------------------------------------------- #
# Tool functions (wrapped by the LLM agent)
# --------------------------------------------------------------------------- #
def get_database_metrics(scenario: Scenario, window: str = "all") -> dict[str, Any]:
    """Latency, throughput, host and connection metrics per one-minute window."""
    indices = _select_windows(scenario, window)
    if indices is None:
        return {
            "error": f"unknown window {window!r}",
            "available_windows": ["all", "latest", *[s.t for s in scenario.snapshots]],
        }

    snaps = [scenario.snapshots[i] for i in indices]
    rows = [
        {
            "t": s.t,
            "qps": s.qps,
            "p50_ms": s.p50_ms,
            "p95_ms": s.p95_ms,
            "p99_ms": s.p99_ms,
            "cpu_pct": s.cpu_pct,
            "mem_pct": s.mem_pct,
            "io_wait_pct": s.io_wait_pct,
            "cache_hit_ratio": s.cache_hit_ratio,
            "db_connections_active": s.db_connections.active,
            "db_connections_max": s.db_connections.max,
            "db_connection_utilization": _round(s.db_connections.utilization),
            "pool_utilization": _round(s.pool.utilization),
            "pool_waiting": s.pool.waiting,
            "lock_waits": s.lock_waits,
            "idle_in_transaction": s.idle_in_transaction,
        }
        for s in snaps
    ]
    cpu_values = [s.cpu_pct for s in snaps if s.cpu_pct is not None]
    first, last = snaps[0], snaps[-1]
    summary = {
        "windows": len(snaps),
        "from": first.t,
        "to": last.t,
        "qps_first": first.qps,
        "qps_last": last.qps,
        "qps_change_ratio": _round(last.qps / first.qps if first.qps else None, 2),
        "p95_first_ms": first.p95_ms,
        "p95_last_ms": last.p95_ms,
        "p95_max_ms": max(s.p95_ms for s in snaps),
        "windows_breaching_p95_slo": sum(s.p95_ms > scenario.slo.p95_latency_ms for s in snaps),
        "cpu_max_pct": max(cpu_values) if cpu_values else None,
        "db_connection_utilization_max": _round(max(s.db_connections.utilization for s in snaps)),
        "missing_metrics": [f"{m}@{s.t}" for s in snaps for m in missing_metrics(s)],
    }
    return {
        "scenario_id": scenario.id,
        "slo": scenario.slo.model_dump(),
        "summary": summary,
        "windows": rows,
    }


def get_connection_pool_status(scenario: Scenario, window: str = "all") -> dict[str, Any]:
    """Application connection-pool counters per window."""
    indices = _select_windows(scenario, window)
    if indices is None:
        return {
            "error": f"unknown window {window!r}",
            "available_windows": ["all", "latest", *[s.t for s in scenario.snapshots]],
        }

    snaps = [scenario.snapshots[i] for i in indices]
    rows = [
        {
            "t": s.t,
            "size": s.pool.size,
            "active": s.pool.active,
            "idle": s.pool.idle,
            "waiting": s.pool.waiting,
            "avg_wait_ms": s.pool.avg_wait_ms,
            "timeouts": s.pool.timeouts,
            "utilization": _round(s.pool.utilization),
            "idle_in_transaction": s.idle_in_transaction,
        }
        for s in snaps
    ]
    summary = {
        "windows": len(snaps),
        "pool_size": snaps[-1].pool.size,
        "max_waiting": max(s.pool.waiting for s in snaps),
        "max_avg_wait_ms": max(s.pool.avg_wait_ms for s in snaps),
        "total_timeouts": sum(s.pool.timeouts for s in snaps),
        "windows_fully_used": sum(s.pool.active == s.pool.size for s in snaps),
        "max_idle_in_transaction": max(s.idle_in_transaction for s in snaps),
        "pool_wait_slo_ms": scenario.slo.pool_wait_ms,
    }
    return {"scenario_id": scenario.id, "summary": summary, "windows": rows}


def list_queries(scenario: Scenario) -> dict[str, Any]:
    """Tracked queries ordered by total time per minute."""
    return {
        "scenario_id": scenario.id,
        "queries": [
            {
                "query_id": q.query_id,
                "sql": q.sql,
                "calls_per_min": q.calls_per_min,
                "mean_ms": q.mean_ms,
                "total_ms_per_min": round(query_impact_ms_per_min(q), 1),
                "plan_node": q.plan.node,
                "table": q.plan.table,
            }
            for q in ranked_queries(scenario)
        ],
    }


def inspect_query_plan(scenario: Scenario, query_id: str = "slowest") -> dict[str, Any]:
    """Execution plan for one query. ``"slowest"`` = highest total time per minute."""
    if not scenario.queries:
        return {"error": "no queries recorded in this scenario"}

    if (query_id or "slowest").strip().lower() == "slowest":
        query = ranked_queries(scenario)[0]
    else:
        query = next((q for q in scenario.queries if q.query_id == query_id.strip()), None)
        if query is None:
            return {
                "error": f"unknown query_id {query_id!r}",
                "available_query_ids": [q.query_id for q in ranked_queries(scenario)],
            }

    plan = query.plan
    table = table_info(scenario, plan.table) if plan.table else None
    return {
        "scenario_id": scenario.id,
        "query_id": query.query_id,
        "sql": query.sql,
        "calls_per_min": query.calls_per_min,
        "mean_ms": query.mean_ms,
        "total_ms_per_min": round(query_impact_ms_per_min(query), 1),
        "plan": plan.model_dump(mode="json"),
        "derived": {
            "is_full_scan": plan.is_full_scan,
            "rows_examined_per_row_returned": round(selectivity_ratio(plan), 1),
            "estimate_error_ratio": round(estimate_error_ratio(plan), 1),
            "unindexed_filter_columns": unindexed_filter_columns(scenario, query),
            "table_rows": table.rows if table else None,
            "table_last_analyzed": table.last_analyzed if table else None,
        },
    }


def search_database_logs(
    scenario: Scenario,
    pattern: str = "",
    level: str = "ANY",
    limit: int = MAX_LOG_RESULTS,
) -> dict[str, Any]:
    """Search logs by case-insensitive regex (falls back to plain text) and minimum level.

    ``level`` is a minimum severity: ``"WARNING"`` returns WARNING, ERROR and FATAL.
    """
    level = (level or "ANY").strip().upper()
    if level != "ANY" and level not in LOG_SEVERITY:
        return {"error": f"unknown level {level!r}", "available_levels": ["ANY", *LOG_SEVERITY]}

    try:
        regex = re.compile(pattern or "", re.I)
    except re.error:
        regex = re.compile(re.escape(pattern), re.I)

    min_severity = LOG_SEVERITY.get(level, -1)
    matches = [
        e for e in scenario.logs if LOG_SEVERITY[e.level] >= min_severity and regex.search(e.message)
    ]
    limit = max(1, min(int(limit), MAX_LOG_RESULTS))

    category_counts: dict[str, int] = {}
    for entry in matches:
        for category in classify_log(entry):
            category_counts[category] = category_counts.get(category, 0) + 1

    return {
        "scenario_id": scenario.id,
        "pattern": pattern,
        "level": level,
        "total_matches": len(matches),
        "returned": min(len(matches), limit),
        "category_counts": category_counts,
        "entries": [
            {**e.model_dump(mode="json"), "categories": classify_log(e)} for e in matches[:limit]
        ],
    }


def get_index_metadata(scenario: Scenario, table: str = "all") -> dict[str, Any]:
    """Indexes and table statistics for one table, or all tables."""
    table = (table or "all").strip()
    names = [t.name for t in scenario.tables]
    if table.lower() == "all":
        selected = names
    elif table in names:
        selected = [table]
    else:
        return {"error": f"unknown table {table!r}", "available_tables": names}

    tables = []
    for name in selected:
        info = table_info(scenario, name)
        tables.append(
            {
                "table": name,
                "rows": info.rows,
                "last_analyzed": info.last_analyzed,
                "indexes": [
                    ix.model_dump(mode="json", exclude={"table"})
                    for ix in scenario.indexes
                    if ix.table == name
                ],
            }
        )
    return {"scenario_id": scenario.id, "tables": tables}


TOOL_FUNCTIONS = (
    get_database_metrics,
    get_connection_pool_status,
    list_queries,
    inspect_query_plan,
    search_database_logs,
    get_index_metadata,
)
