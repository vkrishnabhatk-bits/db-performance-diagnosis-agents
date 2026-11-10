"""Domain knowledge shared by all agents.

- ``THRESHOLDS``: generic limits used to turn raw metrics into symptoms. They
  are deliberately not tuned per scenario.
- ``ACTIONS``: catalogue of safe actions. Every action is a recommendation; none
  executes anything. Each has a modeled benefit per bottleneck, risk, cost and
  time-to-effect (used by the utility agent) and STRIPS-style preconditions and
  effects (used by the goal-based agent).
- ``GOAL_FACTS``: facts that must hold for a bottleneck to count as resolved.
- ``VERIFICATION_METRICS``: how to confirm that a fix worked.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal, Mapping

from dbdiag.models import Bottleneck

B = Bottleneck


# --------------------------------------------------------------------------- #
# Thresholds
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Thresholds:
    # Query plans
    full_scan_min_rows: int = 100_000  # a Seq Scan below this is cheap and normal
    full_scan_selectivity_ratio: float = 1_000  # rows examined per row returned
    frequent_query_calls_per_min: float = 100
    stale_stats_ratio: float = 100  # max(est, actual) / min(est, actual)

    # Latency
    p99_spike_ratio: float = 2.5  # p99 / p95

    # Connection pool and server connections
    pool_utilization_high: float = 0.95
    db_conn_utilization_high: float = 0.85
    idle_in_transaction_high: int = 10

    # Host resources
    cpu_high_pct: float = 85
    io_wait_high_pct: float = 20
    cache_hit_low: float = 0.90

    # Trends (for agents that look at the time series)
    qps_rise_ratio: float = 1.5  # last / first window
    trend_correlation_min: float = 0.8


THRESHOLDS = Thresholds()


# --------------------------------------------------------------------------- #
# Safe actions
# --------------------------------------------------------------------------- #
ActionKind = Literal["diagnostic", "optimization", "none"]


@dataclass(frozen=True)
class SafeAction:
    id: str
    kind: ActionKind
    title: str
    description: str
    benefit: Mapping[Bottleneck, float]  # 0..1, how much it helps if that bottleneck is real
    risk: float  # 0..1
    cost: float  # 0..1, effort
    time_to_effect: float  # 0..1, 0 = immediate
    preconditions: frozenset[str] = field(default_factory=frozenset)
    effects: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        object.__setattr__(self, "benefit", MappingProxyType(dict(self.benefit)))
        for name in ("risk", "cost", "time_to_effect"):
            value = getattr(self, name)
            if not 0 <= value <= 1:
                raise ValueError(f"{self.id}.{name}={value} must be within 0..1")
        for bottleneck, value in self.benefit.items():
            if not 0 <= value <= 1:
                raise ValueError(f"{self.id}.benefit[{bottleneck}]={value} must be within 0..1")

    def benefit_for(self, bottleneck: Bottleneck) -> float:
        return self.benefit.get(bottleneck, 0.0)


_ACTION_LIST: tuple[SafeAction, ...] = (
    # ---- query plan / index ------------------------------------------------
    SafeAction(
        id="explain_analyze_in_staging",
        kind="diagnostic",
        title="Confirm the plan with EXPLAIN ANALYZE in staging",
        description=(
            "Run EXPLAIN (ANALYZE, BUFFERS) for the slow query on a staging replica and confirm "
            "where the time goes (scan type, rows examined vs returned, buffer reads)."
        ),
        benefit={B.MISSING_INDEX_FULL_SCAN: 0.4, B.STALE_STATISTICS: 0.3, B.AMBIGUOUS: 0.3},
        risk=0.05,
        cost=0.1,
        time_to_effect=0.1,
        effects=frozenset({"plan_confirmed"}),
    ),
    SafeAction(
        id="review_index_proposal",
        kind="optimization",
        title="Propose an index for DBA review",
        description=(
            "Draft a CREATE INDEX CONCURRENTLY proposal on the filtered column(s) for DBA review, "
            "test it in staging first, and do not execute it in production from this tool."
        ),
        benefit={B.MISSING_INDEX_FULL_SCAN: 1.0},
        risk=0.3,
        cost=0.3,
        time_to_effect=0.4,
        preconditions=frozenset({"plan_confirmed"}),
        effects=frozenset({"plan_fixed"}),
    ),
    # ---- connection pool ---------------------------------------------------
    SafeAction(
        id="inspect_pool_usage",
        kind="diagnostic",
        title="Inspect pool usage and idle-in-transaction sessions",
        description=(
            "Check pool active/idle/waiting counts over time and list idle-in-transaction "
            "sessions in pg_stat_activity to find connections that are held but not used."
        ),
        benefit={B.CONNECTION_POOL_SATURATION: 0.4, B.RESOURCE_SATURATION_PEAK_LOAD: 0.2},
        risk=0.05,
        cost=0.1,
        time_to_effect=0.1,
        effects=frozenset({"pool_diagnosed"}),
    ),
    SafeAction(
        id="fix_connection_leak",
        kind="optimization",
        title="Fix connection leaks in application code",
        description=(
            "Use the pool's leak-detection stack traces to find code paths that do not return "
            "connections (missing close / context manager) and fix them in a reviewed change."
        ),
        benefit={B.CONNECTION_POOL_SATURATION: 0.9},
        risk=0.2,
        cost=0.5,
        time_to_effect=0.5,
        preconditions=frozenset({"pool_diagnosed"}),
        effects=frozenset({"pool_ok"}),
    ),
    SafeAction(
        id="tune_pool_and_timeouts",
        kind="optimization",
        title="Propose pool size and timeout changes",
        description=(
            "Propose for review: a pool size sized to DB capacity, a shorter connection-acquire "
            "timeout, and an idle_in_transaction_session_timeout, rolled out in staging first."
        ),
        benefit={B.CONNECTION_POOL_SATURATION: 0.7, B.RESOURCE_SATURATION_PEAK_LOAD: 0.2},
        risk=0.3,
        cost=0.2,
        time_to_effect=0.2,
        preconditions=frozenset({"pool_diagnosed"}),
        effects=frozenset({"pool_ok"}),
    ),
    # ---- peak load / resources --------------------------------------------
    SafeAction(
        id="profile_peak_load",
        kind="diagnostic",
        title="Profile the peak: correlate traffic, CPU and connections",
        description=(
            "Correlate QPS with p95 latency, CPU and connection counts per window, and list the "
            "top queries by total time from pg_stat_statements during the peak."
        ),
        benefit={B.RESOURCE_SATURATION_PEAK_LOAD: 0.4, B.CONNECTION_POOL_SATURATION: 0.1},
        risk=0.05,
        cost=0.15,
        time_to_effect=0.1,
        effects=frozenset({"load_diagnosed"}),
    ),
    SafeAction(
        id="rate_limit_peak_traffic",
        kind="optimization",
        title="Shed or rate-limit non-critical traffic at peak",
        description=(
            "Propose rate limits or load shedding for non-critical endpoints during peaks, "
            "and cap autoscaled app instances so total connections stay below the DB limit."
        ),
        benefit={B.RESOURCE_SATURATION_PEAK_LOAD: 0.7, B.CONNECTION_POOL_SATURATION: 0.2},
        risk=0.25,
        cost=0.2,
        time_to_effect=0.2,
        preconditions=frozenset({"load_diagnosed"}),
        effects=frozenset({"load_ok"}),
    ),
    SafeAction(
        id="scale_read_replicas_or_cache",
        kind="optimization",
        title="Move read traffic to replicas or a cache",
        description=(
            "Plan read replicas and/or an application cache for the hottest read queries so the "
            "primary's CPU is not the limit at peak (capacity change, reviewed and staged)."
        ),
        benefit={B.RESOURCE_SATURATION_PEAK_LOAD: 1.0},
        risk=0.35,
        cost=0.7,
        time_to_effect=0.6,
        preconditions=frozenset({"load_diagnosed"}),
        effects=frozenset({"load_ok"}),
    ),
    # ---- locks -------------------------------------------------------------
    SafeAction(
        id="check_locks",
        kind="diagnostic",
        title="Find blocking sessions",
        description=(
            "Query pg_locks joined with pg_stat_activity (read-only) to find blocking and "
            "blocked sessions, how long they wait, and which statements hold the locks."
        ),
        benefit={B.LOCK_CONTENTION: 0.5, B.AMBIGUOUS: 0.4},
        risk=0.05,
        cost=0.1,
        time_to_effect=0.1,
        effects=frozenset({"locks_diagnosed"}),
    ),
    SafeAction(
        id="shorten_long_transactions",
        kind="optimization",
        title="Shorten lock-holding transactions",
        description=(
            "Propose code changes so transactions that take row locks (SELECT ... FOR UPDATE) "
            "commit quickly, e.g. SKIP LOCKED for queue-style work, reviewed before rollout."
        ),
        benefit={B.LOCK_CONTENTION: 0.9},
        risk=0.25,
        cost=0.4,
        time_to_effect=0.4,
        preconditions=frozenset({"locks_diagnosed"}),
        effects=frozenset({"locks_ok"}),
    ),
    # ---- statistics --------------------------------------------------------
    SafeAction(
        id="analyze_stats_in_staging",
        kind="diagnostic",
        title="Refresh statistics in staging and compare plans",
        description=(
            "On a staging copy, refresh table statistics with ANALYZE and compare the planner's "
            "estimated rows and the chosen plan before and after."
        ),
        benefit={B.STALE_STATISTICS: 0.6, B.AMBIGUOUS: 0.3},
        risk=0.05,
        cost=0.15,
        time_to_effect=0.1,
        effects=frozenset({"stats_checked"}),
    ),
    SafeAction(
        id="schedule_stats_refresh",
        kind="optimization",
        title="Propose a statistics refresh schedule",
        description=(
            "Propose an autovacuum/analyze schedule (or per-table settings) for the affected "
            "table for DBA review, so planner estimates stay current."
        ),
        benefit={B.STALE_STATISTICS: 0.9},
        risk=0.15,
        cost=0.2,
        time_to_effect=0.3,
        preconditions=frozenset({"stats_checked"}),
        effects=frozenset({"stats_ok"}),
    ),
    # ---- telemetry / monitoring -------------------------------------------
    SafeAction(
        id="collect_missing_telemetry",
        kind="diagnostic",
        title="Restore missing telemetry",
        description=(
            "Restore the missing host metrics (e.g. the node exporter) and collect a few more "
            "windows before committing to a diagnosis."
        ),
        benefit={B.AMBIGUOUS: 0.6},
        risk=0.0,
        cost=0.1,
        time_to_effect=0.2,
        effects=frozenset({"telemetry_complete"}),
    ),
    SafeAction(
        id="continue_monitoring",
        kind="none",
        title="No action: keep monitoring",
        description="All metrics are within the SLO. Keep the dashboards and alerts as they are.",
        benefit={B.HEALTHY: 1.0},
        risk=0.0,
        cost=0.0,
        time_to_effect=0.0,
        effects=frozenset({"monitoring"}),
    ),
)

ACTIONS: Mapping[str, SafeAction] = MappingProxyType({a.id: a for a in _ACTION_LIST})


def get_action(action_id: str) -> SafeAction:
    try:
        return ACTIONS[action_id]
    except KeyError:
        raise KeyError(f"unknown action {action_id!r}; known: {list(ACTIONS)}") from None


def actions_for(bottleneck: Bottleneck) -> list[SafeAction]:
    """Actions that help with ``bottleneck``, most beneficial first."""
    helpful = [a for a in ACTIONS.values() if a.benefit_for(bottleneck) > 0]
    return sorted(helpful, key=lambda a: (-a.benefit_for(bottleneck), a.risk, a.id))


# --------------------------------------------------------------------------- #
# Goals and verification
# --------------------------------------------------------------------------- #
GOAL_FACTS: Mapping[Bottleneck, frozenset[str]] = MappingProxyType(
    {
        B.MISSING_INDEX_FULL_SCAN: frozenset({"plan_fixed"}),
        B.CONNECTION_POOL_SATURATION: frozenset({"pool_ok"}),
        B.RESOURCE_SATURATION_PEAK_LOAD: frozenset({"load_ok"}),
        B.LOCK_CONTENTION: frozenset({"locks_ok"}),
        B.STALE_STATISTICS: frozenset({"stats_ok"}),
        B.HEALTHY: frozenset({"monitoring"}),
        # Not resolvable yet: the goal is to reduce uncertainty.
        B.AMBIGUOUS: frozenset({"telemetry_complete", "locks_diagnosed", "stats_checked"}),
    }
)

VERIFICATION_METRICS: Mapping[Bottleneck, str] = MappingProxyType(
    {
        B.MISSING_INDEX_FULL_SCAN: (
            "Slow query uses an Index Scan, mean latency < 50 ms, and service p95 < SLO "
            "for 15 minutes"
        ),
        B.CONNECTION_POOL_SATURATION: (
            "Pool waiting = 0, avg wait < pool-wait SLO, zero acquire timeouts and "
            "idle-in-transaction < 5 for 15 minutes"
        ),
        B.RESOURCE_SATURATION_PEAK_LOAD: (
            "At the next peak: CPU < 75%, DB connections < 80% of max, and p95 < SLO"
        ),
        B.LOCK_CONTENTION: "Lock waits = 0 and no lock-timeout log entries for 15 minutes",
        B.STALE_STATISTICS: (
            "Planner estimated rows within 10x of actual rows and query mean latency back "
            "to baseline"
        ),
        B.HEALTHY: "p95 stays below the SLO and pool waiting stays at 0",
        B.AMBIGUOUS: (
            "Telemetry complete for 3+ windows and one hypothesis clearly supported "
            "(e.g. lock waits vs. estimate error) before any optimization"
        ),
    }
)

BOTTLENECK_DESCRIPTIONS: Mapping[Bottleneck, str] = MappingProxyType(
    {
        B.MISSING_INDEX_FULL_SCAN: "A frequent query scans a large table because no index covers its filter",
        B.CONNECTION_POOL_SATURATION: "Requests wait for a connection because the pool is exhausted",
        B.RESOURCE_SATURATION_PEAK_LOAD: "Traffic peak saturates database CPU and connections",
        B.LOCK_CONTENTION: "Sessions wait on locks held by other transactions",
        B.STALE_STATISTICS: "Planner estimates are far from reality because statistics are stale",
        B.HEALTHY: "No bottleneck: metrics are within the SLO",
        B.AMBIGUOUS: "Signals conflict or telemetry is missing; more diagnosis is needed",
    }
)
