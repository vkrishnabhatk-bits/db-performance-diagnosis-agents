"""Run all six agents on one scenario, fairly.

- The scenario is converted to its agent view (ground truth removed) **once**
  and the same frozen object is handed to every agent.
- Every decision must carry the fingerprint of that scenario; a mismatch is an
  error, not something to patch over.
- An agent that fails (e.g. Ollama not running) is reported in ``errors``.
  No placeholder decision is invented for it.
- The learning agent's critic runs only when ``learn=True`` and only *after*
  all decisions are made; it is the only consumer of ground truth here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dbdiag.agents.base import BaseAgent
from dbdiag.agents.goal_based import GoalBasedAgent
from dbdiag.agents.learning_agent import DEFAULT_EPSILON, EXPERIENCE_PATH, SEED_PATH, LearningAgent
from dbdiag.agents.llm_agent import DEFAULT_BASE_URL, DEFAULT_MODEL, LLMAgent
from dbdiag.agents.model_based import ModelBasedReflexAgent
from dbdiag.agents.simple_reflex import SimpleReflexAgent
from dbdiag.agents.utility_based import UtilityBasedAgent
from dbdiag.models import AgentDecision, Scenario
from dbdiag.scenarios import fingerprint

AGENT_ORDER = (
    SimpleReflexAgent.name,
    ModelBasedReflexAgent.name,
    GoalBasedAgent.name,
    UtilityBasedAgent.name,
    LearningAgent.name,
    LLMAgent.name,
)


@dataclass
class RunResult:
    scenario_id: str
    fingerprint: str
    decisions: list[AgentDecision] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    learning_update: dict[str, Any] | None = None

    def decision(self, agent_name: str) -> AgentDecision | None:
        return next((d for d in self.decisions if d.agent == agent_name), None)


def build_agents(
    *,
    include_llm: bool = True,
    model_name: str = DEFAULT_MODEL,
    base_url: str = DEFAULT_BASE_URL,
    think: bool = False,
    epsilon: float = DEFAULT_EPSILON,
    utility_weights: dict[str, float] | None = None,
    experience_path: Path | str = EXPERIENCE_PATH,
    seed_path: Path | str = SEED_PATH,
    llm: Any | None = None,
) -> list[BaseAgent]:
    agents: list[BaseAgent] = [
        SimpleReflexAgent(),
        ModelBasedReflexAgent(),
        GoalBasedAgent(),
        UtilityBasedAgent(weights=utility_weights),
        LearningAgent(experience_path=experience_path, seed_path=seed_path, epsilon=epsilon),
    ]
    if include_llm:
        agents.append(LLMAgent(model_name=model_name, base_url=base_url, think=think, llm=llm))
    return agents


def run_all_agents(
    scenario: Scenario,
    *,
    agents: list[BaseAgent] | None = None,
    learn: bool = False,
    **agent_options: Any,
) -> RunResult:
    """Run every agent on ``scenario`` (full scenario or agent view)."""
    ground_truth = scenario.ground_truth
    view = scenario.agent_view() if ground_truth is not None else scenario
    expected = fingerprint(view)
    agents = agents if agents is not None else build_agents(**agent_options)

    result = RunResult(scenario_id=view.id, fingerprint=expected)
    for agent in agents:
        try:
            decision = agent.decide(view)
        except Exception as exc:  # reported, never replaced by a fake decision
            result.errors[agent.name] = f"{type(exc).__name__}: {exc}"
            continue
        if decision.scenario_fingerprint != expected:
            result.errors[agent.name] = (
                f"fingerprint mismatch: got {decision.scenario_fingerprint[:12]}, expected {expected[:12]}"
            )
            continue
        result.decisions.append(decision)

    if learn and ground_truth is not None:
        learner = next((a for a in agents if isinstance(a, LearningAgent)), None)
        decision = result.decision(LearningAgent.name)
        if learner is not None and decision is not None:
            result.learning_update = learner.critique_and_learn(decision, ground_truth)
    return result
