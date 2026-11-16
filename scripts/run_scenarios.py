import os
from pathlib import Path
from dbdiag.scenarios import list_scenarios, load_scenario
from dbdiag.runner import run_all_agents
from dbdiag.comparison import build_comparison_table, explain_differences

def main():
    results_dir = Path("results")
    results_dir.mkdir(exist_ok=True)
    
    scenario_ids = list_scenarios()
    print(f"Found {len(scenario_ids)} scenarios to evaluate: {scenario_ids}")
    
    summary_lines = ["# Automated Multi-Agent Batch Evaluation Summary\n"]
    
    for sid in scenario_ids:
        scenario = load_scenario(sid)
        print(f"Running agents for Scenario {scenario.id}: {scenario.title}...")
        
        decisions = run_all_agents(scenario, include_llm=True)
        df_comp = build_comparison_table(decisions, scenario)
        explanation = explain_differences(decisions)
        
        # Write individual scenario comparison report
        md_path = results_dir / f"comparison_{scenario.id}.md"
        md_content = f"# Scenario {scenario.id}: {scenario.title}\n\n"
        md_content += f"**Category:** {scenario.category}  \n"
        md_content += f"**Description:** {scenario.description}\n\n"
        md_content += "## Comparison Table\n\n"
        md_content += df_comp.to_markdown(index=False)
        md_content += "\n\n## Why Decisions Differ\n\n"
        md_content += explanation + "\n"
        
        with open(md_path, "w") as f:
            f.write(md_content)
            
        summary_lines.append(f"## Scenario {scenario.id} ({scenario.title})\n")
        summary_lines.append(df_comp.to_markdown(index=False))
        summary_lines.append(f"\n*{explanation}*\n\n---\n")

    summary_path = results_dir / "summary.md"
    with open(summary_path, "w") as f:
        f.writelines(summary_lines)
        
    print(f"Batch evaluation complete! Reports successfully written to {results_dir}/")

if __name__ == "__main__":
    main()