# Scenario B: Requests wait for connections: pool saturated

**Category:** critical  
**Expected bottleneck:** `CONNECTION_POOL_SATURATION`  
**Acceptable actions:** `inspect_pool_usage`, `fix_connection_leak`, `tune_pool_and_timeouts`  
**Fingerprint:** `09cc6f1449712c70…` (identical for every agent)

Requests are timing out. Every pool connection is in use and over a hundred requests queue for one, yet queries are fast and the database server is mostly idle.

## Comparison

| Agent | Bottleneck | Matches ground truth | Top step | Steps | Confidence | Evidence items | Mechanism detail | Verification metric |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Simple Reflex | CONNECTION_POOL_SATURATION | ✔ | inspect_pool_usage | inspect_pool_usage → fix_connection_leak → tune_pool_and_timeouts | 0.8 | 8 | rule 1 (pool saturated) on window 14:05 | Pool waiting = 0, avg wait < pool-wait SLO, zero acquire timeouts and idle-in-transaction < 5 for 15 minutes (now: p95 5100 ms, pool waiting 167) |
| Model-Based Reflex | CONNECTION_POOL_SATURATION | ✔ | inspect_pool_usage | inspect_pool_usage → fix_connection_leak → tune_pool_and_timeouts | 0.95 | 9 | 1 hypothesis(es) from state over 6 windows | Pool waiting = 0, avg wait < pool-wait SLO, zero acquire timeouts and idle-in-transaction < 5 for 15 minutes (now: p95 5100 ms, pool waiting 167) |
| Goal-Based | CONNECTION_POOL_SATURATION | ✔ | inspect_pool_usage | inspect_pool_usage → fix_connection_leak | 0.95 | 12 | plan of 2 step(s), 8 nodes expanded | Pool waiting = 0, avg wait < pool-wait SLO, zero acquire timeouts and idle-in-transaction < 5 for 15 minutes (now: p95 5100 ms, pool waiting 167) |
| Utility-Based | CONNECTION_POOL_SATURATION | ✔ | inspect_pool_usage | inspect_pool_usage → fix_connection_leak → tune_pool_and_timeouts | 0.95 | 12 | top EU fix_connection_leak = 0.647 | Pool waiting = 0, avg wait < pool-wait SLO, zero acquire timeouts and idle-in-transaction < 5 for 15 minutes (now: p95 5100 ms, pool waiting 167) |
| Learning | CONNECTION_POOL_SATURATION | ✔ | inspect_pool_usage | inspect_pool_usage → fix_connection_leak | 0.75 | 10 | CONNECTION_POOL_SATURATION\|fix_connection_leak rate 0.75 | Pool waiting = 0, avg wait < pool-wait SLO, zero acquire timeouts and idle-in-transaction < 5 for 15 minutes (now: p95 5100 ms, pool waiting 167) |
| LLM (Ollama + LangChain) | CONNECTION_POOL_SATURATION | ✔ | inspect_pool_usage | inspect_pool_usage → fix_connection_leak → tune_pool_and_timeouts | 0.88 | 7 | 4 tool calls (4 distinct), grounding 0.86 | connection-pool wait and p95 latency |

## Why decisions differ

