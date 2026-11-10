"""Safety guard and the safe-action catalogue."""

from __future__ import annotations

import pytest

from dbdiag.agents.base import BaseAgent
from dbdiag.knowledge import ACTIONS, GOAL_FACTS, VERIFICATION_METRICS, actions_for, get_action
from dbdiag.models import AgentDecision, Bottleneck
from dbdiag.safety import UnsafeRecommendationError, assert_safe, check_text, is_safe

UNSAFE = [
    ("DROP INDEX orders_created_at_idx", "destructive"),
    ("Clean up with TRUNCATE audit_log", "destructive"),
    ("DELETE FROM sessions WHERE expires_at < now()", "destructive"),
    ("UPDATE accounts SET balance = 0 WHERE id = 7", "destructive"),
    ("GRANT ALL ON payments TO app", "destructive"),
    ("CREATE INDEX idx_orders_email ON orders (customer_email)", "unqualified_schema_change"),
    ("alter table orders add column note text", "unqualified_schema_change"),
    ("Run VACUUM FULL orders", "unqualified_schema_change"),
    ("ANALYZE payments", "unqualified_schema_change"),
    ("Draft CREATE INDEX on orders(customer_email) and run it on production now", "direct_execution"),
    ("Proposal: execute CREATE INDEX CONCURRENTLY directly", "direct_execution"),
]

SAFE = [
    "Run EXPLAIN (ANALYZE, BUFFERS) for Q1 on a staging replica",
    "EXPLAIN ANALYZE SELECT 1",
    "Draft CREATE INDEX CONCURRENTLY ON orders (customer_email) for DBA review; do not execute it in production",
    "Propose ALTER SYSTEM SET idle_in_transaction_session_timeout = '60s' for review",
    "On staging, refresh statistics with ANALYZE payments and compare plans",
    "Query pg_locks joined with pg_stat_activity to find blockers",
    "Do not DROP INDEX orders_created_at_idx even though it is rarely used",
    "Analyze the logs for lock timeouts",  # English 'analyze', not SQL
    "Delete from the cache any stale entries",  # English, lower case
    "Keep monitoring the p95 latency",
]


@pytest.mark.parametrize(("text", "rule"), UNSAFE)
def test_unsafe_text_is_flagged(text, rule):
    issues = check_text(text)
    assert issues, text
    assert rule in {i.rule for i in issues}
    assert not is_safe(text)


@pytest.mark.parametrize("text", SAFE)
def test_safe_text_passes(text):
    assert check_text(text) == []


@pytest.mark.parametrize("action_id", list(ACTIONS))
def test_every_catalogue_action_is_safe(action_id):
    action = ACTIONS[action_id]
    assert is_safe(action.title), action.title
    assert is_safe(action.description), action.description


@pytest.mark.parametrize("bottleneck", list(Bottleneck))
def test_every_bottleneck_has_actions_goal_and_metric(bottleneck):
    assert actions_for(bottleneck)
    assert GOAL_FACTS[bottleneck]
    assert is_safe(VERIFICATION_METRICS[bottleneck])


def test_goal_facts_are_reachable_from_catalogue():
    produced = {fact for a in ACTIONS.values() for fact in a.effects}
    for bottleneck, facts in GOAL_FACTS.items():
        assert facts <= produced, bottleneck
    for action in ACTIONS.values():
        assert action.preconditions <= produced, action.id


def test_actions_for_orders_by_benefit():
    ranked = actions_for(Bottleneck.MISSING_INDEX_FULL_SCAN)
    assert ranked[0].id == "review_index_proposal"
    benefits = [a.benefit_for(Bottleneck.MISSING_INDEX_FULL_SCAN) for a in ranked]
    assert benefits == sorted(benefits, reverse=True)


def test_get_action_unknown():
    with pytest.raises(KeyError, match="known"):
        get_action("drop_everything")


def _decision(steps: list[str]) -> AgentDecision:
    return AgentDecision(
        agent="test",
        scenario_id="A",
        scenario_fingerprint="x",
        symptom_summary="s",
        evidence=["e"],
        bottleneck=Bottleneck.MISSING_INDEX_FULL_SCAN,
        recommended_steps=steps,
        verification_metric=VERIFICATION_METRICS[Bottleneck.MISSING_INDEX_FULL_SCAN],
        confidence=0.9,
    )


def test_assert_safe_on_decisions():
    ok = _decision([get_action("review_index_proposal").description])
    assert assert_safe(ok) is ok
    with pytest.raises(UnsafeRecommendationError) as excinfo:
        assert_safe(_decision(["CREATE INDEX idx ON orders (customer_email)"]))
    assert excinfo.value.issues[0].rule == "unqualified_schema_change"


# --------------------------------------------------------------------------- #
# BaseAgent wiring used by every agent
# --------------------------------------------------------------------------- #
class _EchoAgent(BaseAgent):
    name = "echo"
    mechanism = "returns a fixed decision"

    def __init__(self, steps: list[str]):
        self.steps = steps

    def decide(self, scenario):
        return self.make_decision(
            scenario,
            symptom_summary="test",
            evidence=["e"],
            bottleneck=Bottleneck.HEALTHY,
            recommended_steps=self.steps,
            verification_metric="p95 below SLO",
            confidence=1.7,
            reasoning_trace=["fixed"],
        )


def test_base_agent_stamps_fingerprint_and_clamps_confidence(all_scenarios, views):
    from dbdiag.scenarios import fingerprint

    decision = _EchoAgent(["Keep monitoring"]).decide(views["N"])
    assert decision.scenario_fingerprint == fingerprint(all_scenarios["N"])
    assert decision.confidence == 1.0
    assert decision.extras["mechanism"] == "returns a fixed decision"


def test_base_agent_refuses_full_scenario_and_unsafe_steps(all_scenarios, views):
    with pytest.raises(ValueError, match="agent_view"):
        _EchoAgent(["Keep monitoring"]).decide(all_scenarios["N"])
    with pytest.raises(UnsafeRecommendationError):
        _EchoAgent(["DROP TABLE users"]).decide(views["N"])
