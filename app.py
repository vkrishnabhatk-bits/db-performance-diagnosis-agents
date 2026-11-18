"""Streamlit UI: Database Performance Diagnosis Assistant.

    streamlit run app.py

Set ``DBDIAG_EXPERIENCE`` to use a different learning-agent experience file
(the tests point it at a temporary file).
"""

from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

from dbdiag.agents.learning_agent import EXPERIENCE_PATH, LearningAgent
from dbdiag.agents.llm_agent import DEFAULT_BASE_URL, DEFAULT_MODEL, LLMAgent
from dbdiag.agents.utility_based import DEFAULT_WEIGHTS
from dbdiag.comparison import (
    actions_acceptable,
    agreement_summary,
    comparison_table,
    explain_differences,
    main_action,
    matches,
)
from dbdiag.knowledge import ACTIONS, BOTTLENECK_DESCRIPTIONS
from dbdiag.runner import AGENT_ORDER, run_all_agents
from dbdiag.scenarios import fingerprint, load_all_scenarios

st.set_page_config(page_title="DB Performance Diagnosis", page_icon="🩺", layout="wide")

EXPERIENCE = Path(os.environ.get("DBDIAG_EXPERIENCE", EXPERIENCE_PATH))
CATEGORY_BADGE = {"normal": "🟢 normal", "critical": "🔴 critical", "edge": "🟠 edge"}
LEVEL_ICON = {"FATAL": "🔴", "ERROR": "🔴", "WARNING": "🟠", "LOG": "⚪", "INFO": "⚪", "DEBUG": "⚪"}
AGENT_ICON = {
    "Simple Reflex": "⚡",
    "Model-Based Reflex": "🧠",
    "Goal-Based": "🎯",
    "Utility-Based": "⚖️",
    "Learning": "📈",
    LLMAgent.name: "🤖",
}
# One series per chart in a single hue; the SLO reference line uses the status "critical" red.
SERIES = "#2a78d6"
SLO_RED = "#d03b3b"
SCENARIOS = load_all_scenarios()


@st.cache_data(ttl=15, show_spinner=False)
def ollama_status(base_url: str) -> tuple[bool, list[str], str]:
    """(reachable, pulled model names, error) for the Ollama server at ``base_url``."""
    try:
        with urllib.request.urlopen(base_url.rstrip("/") + "/api/tags", timeout=1.5) as resp:
            models = [m["name"] for m in json.load(resp).get("models", [])]
        return True, models, ""
    except Exception as exc:  # connection refused, timeout, bad URL, ...
        return False, [], f"{type(exc).__name__}: {exc}"


def model_pulled(name: str, models: list[str]) -> bool:
    return name in models or f"{name}:latest" in models


def rate(counts: dict) -> float:
    """Laplace-smoothed success rate, as the learning agent computes it."""
    return (counts["success"] + 1) / (counts["success"] + counts["failure"] + 2)


# --------------------------------------------------------------------------- #
# Chart helpers (Altair; one measure per chart, no dual axes)
# --------------------------------------------------------------------------- #
def line_chart(df: pd.DataFrame, field: str, title: str, unit: str, slo: float | None = None) -> alt.Chart:
    base = alt.Chart(df).encode(x=alt.X("t:O", title=None, axis=alt.Axis(labelAngle=0)))
    line = base.mark_line(color=SERIES, strokeWidth=2, point=alt.OverlayMarkDef(size=64, filled=True, color=SERIES)).encode(
        y=alt.Y(f"{field}:Q", title=unit),
        tooltip=[alt.Tooltip("t:O", title="window"), alt.Tooltip(f"{field}:Q", title=title, format=",.0f")],
    )
    layers = [line]
    if slo is not None:
        ref = alt.Chart(pd.DataFrame({"slo": [slo]}))
        layers.append(ref.mark_rule(color=SLO_RED, strokeDash=[5, 4], strokeWidth=1.5).encode(y="slo:Q"))
        layers.append(
            ref.mark_text(align="left", baseline="bottom", dx=4, dy=-3, fontSize=11).encode(
                y="slo:Q", x=alt.value(0), text=alt.value(f"SLO {slo:,.0f} {unit}")
            )
        )
    return alt.layer(*layers).properties(title=title, height=190)