6/6 agents agree on CONNECTION_POOL_SATURATION.
Expected bottleneck: CONNECTION_POOL_SATURATION (6/6 correct).
All agents reached the same bottleneck; they differ only in the steps and confidence:
- **Simple Reflex** → `CONNECTION_POOL_SATURATION`, main step `fix_connection_leak`, confidence 0.80: rule 1 (pool saturated) matched the latest window 14:05; it has no memory of earlier windows and stops at the first match.
- **Model-Based Reflex** → `CONNECTION_POOL_SATURATION`, main step `fix_connection_leak`, confidence 0.95: its internal state over all 6 windows showed pool saturated for 6/6 windows with max CPU 24% and 5 leak/idle-in-transaction log entries.
- **Goal-Based** → `CONNECTION_POOL_SATURATION`, main step `fix_connection_leak`, confidence 0.95: its search found the shortest safe plan inspect_pool_usage -> fix_connection_leak to the goal ['pool_ok'].
- **Utility-Based** → `CONNECTION_POOL_SATURATION`, main step `fix_connection_leak`, confidence 0.95: fix_connection_leak had the highest expected utility (0.647) given P(CONNECTION_POOL_SATURATION)=0.95 and weights {'risk': 0.3, 'cost': 0.2, 'time': 0.1}.
- **Learning** → `CONNECTION_POOL_SATURATION`, main step `fix_connection_leak`, confidence 0.75: fix_connection_leak has 5.0 successes / 1.0 failures (rate 0.75) for signature high_latency+low_cpu+pool_waiting from high_latency+low_cpu+pool_waiting (sim 1.00); next best CONNECTION_POOL_SATURATION|tune_pool_and_timeouts rate 0.50. Experience agrees with the prior ranking.
- **LLM (Ollama + LangChain)** → `CONNECTION_POOL_SATURATION`, main step `fix_connection_leak`, confidence 0.88: the model read get_connection_pool_status, get_database_metrics, list_queries, search_database_logs and concluded CONNECTION_POOL_SATURATION; 6/7 of its evidence items match tool outputs.

## Evidence and steps per agent

### Simple Reflex

*Latest window 14:05: p95 5100 ms vs 200 ms SLO, QPS 397, pool 50/50 (167 waiting). Symptoms: connection_leak_logs, error_logs, idle_in_transaction_high, latency_slo_breach, p99_spike, pool_saturated, pool_timeouts, pool_wait_slo_breach. Diagnosis: Requests wait for a connection because the pool is exhausted.*

Evidence:
- p95 latency 5100 ms at 14:05 exceeds the 200 ms SLO
- p99 30000 ms is 5.9x p95 at 14:05 (intermittent spikes)
- Pool 50/50 active with 167 requests waiting at 14:05
- Pool avg wait 3900 ms exceeds the 50 ms SLO
- 29 connection-acquire timeouts at 14:05
- 25 sessions idle in transaction at 14:05
- Log: [14:05:02 ERROR app] Request failed: SQLTransientConnectionException: HikariPool-1 - Connection is not available, request timed out after 30000ms (total=50, active=50, idle=0, waiting=167)
- Log: [14:05:30 WARNING app] Connection leak detection triggered for org.postgresql.jdbc.PgConnection@a01c55 on thread billing-export-1, stack trace follows: at com.shop.billing.InvoiceExporter.export(InvoiceExporter.java:88)

Recommended steps:
1. [inspect_pool_usage] Inspect pool usage and idle-in-transaction sessions: Check pool active/idle/waiting counts over time and list idle-in-transaction sessions in pg_stat_activity to find connections that are held but not used.
2. [fix_connection_leak] Fix connection leaks in application code: Use the pool's leak-detection stack traces to find code paths that do not return connections (missing close / context manager) and fix them in a reviewed change.
3. [tune_pool_and_timeouts] Propose pool size and timeout changes: Propose for review: a pool size sized to DB capacity, a shorter connection-acquire timeout, and an idle_in_transaction_session_timeout, rolled out in staging first.

Verification: Pool waiting = 0, avg wait < pool-wait SLO, zero acquire timeouts and idle-in-transaction < 5 for 15 minutes (now: p95 5100 ms, pool waiting 167)

Reasoning trace:
- Percept: latest window 14:05 only (6 windows exist; earlier ones are ignored)
- Rule 1 (pool saturated) FIRED: pool.active 50 == size 50 and pool.waiting 167 > 0

### Model-Based Reflex

*6 windows 14:00-14:05: p95 5100 ms vs 200 ms SLO, QPS 397, pool 50/50 (167 waiting). Symptoms: connection_leak_logs, error_logs, idle_in_transaction_high, latency_slo_breach, p99_spike, pool_saturated, pool_timeouts, pool_wait_slo_breach. Diagnosis: Requests wait for a connection because the pool is exhausted.*

