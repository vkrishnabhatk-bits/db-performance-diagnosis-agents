"""The five classical agents: valid output, correct diagnoses, distinct mechanisms."""

from __future__ import annotations

import pytest

from dbdiag.agents.base import BaseAgent
from dbdiag.agents.goal_based import GoalBasedAgent, bfs
from dbdiag.agents.learning_agent import LearningAgent
from dbdiag.agents.model_based import ModelBasedReflexAgent, WorldState, update_state
from dbdiag.agents.simple_reflex import SimpleReflexAgent
from dbdiag.agents.utility_based import UtilityBasedAgent, expected_utility
from dbdiag.knowledge import ACTIONS, GOAL_FACTS
from dbdiag.models import AgentDecision, Bottleneck
from dbdiag.perception import percept_sequence
from dbdiag.safety import check_decision
from dbdiag.scenarios import fingerprint

from tests.conftest import ALL_IDS

B = Bottleneck


@pytest.fixture
def learning(tmp_path):
    return LearningAgent(experience_path=tmp_path / "experience.json", epsilon=0.0)


def make(agent_cls, learning_agent) -> BaseAgent:
    return learning_agent if agent_cls is LearningAgent else agent_cls()


CLASSICAL = [SimpleReflexAgent, ModelBasedReflexAgent, GoalBasedAgent, UtilityBasedAgent, LearningAgent]
NON_REFLEX = [ModelBasedReflexAgent, GoalBasedAgent, UtilityBasedAgent, LearningAgent]


@pytest.mark.parametrize("agent_cls", CLASSICAL, ids=lambda c: c.__name__)
@pytest.mark.parametrize("sid", ALL_IDS)
def test_valid_decision_with_all_fields(agent_cls, sid, views, learning):
    d = make(agent_cls, learning).decide(views[sid])
    assert isinstance(d, AgentDecision)
    assert d.agent == agent_cls.name
    assert d.scenario_id == sid
    assert d.scenario_fingerprint == fingerprint(views[sid])
    assert d.symptom_summary.strip()
    assert d.evidence and all(e.strip() for e in d.evidence)
    assert d.recommended_steps
    assert d.verification_metric.strip()
    assert 0 <= d.confidence <= 1
    assert d.reasoning_trace
    assert d.extras["action_ids"] and all(a in ACTIONS for a in d.extras["action_ids"])
    assert check_decision(d) == []


@pytest.mark.parametrize("agent_cls", CLASSICAL, ids=lambda c: c.__name__)
def test_agents_refuse_ground_truth(agent_cls, all_scenarios, learning):
    with pytest.raises(ValueError, match="agent_view"):
        make(agent_cls, learning).decide(all_scenarios["A"])


@pytest.mark.parametrize("agent_cls", NON_REFLEX, ids=lambda c: c.__name__)
@pytest.mark.parametrize("sid", ["A", "B", "C"])
def test_non_reflex_agents_get_required_scenarios_right(agent_cls, sid, views, all_scenarios, learning):
    d = make(agent_cls, learning).decide(views[sid])
    assert d.bottleneck == all_scenarios[sid].ground_truth.bottleneck


@pytest.mark.parametrize("sid", ["A", "B"])
def test_simple_reflex_gets_a_and_b_right(sid, views, all_scenarios):
    assert SimpleReflexAgent().decide(views[sid]).bottleneck == all_scenarios[sid].ground_truth.bottleneck


def test_simple_reflex_is_fooled_by_c(views):
    """Documented weakness: on C's last window the pool is saturated, so rule 1 fires."""
    d = SimpleReflexAgent().decide(views["C"])
    assert d.bottleneck == B.CONNECTION_POOL_SATURATION
    assert d.extras["rule_fired"] == 1


@pytest.mark.parametrize("agent_cls", CLASSICAL, ids=lambda c: c.__name__)
def test_healthy_baseline(agent_cls, views, learning):
    d = make(agent_cls, learning).decide(views["N"])
    assert d.bottleneck == B.HEALTHY
    assert d.extras["action_ids"] == ["continue_monitoring"]


