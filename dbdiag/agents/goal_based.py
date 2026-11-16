"""Goal-based agent: search for an action sequence that reaches a goal state.

1. **Belief state.** Weighted evidence over all windows gives the leading
   hypothesis (``hypotheses.score_hypotheses``). If one dominates, the goal is
   that bottleneck resolved; otherwise the goal is to *reduce uncertainty*.
2. **Goal test.** The goal facts for the target (``knowledge.GOAL_FACTS``,
   symbolic stand-ins for "p95 and pool wait back within the SLO") all hold,
   and every step in the plan is safe. Steps that change something have
   preconditions that only a diagnostic can produce, so a change always comes
   after the diagnostic that confirms the hypothesis.
3. **Search.** Breadth-first search over STRIPS-style actions from
   ``knowledge.ACTIONS`` (preconditions/effects), depth <= ``MAX_DEPTH``.
   Successors are ordered by benefit for the hypothesis, then risk, so among
   equally short plans the most useful, least risky one is found first.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from dbdiag.agents.base import BaseAgent
from dbdiag.agents.common import evidence_lines, render_steps, symptom_summary, verification_metric
from dbdiag.agents.hypotheses import score_hypotheses
from dbdiag.knowledge import ACTIONS, GOAL_FACTS, SafeAction
from dbdiag.models import AgentDecision, Bottleneck, Scenario
from dbdiag.perception import percept_sequence
from dbdiag.safety import check_text

B = Bottleneck
MAX_DEPTH = 5


@dataclass(frozen=True)
class SearchResult:
    plan: tuple[str, ...] | None
    nodes_expanded: int
    frontier_max: int


def _safe(action: SafeAction) -> bool:
    return not check_text(action.description)


def bfs(start: frozenset[str], goal: frozenset[str], target: Bottleneck, max_depth: int = MAX_DEPTH) -> SearchResult:
    """Shortest safe action sequence whose cumulative effects contain ``goal``."""
    ordered = sorted(
        (a for a in ACTIONS.values() if _safe(a)),
        key=lambda a: (-a.benefit_for(target), a.risk, a.id),
    )
    frontier: deque[tuple[frozenset[str], tuple[str, ...]]] = deque([(start, ())])
    visited = {start}
    expanded = 0
    frontier_max = 1
    while frontier:
        facts, plan = frontier.popleft()
        if goal <= facts:
            return SearchResult(plan, expanded, frontier_max)
        if len(plan) >= max_depth:
            continue
        expanded += 1
        for action in ordered:
            if not action.preconditions <= facts:
                continue
            new_facts = facts | action.effects
            if new_facts == facts or new_facts in visited:
                continue
            visited.add(new_facts)
            frontier.append((new_facts, (*plan, action.id)))
        frontier_max = max(frontier_max, len(frontier))
    return SearchResult(None, expanded, frontier_max)


class GoalBasedAgent(BaseAgent):
    name = "Goal-Based"
    mechanism = "Breadth-first search over actions with STRIPS preconditions/effects until the goal state (bottleneck resolved, safely) is reached."

    def decide(self, scenario: Scenario) -> AgentDecision:
        percepts = percept_sequence(scenario)
        scores = score_hypotheses(percepts)
        leading, p_lead = scores.top
        dominant = scores.dominant

        trace = [
            "Belief: " + ", ".join(f"{b.value}={p:.2f}" for b, p in scores.ranked if p > 0),
        ]
        if dominant is not None:
            target = dominant
            trace.append(f"{target.value} dominates (P={p_lead:.2f}); goal = {target.value} resolved")
        else:
            target = B.AMBIGUOUS
            trace.append(
                f"No hypothesis dominates (top {leading.value} P={p_lead:.2f}); a fix goal cannot be set yet, "
                "so the goal is to reduce uncertainty"
            )

        goal = GOAL_FACTS[target]
        start: frozenset[str] = frozenset()
        trace.append(f"Start state: {sorted(start) or '{}'} (nothing diagnosed yet); goal facts: {sorted(goal)}")
        result = bfs(start, goal, target)
        trace.append(f"BFS expanded {result.nodes_expanded} nodes (max frontier {result.frontier_max}, depth <= {MAX_DEPTH})")

        if result.plan is None:  # not expected with the current catalogue, but handled
            plan = ("collect_missing_telemetry",)
            reached = False
            trace.append("No plan reaches the goal; falling back to gathering more data")
        else:
            plan = result.plan
            reached = True
            facts: frozenset[str] = start
            for step, action_id in enumerate(plan, 1):
                a = ACTIONS[action_id]
                facts = facts | a.effects
                pre = sorted(a.preconditions) or "-"
                trace.append(f"Step {step}: {action_id} (pre {pre}) -> state {sorted(facts)}")
            trace.append("Goal test passed: goal facts hold and every step is a safe recommendation")

        if target == B.AMBIGUOUS:
            trace.append(
                f"Fix goal not reachable yet: the plan only gathers evidence; re-plan once one hypothesis "
                f"dominates (currently {leading.value} at {p_lead:.2f})"
            )
            confidence = min(p_lead, 0.5)
        else:
            confidence = p_lead

        latest = percepts[-1]
        evidence = [
            f"Evidence for {target.value if target != B.AMBIGUOUS else leading.value}: {c}"
            for c in scores.contributions[target if target != B.AMBIGUOUS else leading]
        ] + evidence_lines(latest)
        actions = list(plan)
        return self.make_decision(
            scenario,
            symptom_summary=symptom_summary(latest, target, f"{len(percepts)} windows"),
            evidence=evidence,
            bottleneck=target,
            recommended_steps=render_steps(actions, latest),
            verification_metric=verification_metric(target, latest),
            confidence=confidence,
            reasoning_trace=trace,
            extras={
                "action_ids": actions,
                "goal": sorted(goal),
                "plan": actions,
                "plan_length": len(actions),
                "nodes_expanded": result.nodes_expanded,
                "goal_reached": reached,
                "fix_goal_reachable": target != B.AMBIGUOUS,
                "belief": {b.value: round(p, 3) for b, p in scores.ranked},
                "mechanism_detail": f"plan of {len(actions)} step(s), {result.nodes_expanded} nodes expanded",
                "why": (
                    f"its search found the shortest safe plan {' -> '.join(actions)} to the goal {sorted(goal)}"
                    + ("" if target != B.AMBIGUOUS else
                       f"; no hypothesis dominated (top {leading.value} {p_lead:.2f}), so it planned to reduce uncertainty")
                ),
            },
        )
