"""Simple reflex agent: condition -> action rules on the current percept only.

It sees the **latest window** (plus the static query plans) and has no memory.
The rules are checked in order and the first match wins. That is its known
weakness: in a load-driven incident (scenario C) the last window shows the pool
saturated, so rule 1 fires even though the cause is the traffic peak.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from dbdiag.agents.base import BaseAgent
from dbdiag.agents.common import DEFAULT_PLAYBOOK, evidence_lines, render_steps, symptom_summary, verification_metric
from dbdiag.models import AgentDecision, Bottleneck, Scenario
from dbdiag.perception import Percept, perceive

B = Bottleneck


@dataclass(frozen=True)
class Rule:
    number: int
    name: str
    condition: Callable[[Percept], bool]
    describe: Callable[[Percept], str]  # the values that made it fire
    bottleneck: Bottleneck
    confidence: float


def _cpu(p: Percept) -> str:
    return "missing" if p.cpu_pct is None else f"{p.cpu_pct:.0f}%"


RULES: tuple[Rule, ...] = (
    Rule(
        1, "pool saturated",
        lambda p: p.pool_waiting > 0 and p.pool_active >= p.pool_size,
        lambda p: f"pool.active {p.pool_active} == size {p.pool_size} and pool.waiting {p.pool_waiting} > 0",
        B.CONNECTION_POOL_SATURATION, 0.8,
    ),
    Rule(
        2, "large full scan",
        lambda p: p.has("full_scan_large_table"),
        lambda p: next(
            f"{pl.query_id} Seq Scan examines {pl.rows_examined:,} rows for {pl.rows_returned} "
            f"(ratio {pl.selectivity_ratio:,.0f})"
            for pl in p.plans if pl.is_large_table_scan
        ),
        B.MISSING_INDEX_FULL_SCAN, 0.8,
    ),
    Rule(
        3, "CPU high",
        lambda p: p.has("cpu_high"),
        lambda p: f"cpu_pct {_cpu(p)} >= 85",
        B.RESOURCE_SATURATION_PEAK_LOAD, 0.75,
    ),
    Rule(
        4, "lock waits",
        lambda p: p.has("lock_waits"),
        lambda p: f"lock_waits {p.lock_waits}, lock-wait log entries {p.logs.count('lock_wait')}",
        B.LOCK_CONTENTION, 0.7,
    ),
    Rule(
        5, "everything inside the SLO",
        lambda p: p.has("healthy"),
        lambda p: f"p95 {p.p95_ms:.0f} <= {p.slo_p95_ms:.0f} ms, pool waiting 0, no problem symptoms",
        B.HEALTHY, 0.9,
    ),
    Rule(
        6, "fallback",
        lambda p: True,
        lambda p: "no earlier rule matched",
        B.AMBIGUOUS, 0.4,
    ),
)


class SimpleReflexAgent(BaseAgent):
    name = "Simple Reflex"
    mechanism = "Ordered condition-action rules on the latest window only; first match wins."

    def decide(self, scenario: Scenario) -> AgentDecision:
        p = perceive(scenario)  # latest window only: no memory
        trace = [f"Percept: latest window {p.t} only ({p.n_windows} windows exist; earlier ones are ignored)"]
        if p.cpu_pct is None:
            trace.append("cpu_pct is missing in this window, so rule 3 cannot match")

        fired: Rule | None = None
        for rule in RULES:
            if rule.condition(p):
                fired = rule
                trace.append(f"Rule {rule.number} ({rule.name}) FIRED: {rule.describe(p)}")
                break
            trace.append(f"Rule {rule.number} ({rule.name}) did not match")
        assert fired is not None  # the fallback always matches

        actions = list(DEFAULT_PLAYBOOK[fired.bottleneck])
        return self.make_decision(
            scenario,
            symptom_summary=symptom_summary(p, fired.bottleneck, f"Latest window {p.t}"),
            evidence=evidence_lines(p),
            bottleneck=fired.bottleneck,
            recommended_steps=render_steps(actions, p),
            verification_metric=verification_metric(fired.bottleneck, p),
            confidence=fired.confidence,
            reasoning_trace=trace,
            extras={
                "action_ids": actions,
                "rule_fired": fired.number,
                "rule_name": fired.name,
                "mechanism_detail": f"rule {fired.number} ({fired.name}) on window {p.t}",
                "why": (
                    f"rule {fired.number} ({fired.name}) matched the latest window {p.t}; "
                    "it has no memory of earlier windows and stops at the first match"
                ),
            },
        )