Evidence:
- Over 6 windows: pool saturated for 6/6 windows with max CPU 24% and 5 leak/idle-in-transaction log entries
- p95 latency 5100 ms at 14:05 exceeds the 200 ms SLO
- p99 30000 ms is 5.9x p95 at 14:05 (intermittent spikes)
- Pool 50/50 active with 167 requests waiting at 14:05
- Pool avg wait 3900 ms exceeds the 50 ms SLO
- 29 connection-acquire timeouts at 14:05
- 25 sessions idle in transaction at 14:05
- Log: [14:05:02 ERROR app] Request failed: SQLTransientConnectionException: HikariPool-1 - Connection is not available, request timed out after 30000ms (total=50, active=50, idle=0, waiting=167)
- Log: [14:05:30 WARNING app] Connection leak detection triggered for org.postgresql.jdbc.PgConnection@a01c55 on thread billing-export-1, stack trace follows: at com.shop.billing.InvoiceExporter.export(InvoiceExporter.java:88)

Recommended steps:
1. [inspect_pool_usage] Inspect pool usage and idle-in-transaction sessions: Check pool active/idle/waiting counts over time and list idle-in-transaction sessions in pg_stat_activity to find connections that are held but not used.
2. [fix_connection_leak] Fix connection leaks in application code: Use the pool's leak-detection stack traces to find code paths that do not return connections (missing close / context manager) and fix them in a reviewed change.
3. [tune_pool_and_timeouts] Propose pool size and timeout changes: Propose for review: a pool size sized to DB capacity, a shorter connection-acquire timeout, and an idle_in_transaction_session_timeout, rolled out in staging first.

Verification: Pool waiting = 0, avg wait < pool-wait SLO, zero acquire timeouts and idle-in-transaction < 5 for 15 minutes (now: p95 5100 ms, pool waiting 167)

Reasoning trace:
- update_state(14:00): symptoms=['idle_in_transaction_high', 'latency_slo_breach', 'p99_spike', 'pool_saturated', 'pool_timeouts', 'pool_wait_slo_breach']
- update_state(14:01): symptoms=['connection_leak_logs', 'idle_in_transaction_high', 'latency_slo_breach', 'p99_spike', 'pool_saturated', 'pool_timeouts', 'pool_wait_slo_breach']
- update_state(14:02): symptoms=['error_logs', 'idle_in_transaction_high', 'latency_slo_breach', 'p99_spike', 'pool_saturated', 'pool_timeouts', 'pool_wait_slo_breach']
- update_state(14:03): symptoms=['connection_leak_logs', 'idle_in_transaction_high', 'latency_slo_breach', 'p99_spike', 'pool_saturated', 'pool_timeouts', 'pool_wait_slo_breach']
- update_state(14:04): symptoms=['error_logs', 'idle_in_transaction_high', 'latency_slo_breach', 'p99_spike', 'pool_saturated', 'pool_timeouts', 'pool_wait_slo_breach']
- update_state(14:05): symptoms=['connection_leak_logs', 'error_logs', 'idle_in_transaction_high', 'latency_slo_breach', 'p99_spike', 'pool_saturated', 'pool_timeouts', 'pool_wait_slo_breach']
- State: QPS x1.01, corr(p95,QPS)=0.19, corr(p95,CPU)=-0.49, max CPU=24.0, latency breach from window 0, pool saturated from window 0, telemetry missing in 0/6
- World model supports CONNECTION_POOL_SATURATION (strength 1.00): pool saturated for 6/6 windows with max CPU 24% and 5 leak/idle-in-transaction log entries
- Decision: CONNECTION_POOL_SATURATION because pool saturated for 6/6 windows with max CPU 24% and 5 leak/idle-in-transaction log entries

### Goal-Based

