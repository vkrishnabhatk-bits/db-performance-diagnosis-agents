from dbdiag.agents.base import BaseAgent
from dbdiag.models import AgentDecision, Bottleneck, Scenario
from dbdiag.perception import extract_percept

class SimpleReflexAgent(BaseAgent):
    def decide(self, scenario: Scenario) -> AgentDecision:
        percept = extract_percept(scenario)
        latest = percept.latest_snapshot
        slowest_query = percept.slowest_query
        
        trace = [f"Evaluated latest snapshot at t={latest.t}"]
        evidence = [
            f"QPS: {latest.qps}, p95 latency: {latest.p95_ms}ms",
            f"Pool active/size: {latest.pool.active}/{latest.pool.size}, waiting: {latest.pool.waiting}"
        ]
        
        if slowest_query:
            evidence.append(f"Slowest query {slowest_query.query_id}: {slowest_query.mean_ms}ms, plan: {slowest_query.plan.node}")

        # Rule 1: Pool saturation
        if latest.pool.waiting > 0 and latest.pool.active >= latest.pool.size:
            bottleneck = Bottleneck.CONNECTION_POOL_SATURATION
            summary = "Connection pool is fully saturated with requests waiting."
            steps = ["inspect_pool_usage", "tune_pool_and_timeouts", "fix_connection_leak"]
            metric = "Pool average wait time < 10ms and 0 waiting requests"
            trace.append("Fired rule: pool.waiting > 0 and pool.active >= pool.size")
            confidence = 0.9
            
        # Rule 2: Missing index / Full scan
        elif slowest_query and slowest_query.plan.node == "Seq Scan" and ( slowest_query.plan.rows_examined / max(slowest_query.plan.rows_returned, 1) > 10000 ):
            bottleneck = Bottleneck.MISSING_INDEX_FULL_SCAN
            summary = f"Query {slowest_query.query_id} performing full table scan examining {slowest_query.plan.rows_examined} rows."
            steps = ["explain_analyze_in_staging", "review_index_proposal"]
            metric = f"Query {slowest_query.query_id} mean latency < 50ms"
            trace.append("Fired rule: Seq Scan with high rows examined vs returned ratio")
            confidence = 0.85
            
        # Rule 3: Resource saturation (CPU high)
        elif latest.cpu_pct >= 85:
            bottleneck = Bottleneck.RESOURCE_SATURATION_PEAK_LOAD
            summary = f"CPU utilization is critically high at {latest.cpu_pct}%."
            steps = ["scale_read_replicas_or_cache", "inspect_pool_usage"]
            metric = "CPU utilization < 70% during peak load"
            trace.append("Fired rule: cpu_pct >= 85")
            confidence = 0.8
            
        # Rule 4: Healthy
        elif latest.p95_ms <= scenario.slo.p95_latency_ms and latest.pool.avg_wait_ms <= scenario.slo.pool_wait_ms:
            bottleneck = Bottleneck.HEALTHY
            summary = "All performance metrics are well within acceptable SLO bounds."
            steps = ["continue_monitoring"]
            metric = "Maintain current p95 latency and pool wait times"
            trace.append("Fired rule: All metrics within SLO")
            confidence = 0.95
            
        # Rule 5: Ambiguous / Fallback
        else:
            bottleneck = Bottleneck.AMBIGUOUS
            summary = "Metrics show minor anomalies but do not match strict single-cause rules."
            steps = ["explain_analyze_in_staging", "inspect_pool_usage"]
            metric = "Stability across subsequent windows"
            trace.append("Fired fallback rule due to unclassified metrics")
            confidence = 0.5

        return AgentDecision(
            agent="Simple Reflex",
            scenario_fingerprint=scenario.fingerprint() if hasattr(scenario, "fingerprint") else "",
            symptom_summary=summary,
            evidence=evidence,
            bottleneck=bottleneck,
            recommended_steps=steps,
            verification_metric=metric,
            confidence=confidence,
            reasoning_trace=trace,
            extras={"rules_evaluated": len(trace)}
        )