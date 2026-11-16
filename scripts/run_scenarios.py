"""Run every agent on every scenario and write the results/ folder.

    python scripts/run_scenarios.py                 # all agents (LLM skipped with a note if Ollama is down)
    python scripts/run_scenarios.py --no-llm        # classical agents only
    python scripts/run_scenarios.py --pytest        # also write results/pytest_report.txt

Writes:
    results/comparison_<ID>.md   one comparison table + "why decisions differ" per scenario
    results/summary.md           accuracy per agent and disagreement notes
    results/decisions.json       every decision, for reference
    results/pytest_report.txt    (with --pytest) output of `pytest -v`

The learning agent runs with epsilon = 0 and does not learn during this batch
run, so results are reproducible (its experience comes from the committed seed).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dbdiag.agents.learning_agent import SEED_PATH  # noqa: E402
from dbdiag.agents.llm_agent import DEFAULT_MODEL  # noqa: E402
from dbdiag.comparison import (  # noqa: E402
    actions_acceptable,
    agreement_summary,
    comparison_table,
    explain_differences,
    matches,
    to_markdown,
)
from dbdiag.runner import AGENT_ORDER, run_all_agents  # noqa: E402
from dbdiag.scenarios import load_all_scenarios  # noqa: E402

RESULTS = ROOT / "results"


def scenario_report(scenario, result) -> str:
    gt = scenario.ground_truth
    df = comparison_table(result.decisions, gt)
    lines = [
        f"# Scenario {scenario.id}: {scenario.title}",
        "",
        f"**Category:** {scenario.category}  ",
        f"**Expected bottleneck:** `{gt.bottleneck.value}`  ",
        f"**Acceptable actions:** {', '.join(f'`{a}`' for a in gt.acceptable_actions)}  ",
        f"**Fingerprint:** `{result.fingerprint[:16]}…` (identical for every agent)",
        "",
        scenario.description,
        "",
        "## Comparison",
        "",
        to_markdown(df),
        "",
        "## Why decisions differ",
        "",
        explain_differences(result.decisions, gt),
    ]
    if result.errors:
        lines += ["", "## Agents that did not run", ""]
        lines += [f"- **{name}:** {err}" for name, err in result.errors.items()]
    lines += ["", "## Evidence and steps per agent", ""]
    for d in result.decisions:
        lines += [f"### {d.agent}", "", f"*{d.symptom_summary}*", "", "Evidence:"]
        lines += [f"- {e}" for e in d.evidence]
        lines += ["", "Recommended steps:"]
        lines += [f"{i}. {s}" for i, s in enumerate(d.recommended_steps, 1)]
        lines += ["", f"Verification: {d.verification_metric}", "", "Reasoning trace:"]
        lines += [f"- {t}" for t in d.reasoning_trace]
        lines.append("")
    return "\n".join(lines)


def summary_report(runs, include_llm: bool) -> str:
    agents = [a for a in AGENT_ORDER if include_llm or "LLM" not in a]
    header = ["Agent", *[s.id for s, _ in runs], "Bottleneck correct", "Acceptable action"]
    rows = []
    for agent in agents:
        cells, ok_b, ok_a, n = [], 0, 0, 0
        for scenario, result in runs:
            d = result.decision(agent)
            if d is None:
                cells.append("not run")
                continue
            n += 1
            m = matches(d, scenario.ground_truth)
            a = actions_acceptable(d, scenario.ground_truth)
            ok_b += bool(m)
            ok_a += bool(a)
            cells.append(f"{'✔' if m else '✘'} {d.bottleneck.value} ({d.confidence:.2f})")
        rows.append([agent, *cells, f"{ok_b}/{n}" if n else "–", f"{ok_a}/{n}" if n else "–"])

    md = ["| " + " | ".join(header) + " |", "| " + " | ".join("---" for _ in header) + " |"]
    md += ["| " + " | ".join(r) + " |" for r in rows]
    lines = [
        "# Summary: all agents on all scenarios",
        "",
        "Cells show ✔/✘ against the expected bottleneck, the diagnosis and the confidence.",
        "",
        *md,
        "",
        "## Agreement and disagreements",
        "",
    ]
    for scenario, result in runs:
        lines.append(f"- **{scenario.id}** ({scenario.category}): {agreement_summary(result.decisions)}")
        for name, err in result.errors.items():
            lines.append(f"  - {name} did not run: {err.splitlines()[0][:200]}")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-llm", action="store_true", help="skip the LLM agent")
    ap.add_argument("--model", default=DEFAULT_MODEL, help=f"Ollama model (default {DEFAULT_MODEL})")
    ap.add_argument("--think", action="store_true", help="enable Qwen3 thinking mode (slower, better reasoning)")
    ap.add_argument("--pytest", action="store_true", help="also write results/pytest_report.txt")
    args = ap.parse_args()

    RESULTS.mkdir(exist_ok=True)
    include_llm = not args.no_llm
    runs, dump = [], {}
    with tempfile.TemporaryDirectory() as tmp:
        experience = Path(tmp) / "experience.json"  # fresh copy of the seed: reproducible
        for scenario in load_all_scenarios().values():
            print(f"Scenario {scenario.id}: {scenario.title}")
            result = run_all_agents(
                scenario, include_llm=include_llm, model_name=args.model, think=args.think,
                epsilon=0.0, experience_path=experience, seed_path=SEED_PATH,
            )
            for d in result.decisions:
                print(f"  {d.agent:26s} {d.bottleneck.value:30s} {d.confidence:.2f}")
            for name, err in result.errors.items():
                print(f"  {name:26s} ERROR {err.splitlines()[0][:120]}")
            (RESULTS / f"comparison_{scenario.id}.md").write_text(scenario_report(scenario, result), encoding="utf-8")
            runs.append((scenario, result))
            dump[scenario.id] = {
                "fingerprint": result.fingerprint,
                "decisions": [d.model_dump(mode="json") for d in result.decisions],
                "errors": result.errors,
            }

    (RESULTS / "summary.md").write_text(summary_report(runs, include_llm), encoding="utf-8")
    (RESULTS / "decisions.json").write_text(json.dumps(dump, indent=2, default=str), encoding="utf-8")
    print(f"Wrote {RESULTS.relative_to(ROOT)}/comparison_*.md, summary.md, decisions.json")

    if args.pytest:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-v"], cwd=ROOT, capture_output=True, text=True
        )
        report = (proc.stdout + proc.stderr).replace(str(ROOT), ".")  # no local absolute paths
        (RESULTS / "pytest_report.txt").write_text(report, encoding="utf-8")
        print(f"pytest exit code {proc.returncode}; wrote results/pytest_report.txt")
        return proc.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
