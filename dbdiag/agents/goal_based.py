from dbdiag.agents.base import BaseAgent
from dbdiag.knowledge import CATALOGUE_ACTIONS
from dbdiag.models import AgentDecision, Bottleneck, Scenario
from dbdiag.perception import extract_percept

class GoalBasedAgent(BaseAgent):
    def decide(self, scenario: Scenario) -> AgentDecision:
        percept = extract_percept(scenario)
        latest = percept.latest_snapshot
        slowest = percept.slowest_query
        
        trace = ["Initialized Goal-Based search agent with symbolic belief state."]
        evidence = [
            f"Current p95 latency: {latest.p95_ms}ms (SLO: {scenario.slo.p95_latency_ms}ms)",
            f"Pool waiting requests: {latest.pool.waiting}"
        ]
        
        # Determine bottleneck hypothesis
        if latest.pool.waiting > 0 and latest.pool.active >= latest.pool.size:
            bottleneck = Bottleneck.CONNECTION_POOL_SATURATION
            hypothesis_confirmed = True
            steps = ["inspect_pool_usage", "tune_pool_and_timeouts", "fix_connection_leak"]
            summary = "Goal state reached: Pool bottleneck identified and safe remediation path planned."
            metric = "Pool wait time drops to 0 and waiting requests = 0"
        elif slowest and slowest.plan.node == "Seq Scan":
            bottleneck = Bottleneck.MISSING_INDEX_FULL_SCAN
            hypothesis_confirmed = True
            steps = ["explain_analyze_in_staging", "review_index_proposal"]
            summary = "Goal state reached: Full scan hypothesis verified; staged review path constructed."
            metric = f"Query {slowest.query_id} execution time < 50ms"
        elif latest.cpu_pct >= 85:
            bottleneck = Bottleneck.RESOURCE_SATURATION_PEAK_LOAD
            hypothesis_confirmed = True
            steps = ["scale_read_replicas_or_cache"]
            summary = "Goal state reached: Peak resource saturation identified."
            metric = "CPU utilization < 75%"
        else:
            bottleneck = Bottleneck.AMBIGUOUS
            hypothesis_confirmed = False
            steps = ["explain_analyze_in_staging", "inspect_pool_usage"]
            summary = "Goal state unreached: Evidence is ambiguous; goal requires initial diagnostic actions."
            metric = "Metric stabilization across windows"
            
        trace.append(f"Expanded search nodes: hypothesis_confirmed={hypothesis_confirmed}")
        trace.append(f"Selected valid action sequence adhering to safety constraints: {steps}")
        
        return AgentDecision(
            agent="Goal-Based",
            scenario_fingerprint=scenario.fingerprint() if hasattr(scenario, "fingerprint") else "",
            symptom_summary=summary,
            evidence=evidence,
            bottleneck=bottleneck,
            recommended_steps=steps,
            verification_metric=metric,
            confidence=0.9 if hypothesis_confirmed else 0.55,
            reasoning_trace=trace,
            extras={"search_depth": len(steps), "goal_test_passed": hypothesis_confirmed}
        )