# Scenario C: Latency rises with peak traffic, CPU and connections

**Category:** edge  
**Expected bottleneck:** `RESOURCE_SATURATION_PEAK_LOAD`  
**Acceptable actions:** `profile_peak_load`, `rate_limit_peak_traffic`, `scale_read_replicas_or_cache`  
**Fingerprint:** `6fca8264a30b0fdb…` (identical for every agent)

An evening traffic peak triples request rate. Latency, database CPU and connection usage climb together; at the very end the pool also starts queueing. Query plans are fine.

## Comparison

| Agent | Bottleneck | Matches ground truth | Top step | Steps | Confidence | Evidence items | Mechanism detail | Verification metric |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Simple Reflex | CONNECTION_POOL_SATURATION | ✘ | inspect_pool_usage | inspect_pool_usage → fix_connection_leak → tune_pool_and_timeouts | 0.8 | 8 | rule 1 (pool saturated) on window 18:07 | Pool waiting = 0, avg wait < pool-wait SLO, zero acquire timeouts and idle-in-transaction < 5 for 15 minutes (now: p95 780 ms, pool waiting 35) |
| Model-Based Reflex | RESOURCE_SATURATION_PEAK_LOAD | ✔ | profile_peak_load | profile_peak_load → rate_limit_peak_traffic → scale_read_replicas_or_cache | 0.94 | 9 | 1 hypothesis(es) from state over 8 windows | At the next peak: CPU < 75%, DB connections < 80% of max, and p95 < SLO (now: CPU 93%, DB connections 94%, p95 780 ms) |
| Goal-Based | RESOURCE_SATURATION_PEAK_LOAD | ✔ | profile_peak_load | profile_peak_load → scale_read_replicas_or_cache | 0.71 | 12 | plan of 2 step(s), 8 nodes expanded | At the next peak: CPU < 75%, DB connections < 80% of max, and p95 < SLO (now: CPU 93%, DB connections 94%, p95 780 ms) |
| Utility-Based | RESOURCE_SATURATION_PEAK_LOAD | ✔ | profile_peak_load | profile_peak_load → scale_read_replicas_or_cache → rate_limit_peak_traffic | 0.71 | 12 | top EU scale_read_replicas_or_cache = 0.410 | At the next peak: CPU < 75%, DB connections < 80% of max, and p95 < SLO (now: CPU 93%, DB connections 94%, p95 780 ms) |
| Learning | RESOURCE_SATURATION_PEAK_LOAD | ✔ | profile_peak_load | profile_peak_load → rate_limit_peak_traffic | 0.71 | 10 | RESOURCE_SATURATION_PEAK_LOAD\|rate_limit_peak_traffic rate 0.71 | At the next peak: CPU < 75%, DB connections < 80% of max, and p95 < SLO (now: CPU 93%, DB connections 94%, p95 780 ms) |
| LLM (Ollama + LangChain) | CONNECTION_POOL_SATURATION | ✘ | inspect_pool_usage | inspect_pool_usage → tune_pool_and_timeouts → profile_peak_load | 0.85 | 7 | 6 tool calls (6 distinct), grounding 1.00 | Connection pool waiting time and CPU usage |

## Why decisions differ