def bar_chart(df: pd.DataFrame, category: str, value: str, fmt: str = ".2f", domain: tuple | None = None) -> alt.Chart:
    x = alt.X(f"{value}:Q", title=None, scale=alt.Scale(domain=domain) if domain else alt.Undefined)
    bars = alt.Chart(df).mark_bar(color=SERIES, cornerRadiusEnd=4, size=16).encode(
        y=alt.Y(f"{category}:N", sort="-x", title=None, axis=alt.Axis(labelLimit=320)),
        x=x,
        tooltip=[alt.Tooltip(f"{category}:N"), alt.Tooltip(f"{value}:Q", format=fmt)],
    )
    labels = bars.mark_text(align="left", dx=4, fontSize=11).encode(text=alt.Text(f"{value}:Q", format=fmt))
    return (bars + labels).properties(height=alt.Step(28))


def snapshots_frame(scenario) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "t": s.t, "qps": s.qps, "p50_ms": s.p50_ms, "p95_ms": s.p95_ms, "p99_ms": s.p99_ms,
                "cpu_pct": s.cpu_pct, "mem_pct": s.mem_pct, "io_wait_pct": s.io_wait_pct,
                "cache_hit_ratio": s.cache_hit_ratio, "pool_active": s.pool.active, "pool_size": s.pool.size,
                "pool_waiting": s.pool.waiting, "pool_avg_wait_ms": s.pool.avg_wait_ms,
                "pool_timeouts": s.pool.timeouts, "db_conn_active": s.db_connections.active,
                "db_conn_max": s.db_connections.max, "lock_waits": s.lock_waits,
                "idle_in_txn": s.idle_in_transaction,
            }
            for s in scenario.snapshots
        ]
    )


# --------------------------------------------------------------------------- #
# Sidebar
# --------------------------------------------------------------------------- #
with st.sidebar:
    st.title("🩺 DB Diagnosis")
    st.caption("Six agents diagnose the same simulated telemetry. Recommendations only; nothing is executed.")
    sid = st.radio(
        "Scenario",
        list(SCENARIOS),
        format_func=lambda s: f"{s} · {SCENARIOS[s].title}  {CATEGORY_BADGE[SCENARIOS[s].category]}",
    )

    st.subheader("LLM agent")
    model_name = st.text_input("Ollama model", value=DEFAULT_MODEL)
    base_url = st.text_input("Ollama URL", value=DEFAULT_BASE_URL)
    reachable, pulled, ollama_error = ollama_status(base_url)
    llm_ready = reachable and model_pulled(model_name, pulled)
    if llm_ready:
        st.success(f"Ollama is running and `{model_name}` is pulled.", icon="✅")
    elif reachable:
        st.warning(f"Ollama is running but `{model_name}` is not pulled. Run `ollama pull {model_name}`.", icon="⚠️")
    else:
        st.warning(
            f"Ollama is not reachable at {base_url}. Run `ollama serve` and `ollama pull {model_name}`. "
            "The five classical agents still run.",
            icon="⚠️",
        )
    include_llm = st.toggle("Include LLM agent", value=llm_ready)
    think = st.toggle("Qwen3 thinking mode (slower)", value=False, disabled=not include_llm)

    with st.expander("Advanced"):
        epsilon = st.slider("Learning ε (exploration)", 0.0, 0.5, 0.1, 0.05)
        st.caption("EU = Σ P(h)·benefit − w_risk·risk − w_cost·cost − w_time·time")
        weights = {
            "risk": st.slider("w_risk", 0.0, 1.0, DEFAULT_WEIGHTS["risk"], 0.05),
            "cost": st.slider("w_cost", 0.0, 1.0, DEFAULT_WEIGHTS["cost"], 0.05),
            "time": st.slider("w_time", 0.0, 1.0, DEFAULT_WEIGHTS["time"], 0.05),
        }
    run = st.button("▶ Run all agents", type="primary", width="stretch")

scenario = SCENARIOS[sid]
settings = (sid, include_llm, model_name, base_url, think, epsilon, tuple(sorted(weights.items())))
cache: dict = st.session_state.setdefault("results", {})

if run:
    label = "Running agents" + (" (the LLM can take a minute)" if include_llm else "") + "…"
    with st.spinner(label):
        cache[settings] = run_all_agents(
            scenario,
            include_llm=include_llm,
            model_name=model_name,
            base_url=base_url,
            think=think,
            epsilon=epsilon,
            utility_weights=weights,
            experience_path=EXPERIENCE,
        )
