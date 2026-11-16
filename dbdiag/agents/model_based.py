"""Model-based reflex agent: rules applied to an internal state.

The agent walks the windows in order and folds each percept into a
``WorldState`` with ``update_state``. The state remembers trends (QPS, p95,
CPU, connections), how long each symptom has persisted, when it first
appeared, the last known value of missing metrics (with a stale flag), and
running log counts.

A small world model ("how the world evolves") then interprets the state:

- latency rising together with QPS and CPU  -> the load is the cause;
  pool queueing that only starts *after* latency rose is a consequence
- high latency with flat QPS and a persistent large Seq Scan -> the query plan
- pool waiting that persists while DB CPU stays low -> the pool / a leak
- several independent explanations, or missing telemetry -> ambiguous
"""

from __future__ import annotations

from dataclasses import dataclass, field

from dbdiag.agents.base import BaseAgent
from dbdiag.agents.common import (
    DEFAULT_PLAYBOOK,
    evidence_lines,
    render_steps,
    safe_correlation,
    symptom_summary,
    verification_metric,
)
from dbdiag.knowledge import THRESHOLDS
from dbdiag.models import AgentDecision, Bottleneck, Scenario
from dbdiag.perception import Percept, percept_sequence

B = Bottleneck
PERSISTENT_WINDOWS = 3  # a condition must hold this many windows to count as persistent


@dataclass
class WorldState:
    windows: int = 0
    times: list[str] = field(default_factory=list)
    qps: list[float] = field(default_factory=list)
    p95: list[float] = field(default_factory=list)
    cpu: list[float | None] = field(default_factory=list)
    db_conn_util: list[float] = field(default_factory=list)
    symptom_counts: dict[str, int] = field(default_factory=dict)
    symptom_streaks: dict[str, int] = field(default_factory=dict)  # current consecutive run
    first_seen: dict[str, int] = field(default_factory=dict)  # window index
    last_known_cpu: float | None = None
    cpu_stale_windows: int = 0
    missing_windows: int = 0
    log_counts: dict[str, int] = field(default_factory=dict)
    max_cpu: float | None = None

    def persisted(self, symptom: str) -> int:
        return self.symptom_counts.get(symptom, 0)


def update_state(state: WorldState, p: Percept) -> WorldState:
    """Fold one percept into the internal state (mutates and returns it)."""
    i = state.windows
    state.windows += 1
    state.times.append(p.t)
    state.qps.append(p.qps)
    state.p95.append(p.p95_ms)
    state.cpu.append(p.cpu_pct)
    state.db_conn_util.append(p.db_conn_utilization)

    for s in p.symptoms:
        state.symptom_counts[s] = state.symptom_counts.get(s, 0) + 1
        state.first_seen.setdefault(s, i)
    for s in set(state.symptom_streaks) | set(p.symptoms):
        state.symptom_streaks[s] = state.symptom_streaks.get(s, 0) + 1 if s in p.symptoms else 0

    if p.cpu_pct is None:
        state.cpu_stale_windows += 1
    else:
        state.last_known_cpu = p.cpu_pct
        state.cpu_stale_windows = 0
        state.max_cpu = p.cpu_pct if state.max_cpu is None else max(state.max_cpu, p.cpu_pct)
    if p.missing_metrics:
        state.missing_windows += 1
    for category, n in p.logs.category_counts:
        state.log_counts[category] = state.log_counts.get(category, 0) + n
    return state


@dataclass(frozen=True)
class Hypothesis:
    bottleneck: Bottleneck
    strength: float  # 0..1 share of windows supporting it
    reason: str


