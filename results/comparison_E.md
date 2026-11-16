# Scenario E: Intermittent spikes with missing telemetry

**Category:** edge  
**Expected bottleneck:** `AMBIGUOUS`  
**Acceptable actions:** `check_locks`, `analyze_stats_in_staging`, `collect_missing_telemetry`, `explain_analyze_in_staging`  
**Fingerprint:** `22dea4039228c37a…` (identical for every agent)

p99 latency spikes now and then and p95 hovers around the SLO. CPU and memory metrics stopped arriving after two minutes. A payments query has planner estimates far off from actual rows, and a few requests hit lock timeouts. No single cause is clear.

## Comparison

| Agent | Bottleneck | Matches ground truth | Top step | Steps | Confidence | Evidence items | Mechanism detail | Verification metric |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Simple Reflex | LOCK_CONTENTION | ✘ | check_locks | check_locks → shorten_long_transactions | 0.7 | 7 | rule 4 (lock waits) on window 09:05 | Lock waits = 0 and no lock-timeout log entries for 15 minutes (now: 3 lock waits) |
| Model-Based Reflex | AMBIGUOUS | ✔ | collect_missing_telemetry | collect_missing_telemetry → check_locks → analyze_stats_in_staging | 0.28 | 9 | 2 hypothesis(es) from state over 6 windows | Telemetry complete for 3+ windows and one hypothesis clearly supported (e.g. lock waits vs. estimate error) before any optimization (now: p95 210 ms, pool waiting 1) |
| Goal-Based | AMBIGUOUS | ✔ | collect_missing_telemetry | collect_missing_telemetry → check_locks → analyze_stats_in_staging | 0.41 | 8 | plan of 3 step(s), 34 nodes expanded | Telemetry complete for 3+ windows and one hypothesis clearly supported (e.g. lock waits vs. estimate error) before any optimization (now: p95 210 ms, pool waiting 1) |
| Utility-Based | AMBIGUOUS | ✔ | analyze_stats_in_staging | analyze_stats_in_staging → check_locks → explain_analyze_in_staging | 0.41 | 8 | top EU analyze_stats_in_staging = 0.265 | Telemetry complete for 3+ windows and one hypothesis clearly supported (e.g. lock waits vs. estimate error) before any optimization (now: p95 210 ms, pool waiting 1) |
| Learning | AMBIGUOUS | ✔ | check_locks | check_locks | 0.77 | 9 | AMBIGUOUS\|check_locks rate 0.76 | Telemetry complete for 3+ windows and one hypothesis clearly supported (e.g. lock waits vs. estimate error) before any optimization (now: p95 210 ms, pool waiting 1) |
| LLM (Ollama + LangChain) | AMBIGUOUS | ✔ | inspect_pool_usage | inspect_pool_usage → analyze_stats_in_staging → explain_analyze_in_staging → review_index_proposal | 0.7 | 7 | 6 tool calls (6 distinct), grounding 1.00 | p95 latency and connection-pool wait metrics |

## Why decisions differ

