"""Compare the agents' decisions on one scenario.

- ``comparison_table``   one row per agent
- ``agreement_summary``  e.g. "5/6 agents agree on CONNECTION_POOL_SATURATION"
- ``explain_differences`` why decisions differ, in terms of each mechanism
- ``to_markdown``        dependency-free Markdown rendering of a DataFrame

Ground truth is used here only to grade decisions that have already been made.
"""

from __future__ import annotations

from collections import Counter

import pandas as pd

from dbdiag.models import AgentDecision, Bottleneck, GroundTruth


def top_action(decision: AgentDecision) -> str:
    ids = decision.extras.get("action_ids") or []
    return ids[0] if ids else decision.recommended_steps[0][:60]


def main_action(decision: AgentDecision) -> str:
    """The step that addresses the cause: the first optimization, else the first step."""
    from dbdiag.knowledge import ACTIONS

    ids = decision.extras.get("action_ids") or []
    for a in ids:
        if a in ACTIONS and ACTIONS[a].kind != "diagnostic":
            return a
    return ids[0] if ids else top_action(decision)


def matches(decision: AgentDecision, ground_truth: GroundTruth | None) -> bool | None:
    if ground_truth is None:
        return None
    return decision.bottleneck == ground_truth.bottleneck


def actions_acceptable(decision: AgentDecision, ground_truth: GroundTruth | None) -> bool | None:
    if ground_truth is None:
        return None
    ids = decision.extras.get("action_ids") or []
    return bool(ids) and any(a in ground_truth.acceptable_actions for a in ids)


def _mark(value: bool | None) -> str:
    return "–" if value is None else ("✔" if value else "✘")


def comparison_table(decisions: list[AgentDecision], ground_truth: GroundTruth | None = None) -> pd.DataFrame:
    rows = []
    for d in decisions:
        rows.append({
            "Agent": d.agent,
            "Bottleneck": d.bottleneck.value,
            "Matches ground truth": _mark(matches(d, ground_truth)),
            "Top step": top_action(d),
            "Steps": " → ".join(d.extras.get("action_ids") or []),
            "Confidence": round(d.confidence, 2),
            "Evidence items": len(d.evidence),
            "Mechanism detail": d.extras.get("mechanism_detail", ""),
            "Verification metric": d.verification_metric,
        })
    return pd.DataFrame(rows)


def agreement_summary(decisions: list[AgentDecision]) -> str:
    if not decisions:
        return "No decisions."
    counts = Counter(d.bottleneck for d in decisions)
    (top, n), = counts.most_common(1)
    text = f"{n}/{len(decisions)} agents agree on {top.value}"
    others = [f"{b.value} ({c})" for b, c in counts.most_common()[1:]]
    if others:
        text += "; others: " + ", ".join(others)
    return text + "."


def explain_differences(decisions: list[AgentDecision], ground_truth: GroundTruth | None = None) -> str:
    """Explain each agent's conclusion by its mechanism, highlighting disagreements."""
    if not decisions:
        return "No decisions to compare."
    counts = Counter(d.bottleneck for d in decisions)
    majority: Bottleneck = counts.most_common(1)[0][0]
    lines = [agreement_summary(decisions)]
    if ground_truth is not None:
        right = [d.agent for d in decisions if d.bottleneck == ground_truth.bottleneck]
        lines.append(f"Expected bottleneck: {ground_truth.bottleneck.value} ({len(right)}/{len(decisions)} correct).")

    dissent = [d for d in decisions if d.bottleneck != majority]
    if not dissent:
        lines.append("All agents reached the same bottleneck; they differ only in the steps and confidence:")
    else:
        lines.append("Why the decisions differ:")
    for d in decisions:
        flag = "" if d.bottleneck == majority else " **(disagrees)**"
        lines.append(
            f"- **{d.agent}**{flag} → `{d.bottleneck.value}`, main step `{main_action(d)}`, "
            f"confidence {d.confidence:.2f}: {d.extras.get('why', d.extras.get('mechanism', ''))}."
        )

    steps = {d.agent: main_action(d) for d in decisions}
    if len(set(steps.values())) > 1:
        lines.append(
            "Main recommended steps differ: "
            + "; ".join(f"{a}: `{s}`" for a, s in steps.items())
            + ". Each mechanism ranks the safe actions differently (rule playbook, shortest plan, "
            "highest expected utility, best success rate, or the model's choice)."
        )
    return "\n".join(lines)


def to_markdown(df: pd.DataFrame) -> str:
    cols = list(df.columns)

    def cell(v) -> str:
        return str(v).replace("|", "\\|").replace("\n", " ")

    out = ["| " + " | ".join(cols) + " |", "| " + " | ".join("---" for _ in cols) + " |"]
    for _, row in df.iterrows():
        out.append("| " + " | ".join(cell(row[c]) for c in cols) + " |")
    return "\n".join(out)