class ModelBasedReflexAgent(BaseAgent):
    name = "Model-Based Reflex"
    mechanism = "Builds an internal state over all windows (trends, persistence, onsets) and applies world-model rules to it."

    def decide(self, scenario: Scenario) -> AgentDecision:
        percepts = percept_sequence(scenario)
        state = WorldState()
        trace: list[str] = []
        for p in percepts:
            update_state(state, p)
            trace.append(f"update_state({p.t}): symptoms={sorted(p.symptoms)}")

        th = THRESHOLDS
        n = state.windows
        qps_ratio = state.qps[-1] / state.qps[0] if state.qps[0] else 1.0
        corr_qps = safe_correlation(state.qps, state.p95)
        corr_cpu = safe_correlation(state.cpu, state.p95)
        load_trend = qps_ratio >= th.qps_rise_ratio and corr_qps >= th.trend_correlation_min
        breach_onset = state.first_seen.get("latency_slo_breach")
        pool_onset = state.first_seen.get("pool_saturated")
        cpu_low = state.max_cpu is not None and state.max_cpu < th.cpu_high_pct

        trace.append(
            f"State: QPS x{qps_ratio:.2f}, corr(p95,QPS)={corr_qps:.2f}, corr(p95,CPU)={corr_cpu:.2f}, "
            f"max CPU={state.max_cpu}, latency breach from window {breach_onset}, "
            f"pool saturated from window {pool_onset}, telemetry missing in {state.missing_windows}/{n}"
        )

        hyps: list[Hypothesis] = []
        # World model 1: load drives latency.
        if load_trend:
            hyps.append(Hypothesis(
                B.RESOURCE_SATURATION_PEAK_LOAD, min(1.0, corr_qps),
                f"latency rises with QPS (x{qps_ratio:.1f}, r={corr_qps:.2f}) and CPU (r={corr_cpu:.2f})",
            ))
        # World model 2: the plan is slow regardless of traffic.
        scans = state.persisted("full_scan_large_table")
        if scans >= PERSISTENT_WINDOWS and not load_trend:
            hyps.append(Hypothesis(
                B.MISSING_INDEX_FULL_SCAN, scans / n,
                f"large Seq Scan present in {scans}/{n} windows while QPS is flat (x{qps_ratio:.2f})",
            ))
        # World model 3: pool waits persist while the DB is idle -> pool / leak.
        pool = state.persisted("pool_saturated")
        pool_is_consequence = (
            load_trend and pool_onset is not None and breach_onset is not None and pool_onset > breach_onset
        )
        if pool >= PERSISTENT_WINDOWS and cpu_low and not pool_is_consequence:
            leak = state.log_counts.get("connection_leak", 0) + state.log_counts.get("idle_in_transaction", 0)
            hyps.append(Hypothesis(
                B.CONNECTION_POOL_SATURATION, pool / n,
                f"pool saturated for {pool}/{n} windows with max CPU {state.max_cpu:.0f}% "
                f"and {leak} leak/idle-in-transaction log entries",
            ))
        elif pool and pool_is_consequence:
            trace.append(
                f"Pool saturation began at window {pool_onset}, after latency breached at window "
                f"{breach_onset}: treated as a consequence of the load, not the cause"
            )
        # Lock contention and stale statistics.
        locks = state.persisted("lock_waits")
        if locks >= PERSISTENT_WINDOWS:
            hyps.append(Hypothesis(B.LOCK_CONTENTION, locks / n, f"lock waits in {locks}/{n} windows"))
        stale = state.persisted("stale_statistics")
        if stale >= PERSISTENT_WINDOWS:
            hyps.append(Hypothesis(B.STALE_STATISTICS, stale / n, f"planner estimate error in {stale}/{n} windows"))

        for h in hyps:
            trace.append(f"World model supports {h.bottleneck.value} (strength {h.strength:.2f}): {h.reason}")

        missing_penalty = 0.25 * state.missing_windows / n
        if state.persisted("healthy") == n:
            bottleneck, confidence = B.HEALTHY, 0.9
            actions = list(DEFAULT_PLAYBOOK[B.HEALTHY])
            why = f"every one of the {n} windows was healthy"
        elif len(hyps) == 1:
            h = hyps[0]
            bottleneck = h.bottleneck
            confidence = 0.6 + 0.35 * h.strength - missing_penalty
            actions = list(DEFAULT_PLAYBOOK[bottleneck])
            why = h.reason
        else:
            bottleneck = B.AMBIGUOUS
            confidence = 0.45 - missing_penalty
            candidates = ", ".join(h.bottleneck.value for h in hyps) or "none"
            why = (
                f"{len(hyps)} competing explanations ({candidates})"
                + (f" and CPU missing for {state.cpu_stale_windows} windows (last known {state.last_known_cpu}%)"
                   if state.cpu_stale_windows else "")
            )
            actions = ["collect_missing_telemetry"] if state.missing_windows else []
            for h in hyps:
                diag = DEFAULT_PLAYBOOK[h.bottleneck][0]
                if diag not in actions:
                    actions.append(diag)
            if not actions:
                actions = list(DEFAULT_PLAYBOOK[B.AMBIGUOUS])
        if state.cpu_stale_windows:
            trace.append(
                f"CPU missing for the last {state.cpu_stale_windows} windows; using last known value "
                f"{state.last_known_cpu}% (stale) and lowering confidence by {missing_penalty:.2f}"
            )
        trace.append(f"Decision: {bottleneck.value} because {why}")

        latest = percepts[-1]
        evidence = [f"Over {n} windows: {h.reason}" for h in hyps] + evidence_lines(latest)
        return self.make_decision(
            scenario,
            symptom_summary=symptom_summary(latest, bottleneck, f"{n} windows {state.times[0]}-{state.times[-1]}"),
            evidence=evidence,
            bottleneck=bottleneck,
            recommended_steps=render_steps(actions, latest),
            verification_metric=verification_metric(bottleneck, latest),
            confidence=confidence,
            reasoning_trace=trace,
            extras={
                "action_ids": actions,
                "state": {
                    "windows": n,
                    "qps_ratio": round(qps_ratio, 2),
                    "corr_p95_qps": round(corr_qps, 2),
                    "corr_p95_cpu": round(corr_cpu, 2),
                    "max_cpu": state.max_cpu,
                    "last_known_cpu": state.last_known_cpu,
                    "cpu_stale_windows": state.cpu_stale_windows,
                    "symptom_counts": dict(sorted(state.symptom_counts.items())),
                    "first_seen": {k: state.times[v] for k, v in sorted(state.first_seen.items())},
                    "log_counts": dict(sorted(state.log_counts.items())),
                },
                "hypotheses": [h.bottleneck.value for h in hyps],
                "mechanism_detail": f"{len(hyps)} hypothesis(es) from state over {n} windows",
                "why": f"its internal state over all {n} windows showed {why}",
            },
        )
