"""Streamlit UI: Database Performance Diagnosis Assistant.

    streamlit run app.py
"""

from __future__ import annotations

import json

import pandas as pd
import streamlit as st

from dbdiag.agents.learning_agent import EXPERIENCE_PATH, LearningAgent
from dbdiag.agents.llm_agent import DEFAULT_BASE_URL, DEFAULT_MODEL, LLMAgent
from dbdiag.agents.utility_based import DEFAULT_WEIGHTS
from dbdiag.comparison import agreement_summary, comparison_table, explain_differences
from dbdiag.runner import run_all_agents
from dbdiag.scenarios import fingerprint, load_all_scenarios

st.set_page_config(page_title="DB Performance Diagnosis", layout="wide")

CATEGORY_BADGE = {"normal": "🟢 normal", "critical": "🔴 critical", "edge": "🟠 edge"}
SCENARIOS = load_all_scenarios()

# --------------------------------------------------------------------------- #
# Sidebar
# --------------------------------------------------------------------------- #
with st.sidebar:
    st.title("DB Diagnosis")
    sid = st.selectbox(
        "Scenario",
        list(SCENARIOS),
        format_func=lambda s: f"{s} · {SCENARIOS[s].title} ({CATEGORY_BADGE[SCENARIOS[s].category]})",
    )
    include_llm = st.toggle("Include LLM agent", value=True)
    model_name = st.text_input("Ollama model", value=DEFAULT_MODEL, disabled=not include_llm)
    base_url = st.text_input("Ollama URL", value=DEFAULT_BASE_URL, disabled=not include_llm)
    think = st.toggle("Qwen3 thinking mode (slower)", value=False, disabled=not include_llm)
    with st.expander("Advanced"):
        epsilon = st.slider("Learning ε (exploration)", 0.0, 0.5, 0.1, 0.05)
        st.caption("Utility weights: EU = Σ P(h)·benefit − w_risk·risk − w_cost·cost − w_time·time")
        weights = {
            "risk": st.slider("w_risk", 0.0, 1.0, DEFAULT_WEIGHTS["risk"], 0.05),
            "cost": st.slider("w_cost", 0.0, 1.0, DEFAULT_WEIGHTS["cost"], 0.05),
            "time": st.slider("w_time", 0.0, 1.0, DEFAULT_WEIGHTS["time"], 0.05),
        }
    run = st.button("Run all agents", type="primary", width="stretch")

scenario = SCENARIOS[sid]
settings = (sid, include_llm, model_name, base_url, think, epsilon, tuple(sorted(weights.items())))
cache: dict = st.session_state.setdefault("results", {})

if run:
    with st.spinner("Running agents" + (" (the LLM can take a minute)" if include_llm else "") + "…"):
        cache[settings] = run_all_agents(
            scenario,
            include_llm=include_llm,
            model_name=model_name,
            base_url=base_url,
            think=think,
            epsilon=epsilon,
            utility_weights=weights,
            experience_path=EXPERIENCE_PATH,
        )
result = cache.get(settings)

st.header(f"Scenario {scenario.id}: {scenario.title}")
st.caption(f"{CATEGORY_BADGE[scenario.category]} · fingerprint `{fingerprint(scenario)[:16]}…`")

tab_scenario, tab_decisions, tab_compare, tab_learning = st.tabs(
    ["Scenario", "Agent decisions", "Comparison", "Learning"]
)


