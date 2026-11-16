"""LLM agent.

By default a scripted fake chat model drives the loop, so these tests need no
Ollama. ``pytest -m ollama`` runs the real local model on A, B and C.
"""

from __future__ import annotations

import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from dbdiag.agents.llm_agent import (
    LLMAgent,
    LLMOutputError,
    LLMUnavailableError,
    extract_numbers,
    make_tools,
    verify_evidence,
)
from dbdiag.models import Bottleneck
from dbdiag.runner import run_all_agents
from dbdiag.safety import check_decision
from dbdiag.scenarios import fingerprint

B = Bottleneck


class ScriptedChat:
    """Minimal chat model: returns scripted AIMessages in order."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[list] = []
        self.bound: list[str] | None = None

    def bind_tools(self, tools):
        self.bound = [t.name for t in tools]
        return self

    def invoke(self, messages):
        self.calls.append(list(messages))
        if not self.responses:
            raise AssertionError("model called more often than scripted")
        return self.responses.pop(0)


def call(name, args=None, i=0):
    return {"name": name, "args": args or {}, "id": f"call_{name}_{i}", "type": "tool_call"}


def tools_msg(*calls):
    return AIMessage(content="", tool_calls=list(calls))


def answer(**overrides):
    body = {
        "symptom_summary": "p95 far above SLO; Q1 scans orders.",
        "evidence": [
            "Q1 Seq Scan on orders examined 2,400,000 rows and returned 3",
            "Q1 runs 3000 calls/min with mean 850 ms",
            "p95 reached 940 ms at 10:03",
            "CPU peaked at 97%",  # not in the tool outputs -> unverified
        ],
        "bottleneck": "MISSING_INDEX_FULL_SCAN",
        "recommended_steps": [
            "explain_analyze_in_staging: confirm the plan",
            "review_index_proposal: index on customer_email",
        ],
        "verification_metric": "Q1 mean latency < 50 ms and p95 < 200 ms for 15 minutes",
        "confidence": 0.9,
    }
    body.update(overrides)
    return AIMessage(content=json.dumps(body))


def investigate(i=0):
    """One turn that touches every evidence group."""
    return tools_msg(
        call("get_database_metrics", i=i), call("inspect_query_plan", {"query_id": "slowest"}, i=i),
        call("search_database_logs", {"level": "WARNING"}, i=i),
    )


def run(view, responses, **kw):
    chat = ScriptedChat(responses)
    return LLMAgent(llm=chat, **kw).decide(view), chat


# --------------------------------------------------------------------------- #
def test_tool_loop_records_calls_and_grounds_evidence(views):
    d, chat = run(views["A"], [investigate(), answer()])
    assert chat.bound and len(chat.bound) == 6
    assert d.bottleneck == B.MISSING_INDEX_FULL_SCAN
    assert [c["name"] for c in d.extras["tool_calls"]] == [
        "get_database_metrics", "inspect_query_plan", "search_database_logs",
    ]
    assert not d.extras["reprompted"] and len(d.extras["evidence_groups_covered"]) == 3
    assert all("latency_ms" in c for c in d.extras["tool_calls"])
    verified = {g["item"]: g["verified"] for g in d.extras["grounding"]}
    assert verified["Q1 Seq Scan on orders examined 2,400,000 rows and returned 3"]
    assert verified["p95 reached 940 ms at 10:03"]
    assert not verified["CPU peaked at 97%"]
    assert d.extras["grounding_score"] == pytest.approx(0.75)
    assert d.extras["grounded"] is True
    assert d.extras["action_ids"] == ["explain_analyze_in_staging", "review_index_proposal"]
    assert d.scenario_fingerprint == fingerprint(views["A"])
    assert check_decision(d) == []


def test_prompt_does_not_reveal_title_or_answer(views):
    _, chat = run(views["A"], [investigate(), answer()])
    first = chat.calls[0]
    user = next(m for m in first if isinstance(m, HumanMessage)).content
    assert views["A"].title not in user and "full table scan" not in user.lower()


def test_reprompts_when_fewer_than_two_tools(views):
    d, chat = run(views["A"], [
        tools_msg(call("get_database_metrics")),
        answer(),  # too early: only one tool
        tools_msg(call("inspect_query_plan", {"query_id": "Q1"}), call("search_database_logs")),
        answer(),
    ])
    assert d.extras["reprompts"] == 1 and d.extras["grounded"]
    reprompt = [m.content for m in chat.calls[2] if isinstance(m, HumanMessage)][-1]
    assert "at least 2" in reprompt and "query plans or indexes" in reprompt


def test_reprompts_for_skipped_evidence_groups(views):
    """Two tools but no plans or logs: the model is sent back to look."""
    d, chat = run(views["A"], [
        tools_msg(call("get_database_metrics"), call("get_connection_pool_status")),
        answer(),
        tools_msg(call("inspect_query_plan"), call("search_database_logs")),
        answer(),
    ])
    assert d.extras["reprompts"] == 1
    reprompt = [m.content for m in chat.calls[2] if isinstance(m, HumanMessage)][-1]
    assert "logs (search_database_logs)" in reprompt
    assert len(d.extras["evidence_groups_covered"]) == 3


def test_answer_accepted_after_max_reprompts(views):
    d, _ = run(views["A"], [
        tools_msg(call("get_database_metrics"), call("get_connection_pool_status")),
        answer(), answer(), answer(),
    ])
    assert d.extras["reprompts"] == 2 and d.extras["grounded"]
    assert d.extras["evidence_groups_covered"] == ["system metrics"]


def test_changes_reordered_after_their_diagnostics(views):
    d, _ = run(views["A"], [
        investigate(),
        answer(recommended_steps=["review_index_proposal: add index", "explain_analyze_in_staging: confirm"]),
    ])
    assert d.extras["action_ids"] == ["explain_analyze_in_staging", "review_index_proposal"]
    assert any("Diagnostics first" in t for t in d.reasoning_trace)


def test_marked_ungrounded_when_model_skips_tools(views):
    d, _ = run(views["A"], [answer(), answer(), answer()])
    assert d.extras["grounded"] is False
    assert d.extras["grounding_score"] == 0.0
    assert d.confidence < 0.5


def test_json_repair_retry(views):
    d, _ = run(views["B"], [
        tools_msg(call("get_connection_pool_status"), call("get_index_metadata"), call("search_database_logs", {"pattern": "pool"})),
        AIMessage(content="The pool is exhausted, I think."),
        answer(bottleneck="CONNECTION_POOL_SATURATION", evidence=["max_waiting 167"], recommended_steps=["inspect_pool_usage"]),
    ])
    assert d.bottleneck == B.CONNECTION_POOL_SATURATION
    assert any("repair" in t for t in d.reasoning_trace)


def test_invalid_json_twice_is_an_error_not_a_fake_answer(views):
    with pytest.raises(LLMOutputError):
        run(views["A"], [
            investigate(),
            AIMessage(content="no json"),
            AIMessage(content="still no json"),
        ])


def test_unsafe_recommendation_removed(views):
    d, _ = run(views["A"], [
        investigate(),
        answer(recommended_steps=[
            "CREATE INDEX idx ON orders(customer_email) and run it on production now",
            "explain_analyze_in_staging: confirm",
        ]),
    ])
    assert len(d.extras["unsafe_removed"]) == 1
    assert d.extras["action_ids"] == ["explain_analyze_in_staging"]
    assert check_decision(d) == []


def test_unknown_tool_and_bad_bottleneck_handled(views):
    d, chat = run(views["E"], [
        tools_msg(call("drop_table"), call("get_database_metrics"), call("inspect_query_plan"),
                  call("search_database_logs", {"pattern": "lock"})),
        answer(bottleneck="SOMETHING_ELSE", evidence=["lock wait timeout in logs"], recommended_steps=["check_locks"]),
        answer(bottleneck="ambiguous", evidence=["lock wait timeout in logs"], recommended_steps=["check_locks"]),
    ])
    assert d.bottleneck == B.AMBIGUOUS  # repaired, lower case accepted
    assert "error" in d.extras["tool_calls"][0]["result"]
    repair = [m.content for m in chat.calls[-1] if isinstance(m, HumanMessage)][-1]
    assert "bottleneck" in repair and "MISSING_INDEX_FULL_SCAN" in repair


def test_wrong_keys_trigger_repair(views):
    """qwen3 sometimes drifts to its own keys (seen in real runs: diagnosis/recommendations)."""
    drifted = AIMessage(content=json.dumps({
        "symptom_summary": "pool exhausted", "evidence": ["max_waiting 167"],
        "diagnosis": "pool exhaustion", "recommendations": ["fix leaks"],
    }))
    d, _ = run(views["B"], [
        investigate(), drifted,
        answer(bottleneck="CONNECTION_POOL_SATURATION", evidence=["max_waiting 167"],
               recommended_steps=["inspect_pool_usage: check", "fix_connection_leak: leaks"]),
    ])
    assert d.bottleneck == B.CONNECTION_POOL_SATURATION
    assert d.extras["action_ids"] == ["inspect_pool_usage", "fix_connection_leak"]
    assert any("did not match the required JSON" in t for t in d.reasoning_trace)


def test_turn_limit_forces_final_answer(views):
    loop = [tools_msg(call("get_database_metrics", i=i), call("list_queries", i=i)) for i in range(3)]
    d, _ = run(views["A"], [*loop, answer()], max_iterations=3)
    assert d.extras["iterations"] == 3
    assert any("Turn limit" in t for t in d.reasoning_trace)


def test_tools_are_read_only_and_bound_to_scenario(views):
    view = views["C"]
    before = fingerprint(view)
    tools = {t.name: t for t in make_tools(view)}
    assert set(tools) == {
        "get_database_metrics", "get_connection_pool_status", "list_queries",
        "inspect_query_plan", "search_database_logs", "get_index_metadata",
    }
    for t in tools.values():
        out = t.invoke({})
        assert out.get("scenario_id") == "C" or "error" in out
        assert "ground_truth" not in json.dumps(out)
    assert fingerprint(view) == before


def test_number_extraction_and_verification():
    assert extract_numbers("2,400,000 rows, 2.4M, 850 ms, 94%, Q1, p95") == [2_400_000, 2_400_000, 850, 94]
    outputs = [{"rows_examined": 2400000, "utilization": 0.94, "t": "10:03"}]
    report = verify_evidence(["2.4M rows examined", "pool at 94%", "spike at 10:03", "made up 12345"], outputs)
    assert [r["verified"] for r in report] == [True, True, True, False]


def test_identifiers_checked_against_logs(views):
    """Hyphenated and CamelCase identifiers from logs count as concrete evidence."""
    from dbdiag import tools as T

    logs = T.search_database_logs(views["B"], "", "ANY")
    report = verify_evidence(
        [
            "connection leaks in the billing-export threads",
            "SQLTransientConnectionException due to pool exhaustion",
            "FakeExceptionName in made-up-thread",
            "waits are increasing over time",  # nothing concrete to check
            "AMBIGUOUS: WARNING and ERROR levels, SLO, CPU",  # all-caps words are not identifiers
        ],
        [logs],
    )
    assert [r["verified"] for r in report] == [True, True, False, False, False]
    assert report[3]["checked_tokens"] == 0 and report[4]["checked_tokens"] == 0


def test_separators_and_prompt_values_count_as_sources():
    outputs = [{"idle_in_transaction": 25, "max_avg_wait_ms": 3900}]
    prompt = "connection-pool wait <= 50 ms"
    report = verify_evidence(
        ["idle-in-transaction sessions reached 25", "connection-pool wait is 3900 ms (SLO is 50 ms)"],
        outputs, given_text=prompt,
    )
    assert [r["verified"] for r in report] == [True, True]
    without_prompt = verify_evidence(["connection-pool wait is 3900 ms (SLO is 50 ms)"], outputs)
    assert not without_prompt[0]["verified"]


def test_ollama_unavailable_raises_clear_error(views):
    agent = LLMAgent(base_url="http://127.0.0.1:9")  # nothing listens on port 9
    with pytest.raises(LLMUnavailableError, match="ollama serve"):
        agent.decide(views["A"])


def test_runner_reports_llm_error_without_fake_decision(all_scenarios, tmp_path):
    result = run_all_agents(
        all_scenarios["A"], include_llm=True, base_url="http://127.0.0.1:9",
        epsilon=0.0, experience_path=tmp_path / "exp.json",
    )
    assert len(result.decisions) == 5
    assert "LLM (Ollama + LangChain)" in result.errors
    assert "ollama serve" in result.errors["LLM (Ollama + LangChain)"]


# --------------------------------------------------------------------------- #
@pytest.mark.ollama
@pytest.mark.parametrize("sid", ["A", "B", "C"])
def test_real_ollama(sid, views):
    d = LLMAgent().decide(views[sid])
    assert len(d.extras["distinct_tools"]) >= 2
    assert d.extras["grounding_score"] >= 0.7
