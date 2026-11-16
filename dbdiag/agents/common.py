"""Helpers shared by the agents: evidence text, recommended steps, metrics.

These only *format* what an agent already decided. Each agent keeps its own
decision mechanism in its own module.
"""

from __future__ import annotations

from statistics import StatisticsError, correlation

from dbdiag.knowledge import ACTIONS, VERIFICATION_METRICS, get_action
from dbdiag.models import Bottleneck
from dbdiag.perception import Percept

B = Bottleneck

# Steps each classical agent falls back to per bottleneck (diagnostics first).
DEFAULT_PLAYBOOK: dict[Bottleneck, tuple[str, ...]] = {
    B.MISSING_INDEX_FULL_SCAN: ("explain_analyze_in_staging", "review_index_proposal"),
    B.CONNECTION_POOL_SATURATION: ("inspect_pool_usage", "fix_connection_leak", "tune_pool_and_timeouts"),
    B.RESOURCE_SATURATION_PEAK_LOAD: ("profile_peak_load", "rate_limit_peak_traffic", "scale_read_replicas_or_cache"),
    B.LOCK_CONTENTION: ("check_locks", "shorten_long_transactions"),
    B.STALE_STATISTICS: ("analyze_stats_in_staging", "schedule_stats_refresh"),
    B.HEALTHY: ("continue_monitoring",),
    B.AMBIGUOUS: ("collect_missing_telemetry", "check_locks", "analyze_stats_in_staging"),
}


# --------------------------------------------------------------------------- #
# Recommended steps
# --------------------------------------------------------------------------- #
def _detail(action_id: str, percept: Percept | None) -> str:
    """Scenario-specific detail appended to a catalogue action, if any."""
    if percept is None:
        return ""
    plan = percept.top_plan
    if action_id == "explain_analyze_in_staging" and plan:
        return f" Target: {plan.query_id} ({plan.node} on {plan.table})."
    if action_id == "review_index_proposal":
        scan = next((p for p in percept.plans if p.is_large_table_scan), None)
        if scan and scan.unindexed_filter_columns:
            cols = ", ".join(scan.unindexed_filter_columns)
            return f" Proposed index: {scan.table}({cols})."
    if action_id == "analyze_stats_in_staging":
        stale = next((p for p in percept.plans if p.has_stale_estimate), None)
        if stale:
            return f" Table: {stale.table} (last analyzed {stale.table_last_analyzed})."
    return ""


def step_text(action_id: str, percept: Percept | None = None) -> str:
    action = get_action(action_id)
    return f"[{action.id}] {action.title}: {action.description}{_detail(action_id, percept)}"


def render_steps(action_ids: list[str] | tuple[str, ...], percept: Percept | None = None) -> list[str]:
    return [step_text(a, percept) for a in action_ids]


def with_prerequisites(action_id: str, ranked_diagnostics: list[str] | None = None) -> list[str]:
    """``action_id`` preceded by diagnostics that provide its preconditions.

    ``ranked_diagnostics`` is the agent's own preference order for choosing a
    provider when several actions provide the same fact.
    """
    action = get_action(action_id)
    order = list(ranked_diagnostics or []) + [a for a in ACTIONS if a not in (ranked_diagnostics or [])]
    steps: list[str] = []
    for fact in sorted(action.preconditions):
        provider = next(
            (a for a in order if fact in ACTIONS[a].effects and not ACTIONS[a].preconditions),
            None,
        )
        if provider and provider not in steps:
            steps.append(provider)
    return [*steps, action_id]


def verification_metric(bottleneck: Bottleneck, percept: Percept | None = None) -> str:
    base = VERIFICATION_METRICS[bottleneck]
    if percept is None:
        return base
    now = f"now: p95 {percept.p95_ms:.0f} ms, pool waiting {percept.pool_waiting}"
    plan = percept.top_plan
    if bottleneck == B.MISSING_INDEX_FULL_SCAN and plan:
        now = f"now: {plan.query_id} mean {plan.mean_ms:.0f} ms, p95 {percept.p95_ms:.0f} ms"
    elif bottleneck == B.RESOURCE_SATURATION_PEAK_LOAD:
        cpu = "n/a" if percept.cpu_pct is None else f"{percept.cpu_pct:.0f}%"
        now = f"now: CPU {cpu}, DB connections {percept.db_conn_utilization:.0%}, p95 {percept.p95_ms:.0f} ms"
    elif bottleneck == B.LOCK_CONTENTION:
        now = f"now: {percept.lock_waits} lock waits"
    return f"{base} ({now})"