# --------------------------------------------------------------------------- #
# Tab 1: scenario
# --------------------------------------------------------------------------- #
with tab_scenario:
    st.write(scenario.description)
    st.caption(f"SLO: p95 ≤ {scenario.slo.p95_latency_ms:.0f} ms, pool wait ≤ {scenario.slo.pool_wait_ms:.0f} ms")
    snaps = pd.DataFrame(
        [
            {
                "t": s.t, "p95 latency (ms)": s.p95_ms, "QPS": s.qps, "CPU (%)": s.cpu_pct,
                "Pool waiting": s.pool.waiting, "p50 (ms)": s.p50_ms, "p99 (ms)": s.p99_ms,
                "Pool active": s.pool.active, "Pool size": s.pool.size, "Pool avg wait (ms)": s.pool.avg_wait_ms,
                "Timeouts": s.pool.timeouts, "DB conn": f"{s.db_connections.active}/{s.db_connections.max}",
                "Lock waits": s.lock_waits, "Idle in txn": s.idle_in_transaction,
            }
            for s in scenario.snapshots
        ]
    ).set_index("t")
    # One metric per chart (no dual axes); each chart is a single series.
    c1, c2 = st.columns(2)
    for col, metric in zip([c1, c2, c1, c2], ["p95 latency (ms)", "QPS", "CPU (%)", "Pool waiting"]):
        with col:
            st.markdown(f"**{metric}**")
            st.line_chart(snaps[[metric]], height=180)
    if snaps["CPU (%)"].isna().any():
        st.warning("CPU telemetry is missing in some windows (gaps in the CPU chart).")
    with st.expander("All metrics (table)"):
        st.dataframe(snaps, width="stretch")

    st.subheader("Queries and plans")
    st.dataframe(
        pd.DataFrame([
            {"query": q.query_id, "sql": q.sql, "calls/min": q.calls_per_min, "mean ms": q.mean_ms,
             "plan": q.plan.node, "table": q.plan.table, "rows examined": q.plan.rows_examined,
             "rows returned": q.plan.rows_returned, "est rows": q.plan.est_rows, "index": q.plan.index_used}
            for q in scenario.queries
        ]),
        width="stretch", hide_index=True,
    )
    i1, i2 = st.columns(2)
    with i1:
        st.subheader("Indexes")
        st.dataframe(
            pd.DataFrame([{"table": i.table, "name": i.name, "columns": ", ".join(i.columns),
                           "size MB": i.size_mb, "scans": i.scans} for i in scenario.indexes]),
            width="stretch", hide_index=True,
        )
    with i2:
        st.subheader("Tables")
        st.dataframe(
            pd.DataFrame([{"table": t.name, "rows": t.rows, "last analyzed": t.last_analyzed} for t in scenario.tables]),
            width="stretch", hide_index=True,
        )
    st.subheader("Logs")
    st.dataframe(
        pd.DataFrame([e.model_dump() for e in scenario.logs]), width="stretch", hide_index=True
    )


# --------------------------------------------------------------------------- #
# Tab 2: decisions
# --------------------------------------------------------------------------- #
def render_decision(d) -> None:
    st.markdown(f"**Symptom summary.** {d.symptom_summary}")
    left, right = st.columns([3, 2])
    with left:
        st.markdown("**Evidence**")
        grounding = {g["item"]: g for g in d.extras.get("grounding", [])}
        for e in d.evidence:
            g = grounding.get(e)
            mark = "" if g is None else ("✅ " if g["verified"] else "⚠️ ")
            note = "" if g is None or g["verified"] else f"  _(unverified: {', '.join(g['unverified_tokens']) or 'no concrete value'})_"
            st.markdown(f"- {mark}{e}{note}")
        st.markdown("**Recommended steps** (recommendations only, nothing is executed)")
        for i, s in enumerate(d.recommended_steps, 1):
            st.markdown(f"{i}. {s}")
        st.markdown(f"**Verification metric.** {d.verification_metric}")
    with right:
        st.metric("Likely bottleneck", d.bottleneck.value)
        st.progress(d.confidence, text=f"Confidence {d.confidence:.2f}")
        st.caption(d.extras.get("mechanism", ""))

    x = d.extras
    if "plan" in x:
        st.markdown("**Goal-based plan**: " + " → ".join(x["plan"]) + f"  (goal {x['goal']}, {x['nodes_expanded']} nodes expanded)")
    if "utility_table" in x:
        st.markdown("**Utility table** (sorted by EU)")
        st.dataframe(pd.DataFrame(x["utility_table"]), width="stretch", hide_index=True)
    if "ranking" in x:
        st.markdown(f"**Learning ranking** for signature `{x['signature']}`")
        st.dataframe(pd.DataFrame(x["ranking"]), width="stretch", hide_index=True)
    if "tool_calls" in x:
        st.markdown(
            f"**Tool-call trace** ({len(x['tool_calls'])} calls, distinct: {', '.join(x['distinct_tools'])}; "
            f"grounding score {x['grounding_score']:.2f}; grounded={x['grounded']})"
        )
        for i, c in enumerate(x["tool_calls"], 1):
            with st.expander(f"{i}. {c['name']}({json.dumps(c['args'])}) · {c['latency_ms']} ms"):
                st.json(c["result"])
        if x.get("unsafe_removed"):
            st.warning("Removed unsafe recommendations: " + "; ".join(x["unsafe_removed"]))
    if "state" in x:
        with st.expander("Model-based internal state"):
            st.json(x["state"])
    with st.expander("Reasoning trace"):
        for t in d.reasoning_trace:
            st.markdown(f"- {t}")


