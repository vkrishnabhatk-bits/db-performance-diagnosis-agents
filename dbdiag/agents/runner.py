from typing import List
from dbdiag.models import Scenario, AgentDecision
from dbdiag.scenarios import fingerprint
from dbdiag.agents.simple_reflex import SimpleReflexAgent
from dbdiag.agents.model_based import ModelBasedReflexAgent
from dbdiag.agents.goal_based import GoalBasedAgent
from dbdiag.agents.utility_based import UtilityBasedAgent
from dbdiag.agents.learning_agent import LearningAgent
from dbdiag.agents.llm_agent import LLMAgent

def run_all_agents(scenario: Scenario, include_llm: bool = True, model_name: str = "qwen3:8b") -> List[AgentDecision]:
    """
    Runs all 6 agents on the given scenario using a shared fingerprint and frozen state.
    """
    # Ensure scenario is frozen / fingerprinted
    scen_fp = fingerprint(scenario)
    
    agents = [
        SimpleReflexAgent(),
        ModelBasedReflexAgent(),
        GoalBasedAgent(),
        UtilityBasedAgent(),
        LearningAgent(),
    ]
    
    if include_llm:
        agents.append(LLMAgent(model_name=model_name))
        
    decisions = []
    for agent in agents:
        try:
            decision = agent.decide(scenario)
            # Ensure fingerprint is correctly populated
            decision.scenario_fingerprint = scen_fp
            decisions.append(decision)
        except Exception as e:
            # Fallback decision container if an agent errors out
            decisions.append(AgentDecision(
                agent=agent.__class__.__name__,
                scenario_fingerprint=scen_fp,
                symptom_summary=f"Execution error: {str(e)}",
                evidence=[],
                bottleneck="AMBIGUOUS",
                recommended_steps=["explain_analyze_in_staging"],
                verification_metric="Resolve agent runtime exception",
                confidence=0.0,
                reasoning_trace=[f"Error encountered: {str(e)}"],
                extras={"error": str(e)}
            ))
            
    return decisions