4/6 agents agree on RESOURCE_SATURATION_PEAK_LOAD; others: CONNECTION_POOL_SATURATION (2).
Expected bottleneck: RESOURCE_SATURATION_PEAK_LOAD (4/6 correct).
Why the decisions differ:
- **Simple Reflex** **(disagrees)** → `CONNECTION_POOL_SATURATION`, main step `fix_connection_leak`, confidence 0.80: rule 1 (pool saturated) matched the latest window 18:07; it has no memory of earlier windows and stops at the first match.
- **Model-Based Reflex** → `RESOURCE_SATURATION_PEAK_LOAD`, main step `rate_limit_peak_traffic`, confidence 0.94: its internal state over all 8 windows showed latency rises with QPS (x3.1, r=0.97) and CPU (r=0.94).
- **Goal-Based** → `RESOURCE_SATURATION_PEAK_LOAD`, main step `scale_read_replicas_or_cache`, confidence 0.71: its search found the shortest safe plan profile_peak_load -> scale_read_replicas_or_cache to the goal ['load_ok'].
- **Utility-Based** → `RESOURCE_SATURATION_PEAK_LOAD`, main step `scale_read_replicas_or_cache`, confidence 0.71: scale_read_replicas_or_cache had the highest expected utility (0.410) given P(RESOURCE_SATURATION_PEAK_LOAD)=0.71 and weights {'risk': 0.3, 'cost': 0.2, 'time': 0.1}.
- **Learning** → `RESOURCE_SATURATION_PEAK_LOAD`, main step `rate_limit_peak_traffic`, confidence 0.71: rate_limit_peak_traffic has 4.0 successes / 1.0 failures (rate 0.71) for signature conn_up+cpu_up+high_latency+qps_up from conn_up+cpu_up+high_latency+qps_up (sim 1.00); next best RESOURCE_SATURATION_PEAK_LOAD|profile_peak_load rate 0.50. Experience changed the choice: the prior alone would pick RESOURCE_SATURATION_PEAK_LOAD|scale_read_replicas_or_cache.
- **LLM (Ollama + LangChain)** **(disagrees)** → `CONNECTION_POOL_SATURATION`, main step `tune_pool_and_timeouts`, confidence 0.85: the model read get_connection_pool_status, get_database_metrics, get_index_metadata, inspect_query_plan, list_queries, search_database_logs and concluded CONNECTION_POOL_SATURATION; 7/7 of its evidence items match tool outputs.
Main recommended steps differ: Simple Reflex: `fix_connection_leak`; Model-Based Reflex: `rate_limit_peak_traffic`; Goal-Based: `scale_read_replicas_or_cache`; Utility-Based: `scale_read_replicas_or_cache`; Learning: `rate_limit_peak_traffic`; LLM (Ollama + LangChain): `tune_pool_and_timeouts`. Each mechanism ranks the safe actions differently (rule playbook, shortest plan, highest expected utility, best success rate, or the model's choice).

## Evidence and steps per agent

### Simple Reflex

*Latest window 18:07: p95 780 ms vs 200 ms SLO, QPS 1250, pool 50/50 (35 waiting). Symptoms: cpu_high, db_conn_high, latency_slo_breach, pool_saturated, pool_timeouts, pool_wait_slo_breach. Diagnosis: Requests wait for a connection because the pool is exhausted.*

Evidence:
- p95 latency 780 ms at 18:07 exceeds the 200 ms SLO
- Pool 50/50 active with 35 requests waiting at 18:07
- Pool avg wait 420 ms exceeds the 50 ms SLO
- 4 connection-acquire timeouts at 18:07
- DB connections 189/200 (94%) at 18:07
- CPU 93% at 18:07
- Log: [18:07:10 WARNING app] GET /api/listings p95 latency 780 ms exceeds SLO 200 ms
- Log: [18:07:25 WARNING monitor] db-primary connections 189/200 (94.5%)

Recommended steps:
1. [inspect_pool_usage] Inspect pool usage and idle-in-transaction sessions: Check pool active/idle/waiting counts over time and list idle-in-transaction sessions in pg_stat_activity to find connections that are held but not used.
2. [fix_connection_leak] Fix connection leaks in application code: Use the pool's leak-detection stack traces to find code paths that do not return connections (missing close / context manager) and fix them in a reviewed change.
3. [tune_pool_and_timeouts] Propose pool size and timeout changes: Propose for review: a pool size sized to DB capacity, a shorter connection-acquire timeout, and an idle_in_transaction_session_timeout, rolled out in staging first.

Verification: Pool waiting = 0, avg wait < pool-wait SLO, zero acquire timeouts and idle-in-transaction < 5 for 15 minutes (now: p95 780 ms, pool waiting 35)

Reasoning trace:
- Percept: latest window 18:07 only (8 windows exist; earlier ones are ignored)
- Rule 1 (pool saturated) FIRED: pool.active 50 == size 50 and pool.waiting 35 > 0

### Model-Based Reflex

*8 windows 18:00-18:07: p95 780 ms vs 200 ms SLO, QPS 1250, pool 50/50 (35 waiting). Symptoms: cpu_high, db_conn_high, latency_slo_breach, pool_saturated, pool_timeouts, pool_wait_slo_breach. Diagnosis: Traffic peak saturates database CPU and connections.*

Evidence:
- Over 8 windows: latency rises with QPS (x3.1, r=0.97) and CPU (r=0.94)
- p95 latency 780 ms at 18:07 exceeds the 200 ms SLO
- Pool 50/50 active with 35 requests waiting at 18:07
- Pool avg wait 420 ms exceeds the 50 ms SLO
- 4 connection-acquire timeouts at 18:07
- DB connections 189/200 (94%) at 18:07
- CPU 93% at 18:07
- Log: [18:07:10 WARNING app] GET /api/listings p95 latency 780 ms exceeds SLO 200 ms
- Log: [18:07:25 WARNING monitor] db-primary connections 189/200 (94.5%)

Recommended steps:
1. [profile_peak_load] Profile the peak: correlate traffic, CPU and connections: Correlate QPS with p95 latency, CPU and connection counts per window, and list the top queries by total time from pg_stat_statements during the peak.
2. [rate_limit_peak_traffic] Shed or rate-limit non-critical traffic at peak: Propose rate limits or load shedding for non-critical endpoints during peaks, and cap autoscaled app instances so total connections stay below the DB limit.
3. [scale_read_replicas_or_cache] Move read traffic to replicas or a cache: Plan read replicas and/or an application cache for the hottest read queries so the primary's CPU is not the limit at peak (capacity change, reviewed and staged).

Verification: At the next peak: CPU < 75%, DB connections < 80% of max, and p95 < SLO (now: CPU 93%, DB connections 94%, p95 780 ms)

Reasoning trace:
- update_state(18:00): symptoms=['healthy']
- update_state(18:01): symptoms=['healthy']
- update_state(18:02): symptoms=['latency_slo_breach']
- update_state(18:03): symptoms=['latency_slo_breach']
- update_state(18:04): symptoms=['latency_slo_breach']
- update_state(18:05): symptoms=['cpu_high', 'latency_slo_breach', 'pool_saturated']
- update_state(18:06): symptoms=['cpu_high', 'db_conn_high', 'latency_slo_breach', 'pool_saturated', 'pool_timeouts', 'pool_wait_slo_breach']
- update_state(18:07): symptoms=['cpu_high', 'db_conn_high', 'latency_slo_breach', 'pool_saturated', 'pool_timeouts', 'pool_wait_slo_breach']
- State: QPS x3.12, corr(p95,QPS)=0.97, corr(p95,CPU)=0.94, max CPU=93.0, latency breach from window 2, pool saturated from window 5, telemetry missing in 0/8
- Pool saturation began at window 5, after latency breached at window 2: treated as a consequence of the load, not the cause
- World model supports RESOURCE_SATURATION_PEAK_LOAD (strength 0.97): latency rises with QPS (x3.1, r=0.97) and CPU (r=0.94)
- Decision: RESOURCE_SATURATION_PEAK_LOAD because latency rises with QPS (x3.1, r=0.97) and CPU (r=0.94)

### Goal-Based

*8 windows: p95 780 ms vs 200 ms SLO, QPS 1250, pool 50/50 (35 waiting). Symptoms: cpu_high, db_conn_high, latency_slo_breach, pool_saturated, pool_timeouts, pool_wait_slo_breach. Diagnosis: Traffic peak saturates database CPU and connections.*

Evidence:
- Evidence for RESOURCE_SATURATION_PEAK_LOAD: +0.38 CPU high in 38% of windows
- Evidence for RESOURCE_SATURATION_PEAK_LOAD: +0.25 DB connections near max
- Evidence for RESOURCE_SATURATION_PEAK_LOAD: +0.97 QPS x3.1 and p95 tracks QPS (r=0.97)
- Evidence for RESOURCE_SATURATION_PEAK_LOAD: +0.47 p95 tracks CPU (r=0.94)
- p95 latency 780 ms at 18:07 exceeds the 200 ms SLO
- Pool 50/50 active with 35 requests waiting at 18:07
- Pool avg wait 420 ms exceeds the 50 ms SLO
- 4 connection-acquire timeouts at 18:07
- DB connections 189/200 (94%) at 18:07
- CPU 93% at 18:07
- Log: [18:07:10 WARNING app] GET /api/listings p95 latency 780 ms exceeds SLO 200 ms
- Log: [18:07:25 WARNING monitor] db-primary connections 189/200 (94.5%)

Recommended steps:
1. [profile_peak_load] Profile the peak: correlate traffic, CPU and connections: Correlate QPS with p95 latency, CPU and connection counts per window, and list the top queries by total time from pg_stat_statements during the peak.
2. [scale_read_replicas_or_cache] Move read traffic to replicas or a cache: Plan read replicas and/or an application cache for the hottest read queries so the primary's CPU is not the limit at peak (capacity change, reviewed and staged).

Verification: At the next peak: CPU < 75%, DB connections < 80% of max, and p95 < SLO (now: CPU 93%, DB connections 94%, p95 780 ms)

Reasoning trace:
- Belief: RESOURCE_SATURATION_PEAK_LOAD=0.71, CONNECTION_POOL_SATURATION=0.16, HEALTHY=0.09, AMBIGUOUS=0.03
- RESOURCE_SATURATION_PEAK_LOAD dominates (P=0.71); goal = RESOURCE_SATURATION_PEAK_LOAD resolved
- Start state: {} (nothing diagnosed yet); goal facts: ['load_ok']
- BFS expanded 8 nodes (max frontier 26, depth <= 5)
- Step 1: profile_peak_load (pre -) -> state ['load_diagnosed']
- Step 2: scale_read_replicas_or_cache (pre ['load_diagnosed']) -> state ['load_diagnosed', 'load_ok']
- Goal test passed: goal facts hold and every step is a safe recommendation

### Utility-Based

*8 windows: p95 780 ms vs 200 ms SLO, QPS 1250, pool 50/50 (35 waiting). Symptoms: cpu_high, db_conn_high, latency_slo_breach, pool_saturated, pool_timeouts, pool_wait_slo_breach. Diagnosis: Traffic peak saturates database CPU and connections.*

Evidence:
- Evidence for RESOURCE_SATURATION_PEAK_LOAD: +0.38 CPU high in 38% of windows
- Evidence for RESOURCE_SATURATION_PEAK_LOAD: +0.25 DB connections near max
- Evidence for RESOURCE_SATURATION_PEAK_LOAD: +0.97 QPS x3.1 and p95 tracks QPS (r=0.97)
- Evidence for RESOURCE_SATURATION_PEAK_LOAD: +0.47 p95 tracks CPU (r=0.94)
- p95 latency 780 ms at 18:07 exceeds the 200 ms SLO
- Pool 50/50 active with 35 requests waiting at 18:07
- Pool avg wait 420 ms exceeds the 50 ms SLO
- 4 connection-acquire timeouts at 18:07
- DB connections 189/200 (94%) at 18:07
- CPU 93% at 18:07
- Log: [18:07:10 WARNING app] GET /api/listings p95 latency 780 ms exceeds SLO 200 ms
- Log: [18:07:25 WARNING monitor] db-primary connections 189/200 (94.5%)

Recommended steps:
1. [profile_peak_load] Profile the peak: correlate traffic, CPU and connections: Correlate QPS with p95 latency, CPU and connection counts per window, and list the top queries by total time from pg_stat_statements during the peak.
2. [scale_read_replicas_or_cache] Move read traffic to replicas or a cache: Plan read replicas and/or an application cache for the hottest read queries so the primary's CPU is not the limit at peak (capacity change, reviewed and staged).
3. [rate_limit_peak_traffic] Shed or rate-limit non-critical traffic at peak: Propose rate limits or load shedding for non-critical endpoints during peaks, and cap autoscaled app instances so total connections stay below the DB limit.

Verification: At the next peak: CPU < 75%, DB connections < 80% of max, and p95 < SLO (now: CPU 93%, DB connections 94%, p95 780 ms)

Reasoning trace:
- P(h | evidence): RESOURCE_SATURATION_PEAK_LOAD=0.71, CONNECTION_POOL_SATURATION=0.16, HEALTHY=0.09, AMBIGUOUS=0.03
- Weights: {'risk': 0.3, 'cost': 0.2, 'time': 0.1}
- Top EU: scale_read_replicas_or_cache=0.410, rate_limit_peak_traffic=0.398, profile_peak_load=0.247, inspect_pool_usage=0.164
- Best action: scale_read_replicas_or_cache (EU 0.410 = benefit 0.715 - 0.3*0.35 - 0.2*0.7 - 0.1*0.6)
- scale_read_replicas_or_cache needs ['profile_peak_load'] first (its preconditions)
- Follow-up (next best EU): ['rate_limit_peak_traffic']

### Learning

*Signature conn_up+cpu_up+high_latency+qps_up: p95 780 ms vs 200 ms SLO, QPS 1250, pool 50/50 (35 waiting). Symptoms: cpu_high, db_conn_high, latency_slo_breach, pool_saturated, pool_timeouts, pool_wait_slo_breach. Diagnosis: Traffic peak saturates database CPU and connections.*

Evidence:
- Symptom signature: conn_up+cpu_up+high_latency+qps_up
- Experience: rate_limit_peak_traffic has 4.0 successes / 1.0 failures (rate 0.71) for signature conn_up+cpu_up+high_latency+qps_up from conn_up+cpu_up+high_latency+qps_up (sim 1.00); next best RESOURCE_SATURATION_PEAK_LOAD|profile_peak_load rate 0.50. Experience changed the choice: the prior alone would pick RESOURCE_SATURATION_PEAK_LOAD|scale_read_replicas_or_cache
- p95 latency 780 ms at 18:07 exceeds the 200 ms SLO
- Pool 50/50 active with 35 requests waiting at 18:07
- Pool avg wait 420 ms exceeds the 50 ms SLO
- 4 connection-acquire timeouts at 18:07
- DB connections 189/200 (94%) at 18:07
- CPU 93% at 18:07
- Log: [18:07:10 WARNING app] GET /api/listings p95 latency 780 ms exceeds SLO 200 ms
- Log: [18:07:25 WARNING monitor] db-primary connections 189/200 (94.5%)

Recommended steps:
1. [profile_peak_load] Profile the peak: correlate traffic, CPU and connections: Correlate QPS with p95 latency, CPU and connection counts per window, and list the top queries by total time from pg_stat_statements during the peak.
2. [rate_limit_peak_traffic] Shed or rate-limit non-critical traffic at peak: Propose rate limits or load shedding for non-critical endpoints during peaks, and cap autoscaled app instances so total connections stay below the DB limit.

Verification: At the next peak: CPU < 75%, DB connections < 80% of max, and p95 < SLO (now: CPU 93%, DB connections 94%, p95 780 ms)

Reasoning trace:
- Signature: conn_up+cpu_up+high_latency+qps_up
- Evidence match: RESOURCE_SATURATION_PEAK_LOAD=1.00
-   RESOURCE_SATURATION_PEAK_LOAD|rate_limit_peak_traffic: match 1.00 x rate 0.71 (4.0 successes / 1.0 failures) = 0.714
-   RESOURCE_SATURATION_PEAK_LOAD|profile_peak_load: match 1.00 x rate 0.50 (0.0 successes / 0.0 failures) = 0.500
-   RESOURCE_SATURATION_PEAK_LOAD|inspect_pool_usage: match 1.00 x rate 0.50 (0.0 successes / 0.0 failures) = 0.500
-   RESOURCE_SATURATION_PEAK_LOAD|tune_pool_and_timeouts: match 1.00 x rate 0.50 (0.0 successes / 0.0 failures) = 0.500
- rate_limit_peak_traffic has 4.0 successes / 1.0 failures (rate 0.71) for signature conn_up+cpu_up+high_latency+qps_up from conn_up+cpu_up+high_latency+qps_up (sim 1.00); next best RESOURCE_SATURATION_PEAK_LOAD|profile_peak_load rate 0.50. Experience changed the choice: the prior alone would pick RESOURCE_SATURATION_PEAK_LOAD|scale_read_replicas_or_cache

### LLM (Ollama + LangChain)

*The database is experiencing high CPU usage and connection pool exhaustion, leading to increased latency and timeouts. Query Q7 is using an index scan on the 'listings' table with a high call rate and moderate latency.*

Evidence:
- Connection pool waiting time increased to 420 ms with 35 requests waiting
- CPU usage reached 93% with a load average of 16.4 on 16 vCPU
- Query Q7 has an Index Scan on the 'listings' table with high call rate and latency
- Query Q9 has an Index Only Scan on the 'listing_views' table with moderate latency
- Query Q8 has an Index Scan on the 'carts' table with low latency
- The 'listings' table has an index 'listings_category_active_score_idx' used by Query Q7, with 9,800,000 scans
- The 'listings' table has 1,800,000 rows and was last analyzed on 2026-11-10

Recommended steps:
1. [inspect_pool_usage] Inspect pool usage and idle-in-transaction sessions: Check pool active/idle/waiting counts over time and list idle-in-transaction sessions in pg_stat_activity to find connections that are held but not used. Model's reason: Check pool usage and idle-in-transaction sessions
2. [tune_pool_and_timeouts] Propose pool size and timeout changes: Propose for review: a pool size sized to DB capacity, a shorter connection-acquire timeout, and an idle_in_transaction_session_timeout, rolled out in staging first. Model's reason: Propose pool size and timeout changes
3. [profile_peak_load] Profile the peak: correlate traffic, CPU and connections: Correlate QPS with p95 latency, CPU and connection counts per window, and list the top queries by total time from pg_stat_statements during the peak. Model's reason: Profile the peak to correlate traffic, CPU and connections

Verification: Connection pool waiting time and CPU usage

Reasoning trace:
- Model qwen3:8b, 6 read-only tools, max 8 turns
- Turn 1: answered after 0 distinct tool(s), missing 3 evidence group(s); re-prompt 1/2
- Turn 2: get_database_metrics({"window": "all"}) -> 2.25 ms
- Turn 2: get_connection_pool_status({"window": "all"}) -> 0.66 ms
- Turn 3: list_queries({}) -> 2.02 ms
- Turn 3: search_database_logs({"level": "WARNING"}) -> 1.76 ms
- Turn 4: answered after 4 distinct tool(s), missing 1 evidence group(s); re-prompt 2/2
- Turn 5: inspect_query_plan({"query_id": "slowest"}) -> 3.35 ms
- Turn 5: get_index_metadata({"table": "listings"}) -> 0.85 ms
- Grounding: 7/7 evidence items verified against tool outputs
- Confidence: model 0.85 x grounding factor 1.00 = 0.85