with tab_decisions:
    if result is None:
        st.info("Choose a scenario and press **Run all agents**.")
    else:
        fps = {d.scenario_fingerprint for d in result.decisions}
        if fps == {result.fingerprint}:
            st.success(f"Fairness check: all {len(result.decisions)} decisions carry fingerprint `{result.fingerprint[:16]}…`")
        else:
            st.error("Fingerprint mismatch between agents!")
        for name, err in result.errors.items():
            st.error(f"**{name}** did not run: {err}")
        for d in result.decisions:
            with st.expander(f"{d.agent} → {d.bottleneck.value} ({d.confidence:.2f})", expanded=False):
                render_decision(d)


# --------------------------------------------------------------------------- #
# Tab 3: comparison
# --------------------------------------------------------------------------- #
with tab_compare:
    if result is None or not result.decisions:
        st.info("Run the agents first.")
    else:
        st.subheader(agreement_summary(result.decisions))
        st.dataframe(comparison_table(result.decisions, scenario.ground_truth), width="stretch", hide_index=True)
        st.caption("Ground truth is used only here, to grade decisions after they are made.")
        st.markdown(explain_differences(result.decisions, scenario.ground_truth))


# --------------------------------------------------------------------------- #
# Tab 4: learning
# --------------------------------------------------------------------------- #
with tab_learning:
    learner = LearningAgent(experience_path=EXPERIENCE_PATH, epsilon=0.0)
    decision = result.decision(LearningAgent.name) if result else None

    if decision is not None:
        st.markdown(
            f"Last decision on **{scenario.id}**: `{decision.extras['option']}` "
            f"(signature `{decision.extras['signature']}`, rate {decision.extras['success_rate']:.2f})"
        )
        b1, b2, b3 = st.columns(3)
        update = None
        if b1.button("👍 It worked"):
            update = learner.feedback(decision, success=True)
        if b2.button("👎 It did not work"):
            update = learner.feedback(decision, success=False)
        if b3.button("Simulated critic (ground truth)"):
            update = learner.critique_and_learn(decision, scenario.ground_truth)
        if update:
            st.session_state["last_update"] = update
            cache.pop(settings, None)  # re-run to see the effect of the new counts
        last = st.session_state.get("last_update")
        if last:
            st.success(
                f"Recorded **{last['outcome']}** for `{last['option']}` ({last['source']}). "
                f"Before: {last['before']} → after: {last['after']}. Press **Run all agents** to see the effect."
            )
    else:
        st.info("Run the agents to give feedback on the learning agent's decision.")

    rows = [
        {"signature": sig, "option": opt, "success": c["success"], "failure": c["failure"],
         "success rate": round((c["success"] + 1) / (c["success"] + c["failure"] + 2), 3)}
        for sig, opts in learner.experience["counts"].items() for opt, c in opts.items()
    ]
    st.subheader("Experience (success / failure counts)")
    if rows:
        df = pd.DataFrame(rows).sort_values(["signature", "success rate"], ascending=[True, False])
        st.dataframe(df, width="stretch", hide_index=True)
        if decision is not None:
            mine = df[df["signature"] == decision.extras["signature"]]
            if not mine.empty:
                st.markdown(f"**Success rate per option for `{decision.extras['signature']}`**")
                st.bar_chart(mine.set_index("option")[["success rate"]], height=220, horizontal=True)
    if st.button("Reset experience to seed"):
        learner.reset()
        st.session_state.pop("last_update", None)
        st.rerun()
    st.subheader("History")
    history = learner.experience["history"]
    if history:
        st.dataframe(pd.DataFrame(history[::-1]), width="stretch", hide_index=True)
    else:
        st.caption("No feedback recorded yet.")