*6 windows: p95 5100 ms vs 200 ms SLO, QPS 397, pool 50/50 (167 waiting). Symptoms: connection_leak_logs, error_logs, idle_in_transaction_high, latency_slo_breach, p99_spike, pool_saturated, pool_timeouts, pool_wait_slo_breach. Diagnosis: Requests wait for a connection because the pool is exhausted.*

Evidence:
- Evidence for CONNECTION_POOL_SATURATION: +1.00 pool saturated in 100% of windows
- Evidence for CONNECTION_POOL_SATURATION: +0.40 acquire timeouts
- Evidence for CONNECTION_POOL_SATURATION: +0.40 many idle-in-transaction sessions
- Evidence for CONNECTION_POOL_SATURATION: +0.20 connection-leak log entries
- p95 latency 5100 ms at 14:05 exceeds the 200 ms SLO
- p99 30000 ms is 5.9x p95 at 14:05 (intermittent spikes)
- Pool 50/50 active with 167 requests waiting at 14:05
- Pool avg wait 3900 ms exceeds the 50 ms SLO
- 29 connection-acquire timeouts at 14:05
- 25 sessions idle in transaction at 14:05
- Log: [14:05:02 ERROR app] Request failed: SQLTransientConnectionException: HikariPool-1 - Connection is not available, request timed out after 30000ms (total=50, active=50, idle=0, waiting=167)
- Log: [14:05:30 WARNING app] Connection leak detection triggered for org.postgresql.jdbc.PgConnection@a01c55 on thread billing-export-1, stack trace follows: at com.shop.billing.InvoiceExporter.export(InvoiceExporter.java:88)

Recommended steps:
1. [inspect_pool_usage] Inspect pool usage and idle-in-transaction sessions: Check pool active/idle/waiting counts over time and list idle-in-transaction sessions in pg_stat_activity to find connections that are held but not used.
2. [fix_connection_leak] Fix connection leaks in application code: Use the pool's leak-detection stack traces to find code paths that do not return connections (missing close / context manager) and fix them in a reviewed change.

Verification: Pool waiting = 0, avg wait < pool-wait SLO, zero acquire timeouts and idle-in-transaction < 5 for 15 minutes (now: p95 5100 ms, pool waiting 167)

Reasoning trace:
- Belief: CONNECTION_POOL_SATURATION=0.95, AMBIGUOUS=0.05
- CONNECTION_POOL_SATURATION dominates (P=0.95); goal = CONNECTION_POOL_SATURATION resolved
- Start state: {} (nothing diagnosed yet); goal facts: ['pool_ok']
- BFS expanded 8 nodes (max frontier 26, depth <= 5)
- Step 1: inspect_pool_usage (pre -) -> state ['pool_diagnosed']
- Step 2: fix_connection_leak (pre ['pool_diagnosed']) -> state ['pool_diagnosed', 'pool_ok']
- Goal test passed: goal facts hold and every step is a safe recommendation

### Utility-Based

*6 windows: p95 5100 ms vs 200 ms SLO, QPS 397, pool 50/50 (167 waiting). Symptoms: connection_leak_logs, error_logs, idle_in_transaction_high, latency_slo_breach, p99_spike, pool_saturated, pool_timeouts, pool_wait_slo_breach. Diagnosis: Requests wait for a connection because the pool is exhausted.*