@pytest.mark.parametrize("agent_cls", NON_REFLEX, ids=lambda c: c.__name__)
def test_ambiguous_edge_lowers_confidence(agent_cls, views, learning):
    d = make(agent_cls, learning).decide(views["E"])
    assert d.bottleneck == B.AMBIGUOUS or d.confidence < 0.6
    kinds = {ACTIONS[a].kind for a in d.extras["action_ids"]}
    assert kinds == {"diagnostic"}, "no changes before the cause is confirmed"


@pytest.mark.parametrize("agent_cls", CLASSICAL, ids=lambda c: c.__name__)
@pytest.mark.parametrize("sid", ALL_IDS)
def test_diagnostics_come_before_changes(agent_cls, sid, views, learning):
    ids = make(agent_cls, learning).decide(views[sid]).extras["action_ids"]
    provided: set[str] = set()
    for a in ids:
        assert ACTIONS[a].preconditions <= provided, f"{a} before its diagnostic in {ids}"
        provided |= ACTIONS[a].effects


# ---- mechanism-specific checks -------------------------------------------
def test_simple_reflex_uses_latest_window_only(views):
    d = SimpleReflexAgent().decide(views["E"])
    assert "latest window 09:05 only" in d.reasoning_trace[0]
    assert any("cpu_pct is missing" in t for t in d.reasoning_trace)


def test_model_based_state_tracks_persistence_and_stale_metrics(views):
    state = WorldState()
    for p in percept_sequence(views["E"]):
        update_state(state, p)
    assert state.windows == 6
    assert state.cpu_stale_windows == 4 and state.last_known_cpu == 36
    assert state.persisted("stale_statistics") == 6


def test_model_based_treats_late_pool_saturation_as_consequence(views):
    d = ModelBasedReflexAgent().decide(views["C"])
    assert any("consequence of the load" in t for t in d.reasoning_trace)


def test_goal_based_plan_respects_preconditions(views):
    d = GoalBasedAgent().decide(views["A"])
    assert d.extras["plan"] == ["explain_analyze_in_staging", "review_index_proposal"]
    assert d.extras["goal_reached"] and d.extras["nodes_expanded"] > 0


def test_bfs_finds_shortest_plan_for_every_goal():
    for target, goal in GOAL_FACTS.items():
        result = bfs(frozenset(), goal, target)
        assert result.plan is not None, target
        facts: set[str] = set()
        for a in result.plan:
            assert ACTIONS[a].preconditions <= facts
            facts |= ACTIONS[a].effects
        assert goal <= facts


def test_goal_based_cannot_reach_fix_goal_on_e(views):
    d = GoalBasedAgent().decide(views["E"])
    assert not d.extras["fix_goal_reachable"]
    assert set(d.extras["plan"]) == {"collect_missing_telemetry", "check_locks", "analyze_stats_in_staging"}


def test_utility_table_and_weights_change_choice(views):
    d = UtilityBasedAgent().decide(views["C"])
    table = d.extras["utility_table"]
    assert len(table) == len(ACTIONS)
    assert table == sorted(table, key=lambda r: -r["EU"])
    assert d.extras["best_action"] == table[0]["action"]
    # Ignore cost and time-to-effect: the higher-benefit capacity change wins outright.
    cheap = UtilityBasedAgent(weights={"risk": 0.0, "cost": 0.0, "time": 0.0}).decide(views["C"])
    assert cheap.extras["best_action"] == "scale_read_replicas_or_cache"
    # Make cost very expensive: the cheap rate limit beats the replica build-out.
    costly = UtilityBasedAgent(weights={"cost": 1.0}).decide(views["C"])
    assert costly.extras["best_action"] == "rate_limit_peak_traffic"


def test_expected_utility_formula():
    probs = {b: 0.0 for b in B}
    probs[B.MISSING_INDEX_FULL_SCAN] = 1.0
    rows = {r["action"]: r for r in expected_utility(probs, {"risk": 0.3, "cost": 0.2, "time": 0.1})}
    a = ACTIONS["review_index_proposal"]
    assert rows[a.id]["EU"] == pytest.approx(1.0 - 0.3 * a.risk - 0.2 * a.cost - 0.1 * a.time_to_effect, abs=1e-3)
