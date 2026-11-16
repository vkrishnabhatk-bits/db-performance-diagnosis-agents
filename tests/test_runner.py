"""Runner fairness and the comparison report."""

from __future__ import annotations

import json

import pytest

from dbdiag.agents.base import BaseAgent
from dbdiag.comparison import agreement_summary, comparison_table, explain_differences, to_markdown
from dbdiag.runner import AGENT_ORDER, build_agents, run_all_agents
from dbdiag.scenarios import fingerprint

from tests.conftest import ALL_IDS
from tests.test_llm_agent import ScriptedChat, answer, investigate


def scripted_llm():
    return ScriptedChat([investigate(), answer()])


@pytest.fixture
def opts(tmp_path):
    return {"epsilon": 0.0, "experience_path": tmp_path / "exp.json"}


def test_all_six_agents_share_one_fingerprint(all_scenarios, opts):
    result = run_all_agents(all_scenarios["A"], llm=scripted_llm(), **opts)
    assert not result.errors
    assert [d.agent for d in result.decisions] == list(AGENT_ORDER)
    fps = {d.scenario_fingerprint for d in result.decisions}
    assert fps == {fingerprint(all_scenarios["A"])} == {result.fingerprint}


def test_every_agent_receives_the_same_object_without_ground_truth(all_scenarios, opts):
    seen = []

    class Spy(BaseAgent):
        name = "spy"
        mechanism = "records its input"

        def decide(self, scenario):
            seen.append(scenario)
            raise RuntimeError("spy only")

    result = run_all_agents(all_scenarios["B"], agents=[Spy(), Spy()])
    assert len(seen) == 2 and seen[0] is seen[1]
    assert seen[0].ground_truth is None
    assert set(result.errors) == {"spy"}


@pytest.mark.parametrize("sid", ALL_IDS)
def test_classical_runs_have_no_errors(sid, all_scenarios, opts):
    result = run_all_agents(all_scenarios[sid], include_llm=False, **opts)
    assert not result.errors and len(result.decisions) == 5


def test_learn_flag_runs_critic_after_decisions(all_scenarios, opts):
    result = run_all_agents(all_scenarios["A"], include_llm=False, learn=True, **opts)
    assert result.learning_update["outcome"] == "success"
    stored = json.loads(opts["experience_path"].read_text())
    assert stored["history"][-1]["scenario"] == "A"


def test_without_learn_flag_experience_is_unchanged(all_scenarios, opts):
    run_all_agents(all_scenarios["A"], include_llm=False, **opts)
    assert json.loads(opts["experience_path"].read_text())["history"] == []


def test_comparison_table_and_explanation(all_scenarios, opts):
    scenario = all_scenarios["C"]
    result = run_all_agents(scenario, include_llm=False, **opts)
    df = comparison_table(result.decisions, scenario.ground_truth)
    assert list(df["Agent"]) == list(AGENT_ORDER[:5])
    assert {"Bottleneck", "Matches ground truth", "Top step", "Confidence", "Mechanism detail"} <= set(df.columns)
    assert list(df["Matches ground truth"]).count("✔") == 4  # Simple Reflex is fooled by C
    summary = agreement_summary(result.decisions)
    assert summary.startswith("4/5 agents agree on RESOURCE_SATURATION_PEAK_LOAD")
    text = explain_differences(result.decisions, scenario.ground_truth)
    assert "**Simple Reflex** **(disagrees)**" in text and "latest window" in text
    md = to_markdown(df)
    assert md.count("\n") == len(df) + 1 and md.startswith("| Agent |")


def test_build_agents_without_llm():
    names = [a.name for a in build_agents(include_llm=False)]
    assert names == list(AGENT_ORDER[:5])
