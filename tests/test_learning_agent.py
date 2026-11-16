"""Learning agent: experience store, critic, learning element, problem generator."""

from __future__ import annotations

import json

import pytest

from dbdiag.agents.learning_agent import SEED_PATH, LearningAgent, option_key
from dbdiag.models import Bottleneck

B = Bottleneck


def write_seed(path, counts):
    path.write_text(json.dumps({"counts": counts, "history": []}))
    return path


@pytest.fixture
def agent(tmp_path):
    return LearningAgent(experience_path=tmp_path / "exp.json", epsilon=0.0)


def test_experience_created_from_seed(tmp_path):
    exp = tmp_path / "exp.json"
    LearningAgent(experience_path=exp, epsilon=0.0)
    assert json.loads(exp.read_text())["counts"] == json.loads(SEED_PATH.read_text())["counts"]


def test_feedback_updates_counts_and_persists(agent, views):
    d = agent.decide(views["A"])
    sig, opt = d.extras["signature"], d.extras["option"]
    before = agent.experience["counts"].get(sig, {}).get(opt, {"success": 0, "failure": 0})["failure"]
    entry = agent.feedback(d, success=False)
    assert entry["after"]["failure"] == before + 1
    reloaded = LearningAgent(experience_path=agent.experience_path, epsilon=0.0)
    assert reloaded.experience["counts"][sig][opt]["failure"] == before + 1
    assert reloaded.experience["history"][-1]["outcome"] == "failure"


def test_success_rates_flip_the_ranking(tmp_path, views):
    """Same evidence, different history -> different choice."""
    sig = "conn_up+cpu_up+high_latency+qps_up"
    load = B.RESOURCE_SATURATION_PEAK_LOAD
    favour_scale = write_seed(tmp_path / "s1.json", {sig: {option_key(load, "scale_read_replicas_or_cache"): {"success": 9, "failure": 0}}})
    favour_rate = write_seed(tmp_path / "s2.json", {sig: {option_key(load, "rate_limit_peak_traffic"): {"success": 9, "failure": 0}}})
    a1 = LearningAgent(experience_path=tmp_path / "e1.json", seed_path=favour_scale, epsilon=0.0)
    a2 = LearningAgent(experience_path=tmp_path / "e2.json", seed_path=favour_rate, epsilon=0.0)
    assert a1.decide(views["C"]).extras["chosen_action"] == "scale_read_replicas_or_cache"
    assert a2.decide(views["C"]).extras["chosen_action"] == "rate_limit_peak_traffic"


def test_repeated_failures_change_choice(tmp_path, views):
    agent = LearningAgent(experience_path=tmp_path / "exp.json", seed_path=tmp_path / "none.json", epsilon=0.0)
    first = agent.decide(views["B"])
    for _ in range(5):
        agent.feedback(first, success=False)
    second = agent.decide(views["B"])
    assert second.extras["option"] != first.extras["option"]
    assert "Experience changed the choice" in second.extras["why"]


def test_critic_uses_ground_truth_after_decision(agent, views, all_scenarios):
    d = agent.decide(views["A"])
    ok, reason = agent.critique(d, all_scenarios["A"].ground_truth)
    assert ok, reason
    bad, _ = agent.critique(d, all_scenarios["B"].ground_truth)
    assert not bad
    entry = agent.critique_and_learn(d, all_scenarios["A"].ground_truth)
    assert entry["outcome"] == "success" and entry["source"].startswith("critic")


def test_epsilon_zero_is_deterministic(agent, views):
    options = {agent.decide(views["C"]).extras["option"] for _ in range(20)}
    assert len(options) == 1


def test_epsilon_one_explores_second_best(tmp_path, views):
    greedy = LearningAgent(experience_path=tmp_path / "a.json", epsilon=0.0).decide(views["A"])
    explorer = LearningAgent(experience_path=tmp_path / "b.json", epsilon=1.0, rng_seed=1).decide(views["A"])
    assert explorer.extras["explored"]
    assert explorer.extras["option"] == greedy.extras["ranking"][1]["option"]
    assert any("Problem generator" in t for t in explorer.reasoning_trace)


def test_explanation_cites_counts(agent, views):
    d = agent.decide(views["A"])
    # Exact signature (7/1) plus a similar one (2/0 at similarity 2/3): 8.3 / 1.0.
    assert "8.3 successes / 1.0 failures" in d.extras["why"]
    assert "full_scan+high_latency (sim 1.00)" in d.extras["why"]
    assert "full_scan+high_latency+qps_up (sim 0.67)" in d.extras["why"]
    assert d.extras["success_rate"] == pytest.approx((8.333 + 1) / (9.333 + 2), abs=0.01)


def test_reset_restores_seed(agent, views):
    agent.feedback(agent.decide(views["A"]), success=False)
    agent.reset()
    assert agent.experience["history"] == []
    assert agent.experience["counts"] == json.loads(SEED_PATH.read_text())["counts"]


def test_similar_signatures_contribute(agent, views):
    """E's exact signature is not in the seed; a similar past incident is used."""
    d = agent.decide(views["E"])
    assert "sim 0.75" in d.extras["why"]
    assert d.bottleneck == B.AMBIGUOUS
