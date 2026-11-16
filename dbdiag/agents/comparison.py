import pandas as pd
from typing import List
from dbdiag.models import AgentDecision, Scenario

def build_comparison_table(decisions: List[AgentDecision], scenario: Scenario) -> pd.DataFrame:
    """
    Builds a structured comparison dataframe across all agent decisions.
    """
    data = []
    for d in decisions:
        top_step = d.recommended_steps[0] if d.recommended_steps else "None"
        data.append({
            "Agent": d.agent,
            "Bottleneck": d.bottleneck.value if hasattr(d.bottleneck, "value") else str(d.bottleneck),
            "Top Recommended Step": top_step,
            "Verification Metric": d.verification_metric,
            "Confidence": round(d.confidence, 2),
            "Evidence Count": len(d.evidence),
            "Trace Steps": len(d.reasoning_trace)
        })
    return pd.DataFrame(data)

def explain_differences(decisions: List[AgentDecision]) -> str:
    """
    Generates an automated explanation of why agent decisions differ based on their underlying mechanisms.
    """
    bottlenecks = set(d.bottleneck for d in decisions)
    if len(bottlenecks) == 1:
        return "All active agents achieved complete consensus on the root bottleneck and recommended action sequence, reflecting clear telemetry signals."
    
    explanation = "Agents diverged in their conclusions due to fundamental differences in their internal architectures:\n\n"
    for d in decisions:
        explanation += f"- **{d.agent}** concluded `{d.bottleneck}` (Confidence: {d.confidence:.2f}) because its mechanism relied on {get_mechanism_description(d.agent)}.\n"
    
    return explanation

def get_mechanism_description(agent_name: str) -> "str":
    if "Simple Reflex" in agent_name:
        return "latest snapshot rule evaluation without historical context."
    elif "Model-Based" in agent_name:
        return "time-series trend analysis and condition persistence tracking."
    elif "Goal-Based" in agent_name:
        return "STRIPS-style belief state search over action preconditions and effects."
    elif "Utility-Based" in agent_name:
        return "expected utility maximization balancing benefit, risk, and cost."
    elif "Learning" in agent_name:
        return "symptom signature matching against historical success/failure rates."
    elif "LLM" in agent_name:
        return "tool-calling telemetry interrogation and textual prompt grounding."
    return "its specific decision heuristic."