5/6 agents agree on AMBIGUOUS; others: LOCK_CONTENTION (1).
Expected bottleneck: AMBIGUOUS (5/6 correct).
Why the decisions differ:
- **Simple Reflex** **(disagrees)** → `LOCK_CONTENTION`, main step `shorten_long_transactions`, confidence 0.70: rule 4 (lock waits) matched the latest window 09:05; it has no memory of earlier windows and stops at the first match.
- **Model-Based Reflex** → `AMBIGUOUS`, main step `collect_missing_telemetry`, confidence 0.28: its internal state over all 6 windows showed 2 competing explanations (LOCK_CONTENTION, STALE_STATISTICS) and CPU missing for 4 windows (last known 36.0%).
- **Goal-Based** → `AMBIGUOUS`, main step `collect_missing_telemetry`, confidence 0.41: its search found the shortest safe plan collect_missing_telemetry -> check_locks -> analyze_stats_in_staging to the goal ['locks_diagnosed', 'stats_checked', 'telemetry_complete']; no hypothesis dominated (top STALE_STATISTICS 0.41), so it planned to reduce uncertainty.
- **Utility-Based** → `AMBIGUOUS`, main step `analyze_stats_in_staging`, confidence 0.41: analyze_stats_in_staging had the highest expected utility (0.265) given P(STALE_STATISTICS)=0.41 and weights {'risk': 0.3, 'cost': 0.2, 'time': 0.1}.
- **Learning** → `AMBIGUOUS`, main step `check_locks`, confidence 0.77: check_locks has 2.2 successes / 0.0 failures (rate 0.76) for signature lock_waits+low_cpu+missing_metric+stale_stats from lock_waits+missing_metric+stale_stats (sim 0.75); next best AMBIGUOUS|collect_missing_telemetry rate 0.50. Experience changed the choice: the prior alone would pick AMBIGUOUS|collect_missing_telemetry.
- **LLM (Ollama + LangChain)** → `AMBIGUOUS`, main step `review_index_proposal`, confidence 0.70: the model read get_connection_pool_status, get_database_metrics, get_index_metadata, inspect_query_plan, list_queries, search_database_logs and concluded AMBIGUOUS; 7/7 of its evidence items match tool outputs.
Main recommended steps differ: Simple Reflex: `shorten_long_transactions`; Model-Based Reflex: `collect_missing_telemetry`; Goal-Based: `collect_missing_telemetry`; Utility-Based: `analyze_stats_in_staging`; Learning: `check_locks`; LLM (Ollama + LangChain): `review_index_proposal`. Each mechanism ranks the safe actions differently (rule playbook, shortest plan, highest expected utility, best success rate, or the model's choice).

## Evidence and steps per agent

### Simple Reflex

*Latest window 09:05: p95 210 ms vs 200 ms SLO, QPS 306, pool 29/50 (1 waiting). Symptoms: latency_slo_breach, lock_waits, missing_metrics, p99_spike, stale_statistics. Diagnosis: Sessions wait on locks held by other transactions.*

Evidence:
- p95 latency 210 ms at 09:05 exceeds the 200 ms SLO
- p99 1900 ms is 9.0x p95 at 09:05 (intermittent spikes)
- Q10 planner estimate 12 rows vs 18,500 actual (1542x off); payments last analyzed 2026-08-14
- 3 lock waits and 1 lock-wait log entries at 09:05
- Missing telemetry at 09:05: cpu_pct, mem_pct
- Log: [09:05:50 WARNING monitor] node_exporter on db-primary not responding; CPU and memory metrics unavailable
- Log: [09:05:10 LOG postgres] process 5544 still waiting for ShareLock on transaction 99871 after 1000.301 ms

Recommended steps:
1. [check_locks] Find blocking sessions: Query pg_locks joined with pg_stat_activity (read-only) to find blocking and blocked sessions, how long they wait, and which statements hold the locks.
2. [shorten_long_transactions] Shorten lock-holding transactions: Propose code changes so transactions that take row locks (SELECT ... FOR UPDATE) commit quickly, e.g. SKIP LOCKED for queue-style work, reviewed before rollout.

Verification: Lock waits = 0 and no lock-timeout log entries for 15 minutes (now: 3 lock waits)

Reasoning trace:
- Percept: latest window 09:05 only (6 windows exist; earlier ones are ignored)
- cpu_pct is missing in this window, so rule 3 cannot match
- Rule 1 (pool saturated) did not match
- Rule 2 (large full scan) did not match
- Rule 3 (CPU high) did not match
- Rule 4 (lock waits) FIRED: lock_waits 3, lock-wait log entries 1

### Model-Based Reflex

*6 windows 09:00-09:05: p95 210 ms vs 200 ms SLO, QPS 306, pool 29/50 (1 waiting). Symptoms: latency_slo_breach, lock_waits, missing_metrics, p99_spike, stale_statistics. Diagnosis: Signals conflict or telemetry is missing; more diagnosis is needed.*

Evidence:
- Over 6 windows: lock waits in 4/6 windows
- Over 6 windows: planner estimate error in 6/6 windows
- p95 latency 210 ms at 09:05 exceeds the 200 ms SLO
- p99 1900 ms is 9.0x p95 at 09:05 (intermittent spikes)
- Q10 planner estimate 12 rows vs 18,500 actual (1542x off); payments last analyzed 2026-08-14
- 3 lock waits and 1 lock-wait log entries at 09:05
- Missing telemetry at 09:05: cpu_pct, mem_pct
- Log: [09:05:50 WARNING monitor] node_exporter on db-primary not responding; CPU and memory metrics unavailable
- Log: [09:05:10 LOG postgres] process 5544 still waiting for ShareLock on transaction 99871 after 1000.301 ms

Recommended steps:
1. [collect_missing_telemetry] Restore missing telemetry: Restore the missing host metrics (e.g. the node exporter) and collect a few more windows before committing to a diagnosis.
2. [check_locks] Find blocking sessions: Query pg_locks joined with pg_stat_activity (read-only) to find blocking and blocked sessions, how long they wait, and which statements hold the locks.
3. [analyze_stats_in_staging] Refresh statistics in staging and compare plans: On a staging copy, refresh table statistics with ANALYZE and compare the planner's estimated rows and the chosen plan before and after. Table: payments (last analyzed 2026-08-14).

Verification: Telemetry complete for 3+ windows and one hypothesis clearly supported (e.g. lock waits vs. estimate error) before any optimization (now: p95 210 ms, pool waiting 1)

Reasoning trace:
- update_state(09:00): symptoms=['p99_spike', 'stale_statistics']
- update_state(09:01): symptoms=['error_logs', 'lock_waits', 'p99_spike', 'stale_statistics']
- update_state(09:02): symptoms=['missing_metrics', 'p99_spike', 'stale_statistics']
- update_state(09:03): symptoms=['error_logs', 'latency_slo_breach', 'lock_waits', 'missing_metrics', 'p99_spike', 'stale_statistics']
- update_state(09:04): symptoms=['lock_waits', 'missing_metrics', 'p99_spike', 'stale_statistics']
- update_state(09:05): symptoms=['latency_slo_breach', 'lock_waits', 'missing_metrics', 'p99_spike', 'stale_statistics']
- State: QPS x1.02, corr(p95,QPS)=-0.20, corr(p95,CPU)=0.00, max CPU=36.0, latency breach from window 3, pool saturated from window None, telemetry missing in 4/6
- World model supports LOCK_CONTENTION (strength 0.67): lock waits in 4/6 windows
- World model supports STALE_STATISTICS (strength 1.00): planner estimate error in 6/6 windows
- CPU missing for the last 4 windows; using last known value 36.0% (stale) and lowering confidence by 0.17
- Decision: AMBIGUOUS because 2 competing explanations (LOCK_CONTENTION, STALE_STATISTICS) and CPU missing for 4 windows (last known 36.0%)

### Goal-Based

*6 windows: p95 210 ms vs 200 ms SLO, QPS 306, pool 29/50 (1 waiting). Symptoms: latency_slo_breach, lock_waits, missing_metrics, p99_spike, stale_statistics. Diagnosis: Signals conflict or telemetry is missing; more diagnosis is needed.*

Evidence:
- Evidence for STALE_STATISTICS: +0.80 planner estimates far from actual rows
- p95 latency 210 ms at 09:05 exceeds the 200 ms SLO
- p99 1900 ms is 9.0x p95 at 09:05 (intermittent spikes)
- Q10 planner estimate 12 rows vs 18,500 actual (1542x off); payments last analyzed 2026-08-14
- 3 lock waits and 1 lock-wait log entries at 09:05
- Missing telemetry at 09:05: cpu_pct, mem_pct
- Log: [09:05:50 WARNING monitor] node_exporter on db-primary not responding; CPU and memory metrics unavailable
- Log: [09:05:10 LOG postgres] process 5544 still waiting for ShareLock on transaction 99871 after 1000.301 ms

Recommended steps:
1. [collect_missing_telemetry] Restore missing telemetry: Restore the missing host metrics (e.g. the node exporter) and collect a few more windows before committing to a diagnosis.
2. [check_locks] Find blocking sessions: Query pg_locks joined with pg_stat_activity (read-only) to find blocking and blocked sessions, how long they wait, and which statements hold the locks.
3. [analyze_stats_in_staging] Refresh statistics in staging and compare plans: On a staging copy, refresh table statistics with ANALYZE and compare the planner's estimated rows and the chosen plan before and after. Table: payments (last analyzed 2026-08-14).

Verification: Telemetry complete for 3+ windows and one hypothesis clearly supported (e.g. lock waits vs. estimate error) before any optimization (now: p95 210 ms, pool waiting 1)

Reasoning trace:
- Belief: STALE_STATISTICS=0.41, LOCK_CONTENTION=0.34, AMBIGUOUS=0.25
- No hypothesis dominates (top STALE_STATISTICS P=0.41); a fix goal cannot be set yet, so the goal is to reduce uncertainty
- Start state: {} (nothing diagnosed yet); goal facts: ['locks_diagnosed', 'stats_checked', 'telemetry_complete']
- BFS expanded 34 nodes (max frontier 67, depth <= 5)
- Step 1: collect_missing_telemetry (pre -) -> state ['telemetry_complete']
- Step 2: check_locks (pre -) -> state ['locks_diagnosed', 'telemetry_complete']
- Step 3: analyze_stats_in_staging (pre -) -> state ['locks_diagnosed', 'stats_checked', 'telemetry_complete']
- Goal test passed: goal facts hold and every step is a safe recommendation
- Fix goal not reachable yet: the plan only gathers evidence; re-plan once one hypothesis dominates (currently STALE_STATISTICS at 0.41)

### Utility-Based

*6 windows: p95 210 ms vs 200 ms SLO, QPS 306, pool 29/50 (1 waiting). Symptoms: latency_slo_breach, lock_waits, missing_metrics, p99_spike, stale_statistics. Diagnosis: Signals conflict or telemetry is missing; more diagnosis is needed.*

Evidence:
- Evidence for STALE_STATISTICS: +0.80 planner estimates far from actual rows
- p95 latency 210 ms at 09:05 exceeds the 200 ms SLO
- p99 1900 ms is 9.0x p95 at 09:05 (intermittent spikes)
- Q10 planner estimate 12 rows vs 18,500 actual (1542x off); payments last analyzed 2026-08-14
- 3 lock waits and 1 lock-wait log entries at 09:05
- Missing telemetry at 09:05: cpu_pct, mem_pct
- Log: [09:05:50 WARNING monitor] node_exporter on db-primary not responding; CPU and memory metrics unavailable
- Log: [09:05:10 LOG postgres] process 5544 still waiting for ShareLock on transaction 99871 after 1000.301 ms

Recommended steps:
1. [analyze_stats_in_staging] Refresh statistics in staging and compare plans: On a staging copy, refresh table statistics with ANALYZE and compare the planner's estimated rows and the chosen plan before and after. Table: payments (last analyzed 2026-08-14).
2. [check_locks] Find blocking sessions: Query pg_locks joined with pg_stat_activity (read-only) to find blocking and blocked sessions, how long they wait, and which statements hold the locks.
3. [explain_analyze_in_staging] Confirm the plan with EXPLAIN ANALYZE in staging: Run EXPLAIN (ANALYZE, BUFFERS) for the slow query on a staging replica and confirm where the time goes (scan type, rows examined vs returned, buffer reads). Target: Q10 (Index Scan on payments).

Verification: Telemetry complete for 3+ windows and one hypothesis clearly supported (e.g. lock waits vs. estimate error) before any optimization (now: p95 210 ms, pool waiting 1)

Reasoning trace:
- P(h | evidence): STALE_STATISTICS=0.41, LOCK_CONTENTION=0.34, AMBIGUOUS=0.25
- Weights: {'risk': 0.3, 'cost': 0.2, 'time': 0.1}
- Top EU: analyze_stats_in_staging=0.265, schedule_stats_refresh=0.251, check_locks=0.226, explain_analyze_in_staging=0.153
- Best action: analyze_stats_in_staging (EU 0.265 = benefit 0.320 - 0.3*0.05 - 0.2*0.15 - 0.1*0.1)
- Follow-up (next best EU): ['check_locks', 'explain_analyze_in_staging']
- No hypothesis dominates (top STALE_STATISTICS P=0.41): diagnosis reported as AMBIGUOUS

### Learning

*Signature lock_waits+low_cpu+missing_metric+stale_stats: p95 210 ms vs 200 ms SLO, QPS 306, pool 29/50 (1 waiting). Symptoms: latency_slo_breach, lock_waits, missing_metrics, p99_spike, stale_statistics. Diagnosis: Signals conflict or telemetry is missing; more diagnosis is needed.*

Evidence:
- Symptom signature: lock_waits+low_cpu+missing_metric+stale_stats
- Experience: check_locks has 2.2 successes / 0.0 failures (rate 0.76) for signature lock_waits+low_cpu+missing_metric+stale_stats from lock_waits+missing_metric+stale_stats (sim 0.75); next best AMBIGUOUS|collect_missing_telemetry rate 0.50. Experience changed the choice: the prior alone would pick AMBIGUOUS|collect_missing_telemetry
- p95 latency 210 ms at 09:05 exceeds the 200 ms SLO
- p99 1900 ms is 9.0x p95 at 09:05 (intermittent spikes)
- Q10 planner estimate 12 rows vs 18,500 actual (1542x off); payments last analyzed 2026-08-14
- 3 lock waits and 1 lock-wait log entries at 09:05
- Missing telemetry at 09:05: cpu_pct, mem_pct
- Log: [09:05:50 WARNING monitor] node_exporter on db-primary not responding; CPU and memory metrics unavailable
- Log: [09:05:10 LOG postgres] process 5544 still waiting for ShareLock on transaction 99871 after 1000.301 ms

Recommended steps:
1. [check_locks] Find blocking sessions: Query pg_locks joined with pg_stat_activity (read-only) to find blocking and blocked sessions, how long they wait, and which statements hold the locks.

Verification: Telemetry complete for 3+ windows and one hypothesis clearly supported (e.g. lock waits vs. estimate error) before any optimization (now: p95 210 ms, pool waiting 1)

Reasoning trace:
- Signature: lock_waits+low_cpu+missing_metric+stale_stats
- Evidence match: LOCK_CONTENTION=0.60, STALE_STATISTICS=0.60, AMBIGUOUS=1.00
-   AMBIGUOUS|check_locks: match 1.00 x rate 0.76 (2.2 successes / 0.0 failures) = 0.765
-   AMBIGUOUS|collect_missing_telemetry: match 1.00 x rate 0.50 (0.8 successes / 0.8 failures) = 0.500
-   AMBIGUOUS|analyze_stats_in_staging: match 1.00 x rate 0.50 (0.0 successes / 0.0 failures) = 0.500
-   AMBIGUOUS|explain_analyze_in_staging: match 1.00 x rate 0.50 (0.0 successes / 0.0 failures) = 0.500
- check_locks has 2.2 successes / 0.0 failures (rate 0.76) for signature lock_waits+low_cpu+missing_metric+stale_stats from lock_waits+missing_metric+stale_stats (sim 0.75); next best AMBIGUOUS|collect_missing_telemetry rate 0.50. Experience changed the choice: the prior alone would pick AMBIGUOUS|collect_missing_telemetry

### LLM (Ollama + LangChain)

*The database is experiencing slightly exceeded SLO for p95 latency and connection-pool wait, along with lock waits and missing telemetry. Additionally, there is a potential issue with the query plan for Q11 and the index metadata for the payment_batches table.*

Evidence:
- p95 latency reached 210 ms in window 09:01 and 09:05, breaching the 200 ms SLO.
- Connection-pool wait reached 35 ms in window 09:03, breaching the 50 ms SLO.
- Lock waits occurred in windows 09:01 and 09:03, with a lock timeout error in window 09:01.
- Missing telemetry for CPU and memory metrics in windows 09:02 and 09:05.
- Query Q11 has a high mean latency of 450 ms, with an estimate error ratio of 2.0.
- The index payment_batches_status_idx is used for Q11, but the rows examined per row returned is 3.0, indicating potential inefficiency.
- The payment_batches table has 52,000 rows, and the index payment_batches_status_idx has 210,000 scans.

Recommended steps:
1. [inspect_pool_usage] Inspect pool usage and idle-in-transaction sessions: Check pool active/idle/waiting counts over time and list idle-in-transaction sessions in pg_stat_activity to find connections that are held but not used. Model's reason: Inspect pool usage and idle-in-transaction sessions
2. [analyze_stats_in_staging] Refresh statistics in staging and compare plans: On a staging copy, refresh table statistics with ANALYZE and compare the planner's estimated rows and the chosen plan before and after. Model's reason: Refresh statistics in staging and compare plans
3. [explain_analyze_in_staging] Confirm the plan with EXPLAIN ANALYZE in staging: Run EXPLAIN (ANALYZE, BUFFERS) for the slow query on a staging replica and confirm where the time goes (scan type, rows examined vs returned, buffer reads).
4. [review_index_proposal] Propose an index for DBA review: Draft a CREATE INDEX CONCURRENTLY proposal on the filtered column(s) for DBA review, test it in staging first, and do not execute it in production from this tool. Model's reason: Propose an index for DBA review

Verification: p95 latency and connection-pool wait metrics

Reasoning trace:
- Model qwen3:8b, 6 read-only tools, max 8 turns
- Turn 1: answered after 0 distinct tool(s), missing 3 evidence group(s); re-prompt 1/2
- Turn 2: get_database_metrics({"window": "all"}) -> 2.65 ms
- Turn 2: get_connection_pool_status({"window": "all"}) -> 4.61 ms
- Turn 3: list_queries({}) -> 2.41 ms
- Turn 4: search_database_logs({"level": "WARNING", "pattern": ".*"}) -> 1.3 ms
- Turn 5: answered after 4 distinct tool(s), missing 1 evidence group(s); re-prompt 2/2
- Turn 6: inspect_query_plan({"query_id": "Q11"}) -> 4.43 ms
- Turn 7: get_index_metadata({"table": "payment_batches"}) -> 2.24 ms
- Grounding: 7/7 evidence items verified against tool outputs
- Diagnostics first: inserted explain_analyze_in_staging before review_index_proposal
- Confidence: model 0.70 x grounding factor 1.00 = 0.70