result = cache.get(settings)


# --------------------------------------------------------------------------- #
# Header: scenario and its current health against the SLO
# --------------------------------------------------------------------------- #
st.title(f"Scenario {scenario.id}: {scenario.title}")
st.caption(f"{CATEGORY_BADGE[scenario.category]} · fingerprint `{fingerprint(scenario.agent_view())[:16]}…`")
st.write(scenario.description)

latest, first = scenario.latest, scenario.snapshots[0]
slo = scenario.slo
breached = latest.p95_ms > slo.p95_latency_ms or latest.pool.avg_wait_ms > slo.pool_wait_ms
st.markdown(
    f":red-badge[✘ SLO breached in window {latest.t}]" if breached else f":green-badge[✔ SLO met in window {latest.t}]"
)
k1, k2, k3, k4, k5 = st.columns(5)
k1.metric(
    "p95 latency", f"{latest.p95_ms:,.0f} ms",
    f"{latest.p95_ms - slo.p95_latency_ms:+,.0f} ms vs SLO {slo.p95_latency_ms:,.0f}", delta_color="inverse", border=True,
)
k2.metric(
    "Pool wait (avg)", f"{latest.pool.avg_wait_ms:,.0f} ms",
    f"{latest.pool.avg_wait_ms - slo.pool_wait_ms:+,.0f} ms vs SLO {slo.pool_wait_ms:,.0f}", delta_color="inverse", border=True,
)
k3.metric(
    "QPS", f"{latest.qps:,.0f}",
    f"{(latest.qps / first.qps - 1) * 100:+.0f}% since {first.t}" if first.qps else None, delta_color="off", border=True,
)
k4.metric("DB CPU", "missing" if latest.cpu_pct is None else f"{latest.cpu_pct:.0f}%", border=True)
k5.metric(
    "App pool (active/size)", f"{latest.pool.active}/{latest.pool.size}",
    f"{latest.pool.waiting} waiting" if latest.pool.waiting else None, delta_color="inverse", border=True,
)

tab_scenario, tab_decisions, tab_compare, tab_learning = st.tabs(
    ["📊 Scenario", "🧩 Agent decisions", "⚖️ Comparison", "📈 Learning"]
)


# --------------------------------------------------------------------------- #
# Tab 1: scenario telemetry
# --------------------------------------------------------------------------- #
with tab_scenario:
    snaps = snapshots_frame(scenario)
    c1, c2 = st.columns(2)
    c1.altair_chart(line_chart(snaps, "p95_ms", "p95 latency", "ms", slo.p95_latency_ms), width="stretch")
    c2.altair_chart(line_chart(snaps, "qps", "Queries per second", "QPS"), width="stretch")
    c1.altair_chart(line_chart(snaps, "cpu_pct", "Database CPU", "%"), width="stretch")
    c2.altair_chart(line_chart(snaps, "pool_waiting", "Requests waiting for a pool connection", "requests"), width="stretch")
    missing = [m for m in ("cpu_pct", "mem_pct", "io_wait_pct", "cache_hit_ratio") if snaps[m].isna().any()]
    if missing:
        st.warning(f"Telemetry is missing in some windows for: {', '.join(missing)} (gaps in the charts).", icon="⚠️")
    with st.expander("All metrics per window (table)"):
        st.dataframe(snaps.set_index("t"), width="stretch")

    st.subheader("Queries and plans")
    st.dataframe(
        pd.DataFrame([
            {
                "query": q.query_id, "sql": q.sql, "calls/min": q.calls_per_min, "mean ms": q.mean_ms,
                "plan": ("⚠️ " if q.plan.is_full_scan else "") + q.plan.node, "table": q.plan.table,
                "rows examined": q.plan.rows_examined, "rows returned": q.plan.rows_returned,
                "examined/returned": round(q.plan.rows_examined / max(q.plan.rows_returned, 1)),
                "est rows": q.plan.est_rows, "index": q.plan.index_used or "—",
            }
            for q in scenario.queries
        ]),
        width="stretch", hide_index=True,
        column_config={"sql": st.column_config.TextColumn(width="large")},
    )
    i1, i2 = st.columns(2)
    with i1:
        st.subheader("Indexes")
        st.dataframe(
            pd.DataFrame([{"table": i.table, "name": i.name, "columns": ", ".join(i.columns), "unique": i.unique,
                           "size MB": i.size_mb, "scans": i.scans} for i in scenario.indexes]),
            width="stretch", hide_index=True,
        )
    with i2:
        st.subheader("Tables")
        st.dataframe(
            pd.DataFrame([{"table": t.name, "rows": t.rows, "last analyzed": t.last_analyzed or "never"}
                          for t in scenario.tables]),
            width="stretch", hide_index=True,
        )

    st.subheader("Database and application logs")
    levels = sorted({e.level for e in scenario.logs}, key=list(LEVEL_ICON).index)
    shown = st.pills("Levels", levels, selection_mode="multi", default=levels, label_visibility="collapsed")
    logs = [e for e in scenario.logs if e.level in (shown or [])]
    if logs:
        st.dataframe(
            pd.DataFrame([{"ts": e.ts, "level": f"{LEVEL_ICON[e.level]} {e.level}", "source": e.source,
                           "message": e.message} for e in logs]),
            width="stretch", hide_index=True,
            column_config={"message": st.column_config.TextColumn(width="large")},
        )
    else:
        st.caption("No log lines for the selected levels.")


