import json
import os
from pathlib import Path
from dbdiag.agents.base import BaseAgent
from dbdiag.models import AgentDecision, Bottleneck, Scenario
from dbdiag.perception import extract_percept

class LearningAgent(BaseAgent):
    def __init__(self, experience_path: str = "data/experience.json"):
        self.experience_path = Path(experience_path)
        self._load_experience()

    def _load_experience(self):
        if self.experience_path.exists():
            try:
                with open(self.experience_path, "r") as f:
                    self.memory = json.load(f)
            except Exception:
                self.memory = {}
        else:
            self.memory = {}

    def _get_success_rate(self, signature: str, action: str) -> float:
        sig_data = self.memory.get(signature, {})
        action_data = sig_data.get(action, {"success": 0, "failure": 0})
        successes = action_data.get("success", 0)
        failures = action_data.get("failure", 0)
        # Laplace smoothing (Beta(1,1) prior)
        return (successes + 1) / (successes + failures + 2)

    def decide(self, scenario: Scenario) -> AgentDecision:
        percept = extract_percept(scenario)
        latest = percept.latest_snapshot
        slowest = percept.slowest_query
        
        # Build symptom signature
        sig_parts = []
        if latest.pool.waiting > 0:
            sig_parts.append("pool_waiting")
        if slowest and slowest.plan.node == "Seq Scan":
            sig_parts.append("seq_scan")
        if latest.cpu_pct >= 85:
            sig_parts.append("high_cpu")
        signature = "+".join(sig_parts) if sig_parts else "healthy_or_ambiguous"
        
        trace = [f"Built symptom signature: '{signature}' from current percept."]
        evidence = [f"Signature matched against experience store: {self.experience_path}"]
        
        # Score candidate actions using historical success rates
        if "pool_waiting" in signature:
            bottleneck = Bottleneck.CONNECTION_POOL_SATURATION
            actions = ["tune_pool_and_timeouts", "inspect_pool_usage", "fix_connection_leak"]
            steps = ["inspect_pool_usage", "tune_pool_and_timeouts", "fix_connection_leak"]
            summary = "Learning agent identified pool saturation based on historical signature matching."
            metric = "Pool wait time drops to 0"
        elif "seq_scan" in signature:
            bottleneck = Bottleneck.MISSING_INDEX_FULL_SCAN
            actions = ["review_index_proposal", "explain_analyze_in_staging"]
            steps = ["explain_analyze_in_staging", "review_index_proposal"]
            summary = "Learning agent recognized full-scan pattern from past successful resolutions."
            metric = f"Query {slowest.query_id} execution latency < 50ms"
        elif "high_cpu" in signature:
            bottleneck = Bottleneck.RESOURCE_SATURATION_PEAK_LOAD
            actions = ["scale_read_replicas_or_cache"]
            steps = ["scale_read_replicas_or_cache"]
            summary = "Learning agent matched peak load CPU signature."
            metric = "CPU utilization < 70%"
        else:
            bottleneck = Bottleneck.AMBIGUOUS
            actions = ["explain_analyze_in_staging", "inspect_pool_usage"]
            steps = ["explain_analyze_in_staging", "inspect_pool_usage"]
            summary = "Learning agent encountered unmapped signature; recommending general diagnostics."
            metric = "Metric stabilization"

        # Select best action based on Laplace-smoothed success rate
        best_action = max(actions, key=lambda a: self._get_success_rate(signature, a))
        rate = self._get_success_rate(signature, best_action)
        
        trace.append(f"Evaluated success rates for signature '{signature}'. Selected '{best_action}' with success rate {rate:.2f}.")
        evidence.append(f"Historical success rate for {best_action}: {rate:.2f}")

        return AgentDecision(
            agent="Learning Agent",
            scenario_fingerprint=scenario.fingerprint() if hasattr(scenario, "fingerprint") else "",
            symptom_summary=summary,
            evidence=evidence,
            bottleneck=bottleneck,
            recommended_steps=steps,
            verification_metric=metric,
            confidence=min(0.5 + rate * 0.4, 0.95),
            reasoning_trace=trace,
            extras={"signature": signature, "historical_success_rate": rate, "memory_size": len(self.memory)}
        )