Evidence:
- Evidence for CONNECTION_POOL_SATURATION: +1.00 pool saturated in 100% of windows
- Evidence for CONNECTION_POOL_SATURATION: +0.40 acquire timeouts
- Evidence for CONNECTION_POOL_SATURATION: +0.40 many idle-in-transaction sessions
- Evidence for CONNECTION_POOL_SATURATION: +0.20 connection-leak log entries
- p95 latency 5100 ms at 14:05 exceeds the 200 ms SLO
- p99 30000 ms is 5.9x p95 at 14:05 (intermittent spikes)
- Pool 50/50 active with 167 requests waiting at 14:05
- Pool avg wait 3900 ms exceeds the 50 ms SLO
- 29 connection-acquire timeouts at 14:05
- 25 sessions idle in transaction at 14:05
- Log: [14:05:02 ERROR app] Request failed: SQLTransientConnectionException: HikariPool-1 - Connection is not available, request timed out after 30000ms (total=50, active=50, idle=0, waiting=167)
- Log: [14:05:30 WARNING app] Connection leak detection triggered for org.postgresql.jdbc.PgConnection@a01c55 on thread billing-export-1, stack trace follows: at com.shop.billing.InvoiceExporter.export(InvoiceExporter.java:88)

Recommended steps:
1. [inspect_pool_usage] Inspect pool usage and idle-in-transaction sessions: Check pool active/idle/waiting counts over time and list idle-in-transaction sessions in pg_stat_activity to find connections that are held but not used.
2. [fix_connection_leak] Fix connection leaks in application code: Use the pool's leak-detection stack traces to find code paths that do not return connections (missing close / context manager) and fix them in a reviewed change.
3. [tune_pool_and_timeouts] Propose pool size and timeout changes: Propose for review: a pool size sized to DB capacity, a shorter connection-acquire timeout, and an idle_in_transaction_session_timeout, rolled out in staging first.

Verification: Pool waiting = 0, avg wait < pool-wait SLO, zero acquire timeouts and idle-in-transaction < 5 for 15 minutes (now: p95 5100 ms, pool waiting 167)

Reasoning trace:
- P(h | evidence): CONNECTION_POOL_SATURATION=0.95, AMBIGUOUS=0.05
- Weights: {'risk': 0.3, 'cost': 0.2, 'time': 0.1}
- Top EU: fix_connection_leak=0.647, tune_pool_and_timeouts=0.517, inspect_pool_usage=0.336, rate_limit_peak_traffic=0.055
- Best action: fix_connection_leak (EU 0.647 = benefit 0.857 - 0.3*0.2 - 0.2*0.5 - 0.1*0.5)
- fix_connection_leak needs ['inspect_pool_usage'] first (its preconditions)
- Follow-up (next best EU): ['tune_pool_and_timeouts']

### Learning

*Signature high_latency+low_cpu+pool_waiting: p95 5100 ms vs 200 ms SLO, QPS 397, pool 50/50 (167 waiting). Symptoms: connection_leak_logs, error_logs, idle_in_transaction_high, latency_slo_breach, p99_spike, pool_saturated, pool_timeouts, pool_wait_slo_breach. Diagnosis: Requests wait for a connection because the pool is exhausted.*

Evidence:
- Symptom signature: high_latency+low_cpu+pool_waiting
- Experience: fix_connection_leak has 5.0 successes / 1.0 failures (rate 0.75) for signature high_latency+low_cpu+pool_waiting from high_latency+low_cpu+pool_waiting (sim 1.00); next best CONNECTION_POOL_SATURATION|tune_pool_and_timeouts rate 0.50. Experience agrees with the prior ranking
- p95 latency 5100 ms at 14:05 exceeds the 200 ms SLO
- p99 30000 ms is 5.9x p95 at 14:05 (intermittent spikes)
- Pool 50/50 active with 167 requests waiting at 14:05
- Pool avg wait 3900 ms exceeds the 50 ms SLO
- 29 connection-acquire timeouts at 14:05
- 25 sessions idle in transaction at 14:05
- Log: [14:05:02 ERROR app] Request failed: SQLTransientConnectionException: HikariPool-1 - Connection is not available, request timed out after 30000ms (total=50, active=50, idle=0, waiting=167)
- Log: [14:05:30 WARNING app] Connection leak detection triggered for org.postgresql.jdbc.PgConnection@a01c55 on thread billing-export-1, stack trace follows: at com.shop.billing.InvoiceExporter.export(InvoiceExporter.java:88)

