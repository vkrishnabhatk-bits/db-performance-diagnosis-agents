"""Base class for all six agents."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar

from dbdiag.models import AgentDecision, Bottleneck, Scenario
from dbdiag.safety import assert_safe
from dbdiag.scenarios import fingerprint


class BaseAgent(ABC):
    """Every agent turns the same scenario into an ``AgentDecision``.

    Subclasses implement ``decide`` and build their result with ``make_decision``,
    which stamps the scenario fingerprint and runs the safety guard.
    """

    name: ClassVar[str]
    mechanism: ClassVar[str]  # one line: how this agent decides

    @abstractmethod
    def decide(self, scenario: Scenario) -> AgentDecision:
        """Diagnose ``scenario`` (an agent view, without ground truth)."""

    def make_decision(
        self,
        scenario: Scenario,
        *,
        symptom_summary: str,
        evidence: list[str],
        bottleneck: Bottleneck,
        recommended_steps: list[str],
        verification_metric: str,
        confidence: float,
        reasoning_trace: list[str],
        extras: dict[str, Any] | None = None,
    ) -> AgentDecision:
        if scenario.ground_truth is not None:
            raise ValueError("agents must receive scenario.agent_view(), not the full scenario")
        decision = AgentDecision(
            agent=self.name,
            scenario_id=scenario.id,
            scenario_fingerprint=fingerprint(scenario),
            symptom_summary=symptom_summary,
            evidence=evidence,
            bottleneck=bottleneck,
            recommended_steps=recommended_steps,
            verification_metric=verification_metric,
            confidence=round(max(0.0, min(1.0, confidence)), 3),
            reasoning_trace=reasoning_trace,
            extras={"mechanism": self.mechanism, **(extras or {})},
        )
        return assert_safe(decision)