# --------------------------------------------------------------------------- #
# Tab 2: decisions
# --------------------------------------------------------------------------- #
def action_label(action_id: str) -> str:
    a = ACTIONS.get(action_id)
    return f"`{action_id}`" if a is None else f"`{action_id}` · {a.title} ({a.kind})"


def render_decision(d) -> None:
    st.markdown(f"**Symptom summary.** {d.symptom_summary}")
    left, right = st.columns([3, 2], gap="large")
    with left:
        st.markdown("**Evidence**")
        grounding = {g["item"]: g for g in d.extras.get("grounding", [])}
        for e in d.evidence:
            g = grounding.get(e)
            mark = "" if g is None else ("✅ " if g["verified"] else "⚠️ ")
            note = ""
            if g is not None and not g["verified"]:
                note = f"  _(unverified: {', '.join(g['unverified_tokens']) or 'no concrete value'})_"
            st.markdown(f"- {mark}{e}{note}")
        st.markdown("**Recommended steps** · diagnostics first; recommendations only, nothing is executed")
        for i, s in enumerate(d.recommended_steps, 1):
            st.markdown(f"{i}. {s}")
        st.markdown(f"**Verification metric.** {d.verification_metric}")
    with right:
        with st.container(border=True):
            st.markdown("**Likely bottleneck**")
            st.markdown(f"### `{d.bottleneck.value}`")
            st.caption(BOTTLENECK_DESCRIPTIONS.get(d.bottleneck, ""))
            st.progress(d.confidence, text=f"Confidence {d.confidence:.2f}")
        st.caption(f"**Mechanism.** {d.extras.get('mechanism', '')}")

    x = d.extras
    if "plan" in x:
        st.markdown(
            f"**Goal-based plan** · goal `{', '.join(x['goal'])}` · {x['nodes_expanded']} nodes expanded · "
            f"goal reached: {'yes' if x.get('goal_reached') else 'no'}"
        )
        for i, a in enumerate(x["plan"], 1):
            st.markdown(f"{i}. {action_label(a)}")
        if x.get("fix_goal_reachable") is False:
            st.info("A fix cannot be planned yet: the plan only reduces uncertainty.", icon="ℹ️")
    probs = x.get("probabilities") or x.get("belief")
    if probs:
        st.markdown("**Belief over bottlenecks** P(h | evidence)")
        df = pd.DataFrame([{"bottleneck": k, "P": v} for k, v in probs.items() if v > 0])
        st.altair_chart(bar_chart(df, "bottleneck", "P", domain=(0, 1)), width="stretch")
    if "utility_table" in x:
        st.markdown(f"**Utility table** (sorted by EU) · weights {x.get('weights', {})}")
        st.dataframe(pd.DataFrame(x["utility_table"]), width="stretch", hide_index=True)
    if "ranking" in x:
        st.markdown(
            f"**Learning ranking** for signature `{x['signature']}`"
            + (" · 🎲 explored the second-best option (ε)" if x.get("explored") else "")
        )
        st.dataframe(pd.DataFrame(x["ranking"]), width="stretch", hide_index=True)
    if "tool_calls" in x:
        st.markdown(
            f"**Tool-call trace** · {len(x['tool_calls'])} calls · distinct: {', '.join(x['distinct_tools'])} · "
            f"grounding score {x['grounding_score']:.2f} · grounded={x['grounded']}"
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


def render_error(name: str, err: str) -> None:
    if name == LLMAgent.name and "LLMUnavailableError" in err:
        st.warning(
            f"**{name}** did not run: Ollama is not available. No answer was invented for it.\n\n"
            f"Start it with `ollama serve`, then `ollama pull {model_name}`, and press **Run all agents** again.",
            icon="🤖",
        )
        with st.expander("Error details"):
            st.code(err, language=None)
    else:
        st.error(f"**{name}** did not run: {err}")


with tab_decisions:
    if result is None:
        st.info("Choose a scenario and press **▶ Run all agents** in the sidebar.", icon="👈")
    else:
        fps = {d.scenario_fingerprint for d in result.decisions}
        if fps <= {result.fingerprint}:
            st.success(
                f"Fairness check: all {len(result.decisions)} decisions were made on the same scenario "
                f"(fingerprint `{result.fingerprint[:16]}…`, ground truth hidden).",
                icon="⚖️",
            )
        else:
            st.error("Fingerprint mismatch between agents!")
        for name, err in result.errors.items():
            render_error(name, err)

        cols = st.columns(3)
        for i, d in enumerate(result.decisions):
            with cols[i % 3].container(border=True):
                st.markdown(f"**{AGENT_ICON.get(d.agent, '')} {d.agent}**")
                st.markdown(f"`{d.bottleneck.value}`")
                st.progress(d.confidence, text=f"confidence {d.confidence:.2f}")
                st.caption(f"Main step: `{main_action(d)}`")

        if result.decisions:
            st.subheader("Details per agent")
            agent_tabs = st.tabs([f"{AGENT_ICON.get(d.agent, '')} {d.agent}" for d in result.decisions])
            for agent_tab, d in zip(agent_tabs, result.decisions):
                with agent_tab:
                    render_decision(d)


# --------------------------------------------------------------------------- #
# Tab 3: comparison
# --------------------------------------------------------------------------- #
def scoreboard(experience: Path) -> pd.DataFrame:
    """Classical agents on every scenario (ε = 0, no learning), graded against ground truth."""
    runs = [(s, run_all_agents(s, include_llm=False, epsilon=0.0, experience_path=experience)) for s in SCENARIOS.values()]
    rows = []
    for agent in AGENT_ORDER:
        if agent == LLMAgent.name:
            continue
        row, ok_b, ok_a = {"Agent": agent}, 0, 0
        for s, r in runs:
            d = r.decision(agent)
            if d is None:
                row[s.id] = "not run"
                continue
            m, a = matches(d, s.ground_truth), actions_acceptable(d, s.ground_truth)
            ok_b, ok_a = ok_b + bool(m), ok_a + bool(a)
            row[s.id] = f"{'✔' if m else '✘'} {d.bottleneck.value} ({d.confidence:.2f})"
        row["Bottleneck correct"] = f"{ok_b}/{len(runs)}"
        row["Acceptable action"] = f"{ok_a}/{len(runs)}"
        rows.append(row)
    return pd.DataFrame(rows)


with tab_compare:
    if result is None or not result.decisions:
        st.info("Run the agents first.", icon="👈")
    else:
        decisions = result.decisions
        gt = scenario.ground_truth
        correct = sum(bool(matches(d, gt)) for d in decisions)
        m1, m2, m3 = st.columns(3)
        m1.metric("Agreement", agreement_summary(decisions).split(" agents")[0], border=True)
        m2.metric("Match expected bottleneck", f"{correct}/{len(decisions)}", border=True)
        with m3.container(border=True, height="stretch"):
            st.caption("Expected bottleneck")
            st.markdown(f"**`{gt.bottleneck.value}`**" if gt else "–")
        st.caption(agreement_summary(decisions) + " Ground truth is used only here, to grade decisions after they are made.")

        st.dataframe(
            comparison_table(decisions, gt), width="stretch", hide_index=True,
            column_config={
                "Confidence": st.column_config.ProgressColumn(min_value=0.0, max_value=1.0, format="%.2f"),
                "Verification metric": st.column_config.TextColumn(width="large"),
            },
        )
        st.markdown("#### Why the decisions differ")
        st.markdown(explain_differences(decisions, gt))

    st.divider()
    st.subheader("Scoreboard across all scenarios")
    st.caption("The five classical agents on A, B, C, N and E with ε = 0 and no learning, so the run is reproducible.")
    if st.button("Score classical agents on every scenario"):
        with st.spinner("Scoring…"):
            st.session_state["scoreboard"] = scoreboard(EXPERIENCE)
    if "scoreboard" in st.session_state:
        st.dataframe(st.session_state["scoreboard"], width="stretch", hide_index=True)


# --------------------------------------------------------------------------- #
# Tab 4: learning
# --------------------------------------------------------------------------- #
with tab_learning:
    learner = LearningAgent(experience_path=EXPERIENCE, epsilon=0.0)
    decision = result.decision(LearningAgent.name) if result else None

    if decision is not None:
        st.markdown(
            f"Last decision on **{scenario.id}**: `{decision.extras['option']}` "
            f"(signature `{decision.extras['signature']}`, success rate {decision.extras['success_rate']:.2f})"
        )
        b1, b2, b3, _ = st.columns([1, 1, 1.4, 2])
        update = None
        if b1.button("👍 It worked", width="stretch"):
            update = learner.feedback(decision, success=True)
        if b2.button("👎 It did not work", width="stretch"):
            update = learner.feedback(decision, success=False)
        if b3.button("🧪 Simulated critic (ground truth)", width="stretch"):
            update = learner.critique_and_learn(decision, scenario.ground_truth)
        if update:
            st.session_state["last_update"] = update
            st.rerun()
    else:
        st.info("Run the agents to give feedback on the learning agent's decision.", icon="👈")

    last = st.session_state.get("last_update")
    if last:
        with st.container(border=True):
            st.markdown(f"**Recorded {last['outcome']}** for `{last['option']}` (source: {last['source']})")
            if last.get("reason"):
                st.caption(f"Critic: {last['reason']}")
            b, a = last["before"], last["after"]
            s1, s2, s3 = st.columns(3)
            s1.metric("Successes", a["success"], a["success"] - b["success"] or None)
            s2.metric("Failures", a["failure"], a["failure"] - b["failure"] or None, delta_color="inverse")
            s3.metric("Success rate", f"{rate(a):.3f}", f"{rate(a) - rate(b):+.3f}")
            st.caption(
                "The decisions on screen were made with the old counts. "
                "Press **▶ Run all agents** to see how the new counts change the ranking."
            )

    rows = [
        {"signature": sig, "option": opt, "success": c["success"], "failure": c["failure"], "success rate": round(rate(c), 3)}
        for sig, opts in learner.experience["counts"].items() for opt, c in opts.items()
    ]
    st.subheader("Experience (success / failure counts)")
    st.caption("success rate = (successes + 1) / (successes + failures + 2), a Beta(1,1) prior")
    if rows:
        df = pd.DataFrame(rows).sort_values(["signature", "success rate"], ascending=[True, False])
        signature = decision.extras["signature"] if decision is not None else None
        mine = df[df["signature"] == signature] if signature else df.iloc[0:0]
        if not mine.empty:
            st.markdown(f"**Success rate per action for `{signature}`**")
            mine = mine.assign(action=mine["option"].str.split("|").str[-1])
            st.altair_chart(bar_chart(mine, "action", "success rate", domain=(0, 1)), width="stretch")
        st.dataframe(
            df, width="stretch", hide_index=True,
            column_config={"success rate": st.column_config.ProgressColumn(min_value=0.0, max_value=1.0, format="%.3f")},
        )
    if st.button("↺ Reset experience to seed"):
        learner.reset()
        st.session_state.pop("last_update", None)
        st.rerun()
    st.subheader("History")
    history = learner.experience["history"]
    if history:
        hist = pd.DataFrame(history[::-1])
        for col in ("before", "after"):
            hist[col] = hist[col].map(lambda c: f"{c['success']} ✔ / {c['failure']} ✘")
        st.dataframe(hist, width="stretch", hide_index=True)
    else:
        st.caption("No feedback recorded yet.")
