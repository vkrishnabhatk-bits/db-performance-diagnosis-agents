from dbdiag.agents.base import BaseAgent
from dbdiag.models import AgentDecision, Bottleneck, Scenario
from dbdiag.perception import extract_percept

class ModelBasedReflexAgent(BaseAgent):
    def decide(self, scenario: Scenario) -> AgentDecision:
        percept = extract_percept(scenario)
        snapshots = scenario.snapshots
        
        trace = [f"Analyzed time series of {len(snapshots)} snapshots."]
        evidence = []
        
        # Track trends across windows
        qps_trend = [s.qps for s in snapshots]
        cpu_trend = [s.cpu_pct for s in snapshots]
        p95_trend = [s.p95_ms for s in snapshots]
        waiting_trend = [s.pool.waiting for s in snapshots]
        
        is_qps_rising = all(qps_trend[i] <= qps_trend[i+1] for i in range(len(qps_trend)-1)) and qps_trend[-1] > qps_trend[0] * 1.5
        persistent_waiting = sum(1 for w in waiting_trend if w > 0) >= 2
        
        evidence.append(f"QPS trend: {qps_trend}")
        evidence.append(f"CPU trend: {cpu_trend}")
        evidence.append(f"Persistent pool waiting windows count: {sum(1 for w in waiting_trend if w > 0)}")
        
        slowest_query = percept.slowest_query
        
        # Decision logic leveraging internal state / trend modeling
        if persistent_waiting:
            bottleneck = Bottleneck.CONNECTION_POOL_SATURATION
            summary = "Connection pool waiting state has persisted across multiple consecutive monitoring windows."
            steps = ["inspect_pool_usage", "tune_pool_and_timeouts", "fix_connection_leak"]
            metric = "Pool waiting count returns to 0 across all windows"
            trace.append("State model detected persistent pool waiting over time.")
            confidence = 0.92
        elif is_qps_rising and all(c >= 80 for c in cpu_trend[-2:]):
            bottleneck = Bottleneck.RESOURCE_SATURATION_PEAK_LOAD
            summary = "QPS and CPU utilization are rising concurrently across windows, indicating peak load saturation."
            steps = ["scale_read_replicas_or_cache"]
            metric = "CPU utilization stabilizes below 75% under high QPS"
            trace.append("State model detected correlation between rising QPS, CPU, and latency.")
            confidence = 0.88
        elif slowest_query and slowest_query.plan.node == "Seq Scan":
            bottleneck = Bottleneck.MISSING_INDEX_FULL_SCAN
            summary = f"Query {slowest_query.query_id} consistently performs full table scans with high latency."
            steps = ["explain_analyze_in_staging", "review_index_proposal"]
            metric = f"Query {slowest_query.query_id} execution latency drops below SLO threshold"
            trace.append("State model confirmed query plan bottleneck independent of traffic spikes.")
            confidence = 0.9
        else:
            bottleneck = Bottleneck.AMBIGUOUS
            summary = "Trend analysis shows mixed signals or insufficient persistence to isolate a single root cause."
            steps = ["explain_analyze_in_staging", "inspect_pool_usage"]
            metric = "Metric stabilization across windows"
            trace.append("State model fell back to ambiguous due to conflicting or flat trends.")
            confidence = 0.6

        return AgentDecision(
            agent="Model-Based Reflex",
            scenario_fingerprint=scenario.fingerprint() if hasattr(scenario, "fingerprint") else "",
            symptom_summary=summary,
            evidence=evidence,
            bottleneck=bottleneck,
            recommended_steps=steps,
            verification_metric=metric,
            confidence=confidence,
            reasoning_trace=trace,
            extras={"snapshots_processed": len(snapshots)}
        )