Recommended steps:
1. [inspect_pool_usage] Inspect pool usage and idle-in-transaction sessions: Check pool active/idle/waiting counts over time and list idle-in-transaction sessions in pg_stat_activity to find connections that are held but not used.
2. [fix_connection_leak] Fix connection leaks in application code: Use the pool's leak-detection stack traces to find code paths that do not return connections (missing close / context manager) and fix them in a reviewed change.

Verification: Pool waiting = 0, avg wait < pool-wait SLO, zero acquire timeouts and idle-in-transaction < 5 for 15 minutes (now: p95 5100 ms, pool waiting 167)

Reasoning trace:
- Signature: high_latency+low_cpu+pool_waiting
- Evidence match: CONNECTION_POOL_SATURATION=1.00
-   CONNECTION_POOL_SATURATION|fix_connection_leak: match 1.00 x rate 0.75 (5.0 successes / 1.0 failures) = 0.750
-   CONNECTION_POOL_SATURATION|tune_pool_and_timeouts: match 1.00 x rate 0.50 (3.5 successes / 3.5 failures) = 0.500
-   CONNECTION_POOL_SATURATION|inspect_pool_usage: match 1.00 x rate 0.50 (0.0 successes / 0.0 failures) = 0.500
-   CONNECTION_POOL_SATURATION|rate_limit_peak_traffic: match 1.00 x rate 0.50 (0.0 successes / 0.0 failures) = 0.500
- fix_connection_leak has 5.0 successes / 1.0 failures (rate 0.75) for signature high_latency+low_cpu+pool_waiting from high_latency+low_cpu+pool_waiting (sim 1.00); next best CONNECTION_POOL_SATURATION|tune_pool_and_timeouts rate 0.50. Experience agrees with the prior ranking

### LLM (Ollama + LangChain)

*The database telemetry set B shows p95 latency and connection-pool wait exceeding the SLO, with connection leaks and idle-in-transaction sessions.*

Evidence:
- p95 latency is 5100 ms (SLO is 200 ms)
- connection-pool wait is 3900 ms (SLO is 50 ms)
- pool utilization is 100% in all windows
- average wait time is increasing over time
- idle-in-transaction sessions are increasing over time
- connection leaks detected in the billing-export threads
- SQLTransientConnectionException due to pool exhaustion

Recommended steps:
1. [inspect_pool_usage] Inspect pool usage and idle-in-transaction sessions: Check pool active/idle/waiting counts over time and list idle-in-transaction sessions in pg_stat_activity to find connections that are held but not used.
2. [fix_connection_leak] Fix connection leaks in application code: Use the pool's leak-detection stack traces to find code paths that do not return connections (missing close / context manager) and fix them in a reviewed change. Model's reason: Address connection leaks in the billing-export threads
3. [tune_pool_and_timeouts] Propose pool size and timeout changes: Propose for review: a pool size sized to DB capacity, a shorter connection-acquire timeout, and an idle_in_transaction_session_timeout, rolled out in staging first. Model's reason: Increase pool size or adjust timeouts to reduce wait times

Verification: connection-pool wait and p95 latency

Reasoning trace:
- Model qwen3:8b, 6 read-only tools, max 8 turns
- Turn 1: answered after 0 distinct tool(s), missing 3 evidence group(s); re-prompt 1/2
- Turn 2: get_database_metrics({"window": "all"}) -> 1.04 ms
- Turn 2: get_connection_pool_status({"window": "all"}) -> 0.34 ms
- Turn 3: answered after 2 distinct tool(s), missing 2 evidence group(s); re-prompt 2/2
- Turn 4: list_queries({}) -> 2.57 ms
- Turn 4: search_database_logs({"level": "WARNING"}) -> 2.72 ms
- Grounding: 6/7 evidence items verified against tool outputs
- Diagnostics first: inserted inspect_pool_usage before fix_connection_leak
- Confidence: model 0.95 x grounding factor 0.93 = 0.88