# --------------------------------------------------------------------------- #
# Evidence
# --------------------------------------------------------------------------- #
def evidence_lines(p: Percept) -> list[str]:
    """Concrete, value-carrying evidence for the symptoms present in ``p``."""
    out: list[str] = []
    at = f"at {p.t}"
    if p.has("latency_slo_breach"):
        out.append(f"p95 latency {p.p95_ms:.0f} ms {at} exceeds the {p.slo_p95_ms:.0f} ms SLO")
    if p.has("p99_spike"):
        out.append(f"p99 {p.p99_ms:.0f} ms is {p.p99_ms / p.p95_ms:.1f}x p95 {at} (intermittent spikes)")
    for pl in p.plans:
        if pl.is_large_table_scan:
            out.append(
                f"{pl.query_id} {pl.node} on {pl.table}: {pl.rows_examined:,} rows examined for "
                f"{pl.rows_returned} returned, {pl.calls_per_min:.0f} calls/min, mean {pl.mean_ms:.0f} ms"
            )
            if pl.unindexed_filter_columns:
                out.append(f"No index covers {pl.table}({', '.join(pl.unindexed_filter_columns)})")
        if pl.has_stale_estimate:
            out.append(
                f"{pl.query_id} planner estimate {pl.est_rows:,} rows vs {pl.rows_returned:,} actual "
                f"({pl.estimate_error_ratio:.0f}x off); {pl.table} last analyzed {pl.table_last_analyzed}"
            )
    if p.has("pool_saturated"):
        out.append(f"Pool {p.pool_active}/{p.pool_size} active with {p.pool_waiting} requests waiting {at}")
    if p.has("pool_wait_slo_breach"):
        out.append(f"Pool avg wait {p.pool_avg_wait_ms:.0f} ms exceeds the {p.slo_pool_wait_ms:.0f} ms SLO")
    if p.has("pool_timeouts"):
        out.append(f"{p.pool_timeouts} connection-acquire timeouts {at}")
    if p.has("idle_in_transaction_high"):
        out.append(f"{p.idle_in_transaction} sessions idle in transaction {at}")
    if p.has("db_conn_high"):
        out.append(f"DB connections {p.db_conn_active}/{p.db_conn_max} ({p.db_conn_utilization:.0%}) {at}")
    if p.has("cpu_high"):
        out.append(f"CPU {p.cpu_pct:.0f}% {at}")
    if p.has("io_wait_high"):
        out.append(f"I/O wait {p.io_wait_pct:.0f}% {at}")
    if p.has("cache_hit_low"):
        out.append(f"Cache hit ratio {p.cache_hit_ratio:.2f} {at}")
    if p.has("lock_waits"):
        out.append(f"{p.lock_waits} lock waits and {p.logs.count('lock_wait')} lock-wait log entries {at}")
    if p.has("missing_metrics"):
        out.append(f"Missing telemetry {at}: {', '.join(p.missing_metrics)}")
    for sample in p.logs.samples[:2]:
        if p.logs.errors or p.logs.warnings:
            out.append(f"Log: {sample}")
    if p.has("healthy"):
        out.append(
            f"All metrics within SLO {at}: p95 {p.p95_ms:.0f} ms (SLO {p.slo_p95_ms:.0f}), "
            f"pool waiting {p.pool_waiting}, CPU {p.cpu_pct if p.cpu_pct is not None else 'n/a'}%"
        )
    return out


def symptom_summary(p: Percept, bottleneck: Bottleneck, scope: str) -> str:
    from dbdiag.knowledge import BOTTLENECK_DESCRIPTIONS

    problems = sorted(s for s in p.symptoms if s != "healthy")
    seen = ", ".join(problems) if problems else "no problem symptoms"
    return (
        f"{scope}: p95 {p.p95_ms:.0f} ms vs {p.slo_p95_ms:.0f} ms SLO, QPS {p.qps:.0f}, "
        f"pool {p.pool_active}/{p.pool_size} ({p.pool_waiting} waiting). Symptoms: {seen}. "
        f"Diagnosis: {BOTTLENECK_DESCRIPTIONS[bottleneck]}."
    )


# --------------------------------------------------------------------------- #
# Small numeric helpers
# --------------------------------------------------------------------------- #
def safe_correlation(xs: list[float], ys: list[float]) -> float:
    """Pearson correlation over paired values with ``None`` dropped (0.0 if undefined)."""
    pairs = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    if len(pairs) < 3:
        return 0.0
    try:
        return correlation([x for x, _ in pairs], [y for _, y in pairs])
    except StatisticsError:  # constant series
        return 0.0


def fraction(percepts: list[Percept], symptom: str) -> float:
    return sum(p.has(symptom) for p in percepts) / len(percepts)
