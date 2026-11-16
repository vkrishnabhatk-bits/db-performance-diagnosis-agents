"""Weighted-evidence hypothesis scoring over the percept sequence.

Used by the goal-based agent (to form its belief state) and the utility-based
agent (as ``P(h | evidence)``). Each bottleneck gets a score from generic,
weighted features; scores are normalized into pseudo-probabilities.

A hypothesis *dominates* when its probability is at least ``DOMINANCE_MIN``
and at least ``DOMINANCE_RATIO`` times the runner-up. Otherwise the evidence is
treated as ambiguous.
"""

from __future__ import annotations

from dataclasses import dataclass

from dbdiag.agents.common import fraction, safe_correlation
from dbdiag.knowledge import THRESHOLDS
from dbdiag.models import Bottleneck
from dbdiag.perception import Percept

B = Bottleneck

DOMINANCE_MIN = 0.5
DOMINANCE_RATIO = 1.5
AMBIGUITY_PRIOR = 0.1  # small constant mass on "we cannot tell yet"


@dataclass(frozen=True)
class HypothesisScores:
    raw: dict[Bottleneck, float]
    probabilities: dict[Bottleneck, float]
    features: dict[str, float]
    contributions: dict[Bottleneck, list[str]]

    @property
    def ranked(self) -> list[tuple[Bottleneck, float]]:
        return sorted(self.probabilities.items(), key=lambda kv: (-kv[1], kv[0].value))

    @property
    def top(self) -> tuple[Bottleneck, float]:
        return self.ranked[0]

    @property
    def dominant(self) -> Bottleneck | None:
        (best, p1), (_, p2) = self.ranked[0], self.ranked[1]
        if p1 >= DOMINANCE_MIN and p1 >= DOMINANCE_RATIO * max(p2, 1e-9):
            return best
        return None


def score_hypotheses(percepts: list[Percept]) -> HypothesisScores:
    th = THRESHOLDS
    f = {s: fraction(percepts, s) for s in (
        "full_scan_large_table", "unindexed_filter", "latency_slo_breach", "pool_saturated",
        "pool_timeouts", "idle_in_transaction_high", "connection_leak_logs", "cpu_high",
        "db_conn_high", "lock_waits", "stale_statistics", "missing_metrics", "healthy",
    )}
    first, last = percepts[0], percepts[-1]
    qps_ratio = last.qps / first.qps if first.qps else 1.0
    p95 = [p.p95_ms for p in percepts]
    corr_qps = safe_correlation([p.qps for p in percepts], p95)
    corr_cpu = safe_correlation([p.cpu_pct for p in percepts], p95)
    qps_rising = qps_ratio >= th.qps_rise_ratio
    features = {
        **{f"frac_{k}": round(v, 3) for k, v in f.items()},
        "qps_ratio": round(qps_ratio, 2),
        "corr_p95_qps": round(corr_qps, 2),
        "corr_p95_cpu": round(corr_cpu, 2),
    }

    contrib: dict[Bottleneck, list[str]] = {b: [] for b in B}
    raw: dict[Bottleneck, float] = {}

    def add(b: Bottleneck, value: float, why: str) -> None:
        if value > 0:
            raw[b] = raw.get(b, 0.0) + value
            contrib[b].append(f"+{value:.2f} {why}")

    # Missing index: a frequent large Seq Scan, worse if no index covers the filter.
    scan = f["full_scan_large_table"] * (1 + 0.5 * f["unindexed_filter"]) * (0.5 + 0.5 * f["latency_slo_breach"])
    add(B.MISSING_INDEX_FULL_SCAN, scan, f"large full scan in {f['full_scan_large_table']:.0%} of windows")

    # Pool: saturation plus its usual companions.
    add(B.CONNECTION_POOL_SATURATION, f["pool_saturated"], f"pool saturated in {f['pool_saturated']:.0%} of windows")
    add(B.CONNECTION_POOL_SATURATION, 0.4 * f["pool_timeouts"], "acquire timeouts")
    add(B.CONNECTION_POOL_SATURATION, 0.4 * f["idle_in_transaction_high"], "many idle-in-transaction sessions")
    add(B.CONNECTION_POOL_SATURATION, 0.4 * f["connection_leak_logs"], "connection-leak log entries")

    # Peak load: latency rising with traffic and CPU.
    add(B.RESOURCE_SATURATION_PEAK_LOAD, f["cpu_high"], f"CPU high in {f['cpu_high']:.0%} of windows")
    add(B.RESOURCE_SATURATION_PEAK_LOAD, f["db_conn_high"], "DB connections near max")
    if qps_rising:
        add(B.RESOURCE_SATURATION_PEAK_LOAD, max(corr_qps, 0.0), f"QPS x{qps_ratio:.1f} and p95 tracks QPS (r={corr_qps:.2f})")
        add(B.RESOURCE_SATURATION_PEAK_LOAD, 0.5 * max(corr_cpu, 0.0), f"p95 tracks CPU (r={corr_cpu:.2f})")

    add(B.LOCK_CONTENTION, f["lock_waits"], f"lock waits in {f['lock_waits']:.0%} of windows")
    add(B.STALE_STATISTICS, 0.8 * f["stale_statistics"], "planner estimates far from actual rows")
    add(B.HEALTHY, f["healthy"], f"healthy in {f['healthy']:.0%} of windows")
    add(B.AMBIGUOUS, AMBIGUITY_PRIOR, "prior")
    add(B.AMBIGUOUS, 0.6 * f["missing_metrics"], f"telemetry missing in {f['missing_metrics']:.0%} of windows")

    total = sum(raw.values())
    probs = {b: raw.get(b, 0.0) / total for b in B}
    return HypothesisScores(
        raw={b: round(raw.get(b, 0.0), 4) for b in B},
        probabilities=probs,
        features=features,
        contributions=contrib,
    )
