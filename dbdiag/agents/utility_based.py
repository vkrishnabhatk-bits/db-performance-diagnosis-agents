"""Utility-based agent: rank actions by expected utility.

``EU(a) = sum_h P(h) * benefit(a, h) - w_risk * risk(a) - w_cost * cost(a) - w_time * time_to_effect(a)``

``P(h)`` comes from weighted evidence (``hypotheses.score_hypotheses``);
benefit, risk, cost and time-to-effect come from the shared action catalogue.
The weights are one config dict that the UI can change, so the trade-off is
visible. When no hypothesis dominates, the benefit of a fix is spread thin and
cheap, low-risk diagnostics win: the intended behaviour on ambiguous evidence.
"""

from __future__ import annotations

from dbdiag.agents.base import BaseAgent
from dbdiag.agents.common import evidence_lines, render_steps, symptom_summary, verification_metric, with_prerequisites
from dbdiag.agents.hypotheses import score_hypotheses
from dbdiag.knowledge import ACTIONS
from dbdiag.models import AgentDecision, Bottleneck, Scenario
from dbdiag.perception import percept_sequence

B = Bottleneck

DEFAULT_WEIGHTS: dict[str, float] = {"risk": 0.3, "cost": 0.2, "time": 0.1}
MAX_FOLLOW_UPS = 1


def expected_utility(probabilities: dict[Bottleneck, float], weights: dict[str, float]) -> list[dict]:
    rows = []
    for a in ACTIONS.values():
        benefit = sum(p * a.benefit_for(h) for h, p in probabilities.items())
        eu = benefit - weights["risk"] * a.risk - weights["cost"] * a.cost - weights["time"] * a.time_to_effect
        rows.append({
            "action": a.id,
            "kind": a.kind,
            "expected_benefit": round(benefit, 3),
            "risk": a.risk,
            "cost": a.cost,
            "time_to_effect": a.time_to_effect,
            "EU": round(eu, 3),
        })
    rows.sort(key=lambda r: (-r["EU"], r["risk"], r["action"]))
    return rows


class UtilityBasedAgent(BaseAgent):
    name = "Utility-Based"
    mechanism = "Ranks every safe action by expected utility: P(h)-weighted benefit minus weighted risk, cost and time."

    def __init__(self, weights: dict[str, float] | None = None):
        self.weights = {**DEFAULT_WEIGHTS, **(weights or {})}

    def decide(self, scenario: Scenario) -> AgentDecision:
        percepts = percept_sequence(scenario)
        scores = score_hypotheses(percepts)
        probs = scores.probabilities
        leading, p_lead = scores.top
        dominant = scores.dominant
        bottleneck = dominant if dominant is not None else B.AMBIGUOUS

        table = expected_utility(probs, self.weights)
        best = table[0]
        trace = [
            "P(h | evidence): " + ", ".join(f"{b.value}={p:.2f}" for b, p in scores.ranked if p > 0),
            f"Weights: {self.weights}",
            "Top EU: " + ", ".join(f"{r['action']}={r['EU']:.3f}" for r in table[:4]),
            f"Best action: {best['action']} (EU {best['EU']:.3f} = benefit {best['expected_benefit']:.3f} "
            f"- {self.weights['risk']}*{best['risk']} - {self.weights['cost']}*{best['cost']} "
            f"- {self.weights['time']}*{best['time_to_effect']})",
        ]

        ranked_ids = [r["action"] for r in table]
        actions = with_prerequisites(best["action"], ranked_diagnostics=ranked_ids)
        if len(actions) > 1:
            trace.append(f"{best['action']} needs {actions[:-1]} first (its preconditions)")
        # Follow-ups: next best EU whose preconditions are already met. With no
        # dominant hypothesis, only diagnostics (no change before a confirmed cause).
        allowed = {"diagnostic"} if dominant is None else {"diagnostic", "optimization"}
        follow_ups = [
            r["action"] for r in table[1:]
            if r["EU"] > 0 and r["action"] not in actions and r["kind"] in allowed
            and ACTIONS[r["action"]].preconditions <= {f for a in actions for f in ACTIONS[a].effects}
        ][: MAX_FOLLOW_UPS + (1 if dominant is None else 0)]
        if best["action"] == "continue_monitoring":
            follow_ups = []
        actions += follow_ups
        if follow_ups:
            trace.append(f"Follow-up (next best EU): {follow_ups}")

        if dominant is None:
            trace.append(
                f"No hypothesis dominates (top {leading.value} P={p_lead:.2f}): diagnosis reported as AMBIGUOUS"
            )
            confidence = min(p_lead, 0.5)
        else:
            confidence = p_lead

        latest = percepts[-1]
        why_h = leading if dominant is None else dominant
        evidence = [f"Evidence for {why_h.value}: {c}" for c in scores.contributions[why_h]] + evidence_lines(latest)
        return self.make_decision(
            scenario,
            symptom_summary=symptom_summary(latest, bottleneck, f"{len(percepts)} windows"),
            evidence=evidence,
            bottleneck=bottleneck,
            recommended_steps=render_steps(actions, latest),
            verification_metric=verification_metric(bottleneck, latest),
            confidence=confidence,
            reasoning_trace=trace,
            extras={
                "action_ids": actions,
                "best_action": best["action"],
                "top_eu": best["EU"],
                "utility_table": table,
                "weights": dict(self.weights),
                "probabilities": {b.value: round(p, 3) for b, p in scores.ranked},
                "mechanism_detail": f"top EU {best['action']} = {best['EU']:.3f}",
                "why": (
                    f"{best['action']} had the highest expected utility ({best['EU']:.3f}) given "
                    f"P({leading.value})={p_lead:.2f} and weights {self.weights}"
                ),
            },
        )
