# Scenario A: Frequent query slow: full table scan

**Category:** normal  
**Expected bottleneck:** `MISSING_INDEX_FULL_SCAN`  
**Acceptable actions:** `explain_analyze_in_staging`, `review_index_proposal`  
**Fingerprint:** `f21fcc982ab9cdf4…` (identical for every agent)

The customer order-history endpoint is slow. Traffic is steady, the pool has headroom, but the most frequent query reads the whole orders table to return a few rows.

## Comparison

| Agent | Bottleneck | Matches ground truth | Top step | Steps | Confidence | Evidence items | Mechanism detail | Verification metric |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Simple Reflex | MISSING_INDEX_FULL_SCAN | ✔ | explain_analyze_in_staging | explain_analyze_in_staging → review_index_proposal | 0.8 | 7 | rule 2 (large full scan) on window 10:05 | Slow query uses an Index Scan, mean latency < 50 ms, and service p95 < SLO for 15 minutes (now: Q1 mean 850 ms, p95 890 ms) |
| Model-Based Reflex | MISSING_INDEX_FULL_SCAN | ✔ | explain_analyze_in_staging | explain_analyze_in_staging → review_index_proposal | 0.95 | 8 | 1 hypothesis(es) from state over 6 windows | Slow query uses an Index Scan, mean latency < 50 ms, and service p95 < SLO for 15 minutes (now: Q1 mean 850 ms, p95 890 ms) |
| Goal-Based | MISSING_INDEX_FULL_SCAN | ✔ | explain_analyze_in_staging | explain_analyze_in_staging → review_index_proposal | 0.94 | 8 | plan of 2 step(s), 8 nodes expanded | Slow query uses an Index Scan, mean latency < 50 ms, and service p95 < SLO for 15 minutes (now: Q1 mean 850 ms, p95 890 ms) |
| Utility-Based | MISSING_INDEX_FULL_SCAN | ✔ | explain_analyze_in_staging | explain_analyze_in_staging → review_index_proposal | 0.94 | 8 | top EU review_index_proposal = 0.748 | Slow query uses an Index Scan, mean latency < 50 ms, and service p95 < SLO for 15 minutes (now: Q1 mean 850 ms, p95 890 ms) |
| Learning | MISSING_INDEX_FULL_SCAN | ✔ | explain_analyze_in_staging | explain_analyze_in_staging → review_index_proposal | 0.82 | 9 | MISSING_INDEX_FULL_SCAN\|review_index_proposal rate 0.82 | Slow query uses an Index Scan, mean latency < 50 ms, and service p95 < SLO for 15 minutes (now: Q1 mean 850 ms, p95 890 ms) |
| LLM (Ollama + LangChain) | MISSING_INDEX_FULL_SCAN | ✔ | explain_analyze_in_staging | explain_analyze_in_staging → review_index_proposal → analyze_stats_in_staging | 0.95 | 5 | 6 tool calls (6 distinct), grounding 1.00 | p95 latency for the GET /api/customers/orders endpoint |

## Why decisions differ

6/6 agents agree on MISSING_INDEX_FULL_SCAN.
Expected bottleneck: MISSING_INDEX_FULL_SCAN (6/6 correct).
All agents reached the same bottleneck; they differ only in the steps and confidence:
- **Simple Reflex** → `MISSING_INDEX_FULL_SCAN`, main step `review_index_proposal`, confidence 0.80: rule 2 (large full scan) matched the latest window 10:05; it has no memory of earlier windows and stops at the first match.
- **Model-Based Reflex** → `MISSING_INDEX_FULL_SCAN`, main step `review_index_proposal`, confidence 0.95: its internal state over all 6 windows showed large Seq Scan present in 6/6 windows while QPS is flat (x1.01).
- **Goal-Based** → `MISSING_INDEX_FULL_SCAN`, main step `review_index_proposal`, confidence 0.94: its search found the shortest safe plan explain_analyze_in_staging -> review_index_proposal to the goal ['plan_fixed'].
- **Utility-Based** → `MISSING_INDEX_FULL_SCAN`, main step `review_index_proposal`, confidence 0.94: review_index_proposal had the highest expected utility (0.748) given P(MISSING_INDEX_FULL_SCAN)=0.94 and weights {'risk': 0.3, 'cost': 0.2, 'time': 0.1}.
- **Learning** → `MISSING_INDEX_FULL_SCAN`, main step `review_index_proposal`, confidence 0.82: review_index_proposal has 8.3 successes / 1.0 failures (rate 0.82) for signature full_scan+high_latency from full_scan+high_latency (sim 1.00), full_scan+high_latency+qps_up (sim 0.67); next best MISSING_INDEX_FULL_SCAN|explain_analyze_in_staging rate 0.75. Experience agrees with the prior ranking.
- **LLM (Ollama + LangChain)** → `MISSING_INDEX_FULL_SCAN`, main step `review_index_proposal`, confidence 0.95: the model read get_connection_pool_status, get_database_metrics, get_index_metadata, inspect_query_plan, list_queries, search_database_logs and concluded MISSING_INDEX_FULL_SCAN; 5/5 of its evidence items match tool outputs.

## Evidence and steps per agent

### Simple Reflex

*Latest window 10:05: p95 890 ms vs 200 ms SLO, QPS 415, pool 32/50 (0 waiting). Symptoms: cache_hit_low, full_scan_large_table, io_wait_high, latency_slo_breach, unindexed_filter. Diagnosis: A frequent query scans a large table because no index covers its filter.*

Evidence:
- p95 latency 890 ms at 10:05 exceeds the 200 ms SLO
- Q1 Seq Scan on orders: 2,400,000 rows examined for 3 returned, 3000 calls/min, mean 850 ms
- No index covers orders(customer_email)
- I/O wait 20% at 10:05
- Cache hit ratio 0.83 at 10:05
- Log: [10:05:40 WARNING app] GET /api/customers/orders p95 latency 890 ms exceeds SLO 200 ms
- Log: [10:05:12 LOG postgres] duration: 948.3 ms  statement: SELECT id, status, total_amount FROM orders WHERE customer_email = 'r***@example.com' ...

Recommended steps:
1. [explain_analyze_in_staging] Confirm the plan with EXPLAIN ANALYZE in staging: Run EXPLAIN (ANALYZE, BUFFERS) for the slow query on a staging replica and confirm where the time goes (scan type, rows examined vs returned, buffer reads). Target: Q1 (Seq Scan on orders).
2. [review_index_proposal] Propose an index for DBA review: Draft a CREATE INDEX CONCURRENTLY proposal on the filtered column(s) for DBA review, test it in staging first, and do not execute it in production from this tool. Proposed index: orders(customer_email).

Verification: Slow query uses an Index Scan, mean latency < 50 ms, and service p95 < SLO for 15 minutes (now: Q1 mean 850 ms, p95 890 ms)

Reasoning trace:
- Percept: latest window 10:05 only (6 windows exist; earlier ones are ignored)
- Rule 1 (pool saturated) did not match
- Rule 2 (large full scan) FIRED: Q1 Seq Scan examines 2,400,000 rows for 3 (ratio 800,000)

### Model-Based Reflex

*6 windows 10:00-10:05: p95 890 ms vs 200 ms SLO, QPS 415, pool 32/50 (0 waiting). Symptoms: cache_hit_low, full_scan_large_table, io_wait_high, latency_slo_breach, unindexed_filter. Diagnosis: A frequent query scans a large table because no index covers its filter.*

Evidence:
- Over 6 windows: large Seq Scan present in 6/6 windows while QPS is flat (x1.01)
- p95 latency 890 ms at 10:05 exceeds the 200 ms SLO
- Q1 Seq Scan on orders: 2,400,000 rows examined for 3 returned, 3000 calls/min, mean 850 ms
- No index covers orders(customer_email)
- I/O wait 20% at 10:05
- Cache hit ratio 0.83 at 10:05
- Log: [10:05:40 WARNING app] GET /api/customers/orders p95 latency 890 ms exceeds SLO 200 ms
- Log: [10:05:12 LOG postgres] duration: 948.3 ms  statement: SELECT id, status, total_amount FROM orders WHERE customer_email = 'r***@example.com' ...

Recommended steps:
1. [explain_analyze_in_staging] Confirm the plan with EXPLAIN ANALYZE in staging: Run EXPLAIN (ANALYZE, BUFFERS) for the slow query on a staging replica and confirm where the time goes (scan type, rows examined vs returned, buffer reads). Target: Q1 (Seq Scan on orders).
2. [review_index_proposal] Propose an index for DBA review: Draft a CREATE INDEX CONCURRENTLY proposal on the filtered column(s) for DBA review, test it in staging first, and do not execute it in production from this tool. Proposed index: orders(customer_email).

Verification: Slow query uses an Index Scan, mean latency < 50 ms, and service p95 < SLO for 15 minutes (now: Q1 mean 850 ms, p95 890 ms)

Reasoning trace:
- update_state(10:00): symptoms=['cache_hit_low', 'full_scan_large_table', 'latency_slo_breach', 'unindexed_filter']
- update_state(10:01): symptoms=['cache_hit_low', 'full_scan_large_table', 'io_wait_high', 'latency_slo_breach', 'unindexed_filter']
- update_state(10:02): symptoms=['cache_hit_low', 'full_scan_large_table', 'io_wait_high', 'latency_slo_breach', 'unindexed_filter']
- update_state(10:03): symptoms=['cache_hit_low', 'full_scan_large_table', 'io_wait_high', 'latency_slo_breach', 'unindexed_filter']
- update_state(10:04): symptoms=['cache_hit_low', 'full_scan_large_table', 'io_wait_high', 'latency_slo_breach', 'unindexed_filter']
- update_state(10:05): symptoms=['cache_hit_low', 'full_scan_large_table', 'io_wait_high', 'latency_slo_breach', 'unindexed_filter']
- State: QPS x1.01, corr(p95,QPS)=0.96, corr(p95,CPU)=0.97, max CPU=63.0, latency breach from window 0, pool saturated from window None, telemetry missing in 0/6
- World model supports MISSING_INDEX_FULL_SCAN (strength 1.00): large Seq Scan present in 6/6 windows while QPS is flat (x1.01)
- Decision: MISSING_INDEX_FULL_SCAN because large Seq Scan present in 6/6 windows while QPS is flat (x1.01)

### Goal-Based

*6 windows: p95 890 ms vs 200 ms SLO, QPS 415, pool 32/50 (0 waiting). Symptoms: cache_hit_low, full_scan_large_table, io_wait_high, latency_slo_breach, unindexed_filter. Diagnosis: A frequent query scans a large table because no index covers its filter.*

Evidence:
- Evidence for MISSING_INDEX_FULL_SCAN: +1.50 large full scan in 100% of windows
- p95 latency 890 ms at 10:05 exceeds the 200 ms SLO
- Q1 Seq Scan on orders: 2,400,000 rows examined for 3 returned, 3000 calls/min, mean 850 ms
- No index covers orders(customer_email)
- I/O wait 20% at 10:05
- Cache hit ratio 0.83 at 10:05
- Log: [10:05:40 WARNING app] GET /api/customers/orders p95 latency 890 ms exceeds SLO 200 ms
- Log: [10:05:12 LOG postgres] duration: 948.3 ms  statement: SELECT id, status, total_amount FROM orders WHERE customer_email = 'r***@example.com' ...

Recommended steps:
1. [explain_analyze_in_staging] Confirm the plan with EXPLAIN ANALYZE in staging: Run EXPLAIN (ANALYZE, BUFFERS) for the slow query on a staging replica and confirm where the time goes (scan type, rows examined vs returned, buffer reads). Target: Q1 (Seq Scan on orders).
2. [review_index_proposal] Propose an index for DBA review: Draft a CREATE INDEX CONCURRENTLY proposal on the filtered column(s) for DBA review, test it in staging first, and do not execute it in production from this tool. Proposed index: orders(customer_email).

Verification: Slow query uses an Index Scan, mean latency < 50 ms, and service p95 < SLO for 15 minutes (now: Q1 mean 850 ms, p95 890 ms)

Reasoning trace:
- Belief: MISSING_INDEX_FULL_SCAN=0.94, AMBIGUOUS=0.06
- MISSING_INDEX_FULL_SCAN dominates (P=0.94); goal = MISSING_INDEX_FULL_SCAN resolved
- Start state: {} (nothing diagnosed yet); goal facts: ['plan_fixed']
- BFS expanded 8 nodes (max frontier 26, depth <= 5)
- Step 1: explain_analyze_in_staging (pre -) -> state ['plan_confirmed']
- Step 2: review_index_proposal (pre ['plan_confirmed']) -> state ['plan_confirmed', 'plan_fixed']
- Goal test passed: goal facts hold and every step is a safe recommendation

### Utility-Based

*6 windows: p95 890 ms vs 200 ms SLO, QPS 415, pool 32/50 (0 waiting). Symptoms: cache_hit_low, full_scan_large_table, io_wait_high, latency_slo_breach, unindexed_filter. Diagnosis: A frequent query scans a large table because no index covers its filter.*

Evidence:
- Evidence for MISSING_INDEX_FULL_SCAN: +1.50 large full scan in 100% of windows
- p95 latency 890 ms at 10:05 exceeds the 200 ms SLO
- Q1 Seq Scan on orders: 2,400,000 rows examined for 3 returned, 3000 calls/min, mean 850 ms
- No index covers orders(customer_email)
- I/O wait 20% at 10:05
- Cache hit ratio 0.83 at 10:05
- Log: [10:05:40 WARNING app] GET /api/customers/orders p95 latency 890 ms exceeds SLO 200 ms
- Log: [10:05:12 LOG postgres] duration: 948.3 ms  statement: SELECT id, status, total_amount FROM orders WHERE customer_email = 'r***@example.com' ...

Recommended steps:
1. [explain_analyze_in_staging] Confirm the plan with EXPLAIN ANALYZE in staging: Run EXPLAIN (ANALYZE, BUFFERS) for the slow query on a staging replica and confirm where the time goes (scan type, rows examined vs returned, buffer reads). Target: Q1 (Seq Scan on orders).
2. [review_index_proposal] Propose an index for DBA review: Draft a CREATE INDEX CONCURRENTLY proposal on the filtered column(s) for DBA review, test it in staging first, and do not execute it in production from this tool. Proposed index: orders(customer_email).

Verification: Slow query uses an Index Scan, mean latency < 50 ms, and service p95 < SLO for 15 minutes (now: Q1 mean 850 ms, p95 890 ms)

Reasoning trace:
- P(h | evidence): MISSING_INDEX_FULL_SCAN=0.94, AMBIGUOUS=0.06
- Weights: {'risk': 0.3, 'cost': 0.2, 'time': 0.1}
- Top EU: review_index_proposal=0.748, explain_analyze_in_staging=0.349, continue_monitoring=0.000, collect_missing_telemetry=-0.003
- Best action: review_index_proposal (EU 0.748 = benefit 0.938 - 0.3*0.3 - 0.2*0.3 - 0.1*0.4)
- review_index_proposal needs ['explain_analyze_in_staging'] first (its preconditions)

### Learning

*Signature full_scan+high_latency: p95 890 ms vs 200 ms SLO, QPS 415, pool 32/50 (0 waiting). Symptoms: cache_hit_low, full_scan_large_table, io_wait_high, latency_slo_breach, unindexed_filter. Diagnosis: A frequent query scans a large table because no index covers its filter.*

Evidence:
- Symptom signature: full_scan+high_latency
- Experience: review_index_proposal has 8.3 successes / 1.0 failures (rate 0.82) for signature full_scan+high_latency from full_scan+high_latency (sim 1.00), full_scan+high_latency+qps_up (sim 0.67); next best MISSING_INDEX_FULL_SCAN|explain_analyze_in_staging rate 0.75. Experience agrees with the prior ranking
- p95 latency 890 ms at 10:05 exceeds the 200 ms SLO
- Q1 Seq Scan on orders: 2,400,000 rows examined for 3 returned, 3000 calls/min, mean 850 ms
- No index covers orders(customer_email)
- I/O wait 20% at 10:05
- Cache hit ratio 0.83 at 10:05
- Log: [10:05:40 WARNING app] GET /api/customers/orders p95 latency 890 ms exceeds SLO 200 ms
- Log: [10:05:12 LOG postgres] duration: 948.3 ms  statement: SELECT id, status, total_amount FROM orders WHERE customer_email = 'r***@example.com' ...

Recommended steps:
1. [explain_analyze_in_staging] Confirm the plan with EXPLAIN ANALYZE in staging: Run EXPLAIN (ANALYZE, BUFFERS) for the slow query on a staging replica and confirm where the time goes (scan type, rows examined vs returned, buffer reads). Target: Q1 (Seq Scan on orders).
2. [review_index_proposal] Propose an index for DBA review: Draft a CREATE INDEX CONCURRENTLY proposal on the filtered column(s) for DBA review, test it in staging first, and do not execute it in production from this tool. Proposed index: orders(customer_email).

Verification: Slow query uses an Index Scan, mean latency < 50 ms, and service p95 < SLO for 15 minutes (now: Q1 mean 850 ms, p95 890 ms)

Reasoning trace:
- Signature: full_scan+high_latency
- Evidence match: MISSING_INDEX_FULL_SCAN=1.00
-   MISSING_INDEX_FULL_SCAN|review_index_proposal: match 1.00 x rate 0.82 (8.3 successes / 1.0 failures) = 0.824
-   MISSING_INDEX_FULL_SCAN|explain_analyze_in_staging: match 1.00 x rate 0.75 (2.0 successes / 0.0 failures) = 0.750
- review_index_proposal has 8.3 successes / 1.0 failures (rate 0.82) for signature full_scan+high_latency from full_scan+high_latency (sim 1.00), full_scan+high_latency+qps_up (sim 0.67); next best MISSING_INDEX_FULL_SCAN|explain_analyze_in_staging rate 0.75. Experience agrees with the prior ranking

### LLM (Ollama + LangChain)

*The 'orders' table is experiencing a full scan due to the lack of an index on the 'customer_email' column, leading to high latency and poor query performance.*

Evidence:
- Query Q1 has a Seq Scan on the 'orders' table with a mean latency of 850 ms
- The 'orders' table has 2,400,000 rows and was last analyzed on 2026-11-08
- The 'orders' table has an index 'orders_pkey' on the 'id' column and an index 'orders_created_at_idx' on the 'created_at' column
- The query plan for Q1 shows a full scan with an estimate error ratio of 1.3 and rows examined per row returned of 800,000.0
- The 'customer_email' column is not indexed, leading to a full scan

Recommended steps:
1. [explain_analyze_in_staging] Confirm the plan with EXPLAIN ANALYZE in staging: Run EXPLAIN (ANALYZE, BUFFERS) for the slow query on a staging replica and confirm where the time goes (scan type, rows examined vs returned, buffer reads).
2. [review_index_proposal] Propose an index for DBA review: Draft a CREATE INDEX CONCURRENTLY proposal on the filtered column(s) for DBA review, test it in staging first, and do not execute it in production from this tool. Model's reason: Propose an index on the 'orders' table for the 'customer_email' column to optimize the Seq Scan
3. [analyze_stats_in_staging] Refresh statistics in staging and compare plans: On a staging copy, refresh table statistics with ANALYZE and compare the planner's estimated rows and the chosen plan before and after. Model's reason: Refresh statistics in staging and compare plans to confirm the impact of the index

Verification: p95 latency for the GET /api/customers/orders endpoint

Reasoning trace:
- Model qwen3:8b, 6 read-only tools, max 8 turns
- Turn 1: answered after 0 distinct tool(s), missing 3 evidence group(s); re-prompt 1/2
- Turn 2: get_database_metrics({"window": "all"}) -> 1.3 ms
- Turn 2: get_connection_pool_status({"window": "all"}) -> 0.29 ms
- Turn 3: list_queries({}) -> 4.83 ms
- Turn 4: search_database_logs({"level": "WARNING", "pattern": ".*"}) -> 3.63 ms
- Turn 5: answered after 4 distinct tool(s), missing 1 evidence group(s); re-prompt 2/2
- Turn 6: inspect_query_plan({"query_id": "Q1"}) -> 2.78 ms
- Turn 7: get_index_metadata({"table": "orders"}) -> 2.18 ms
- Grounding: 5/5 evidence items verified against tool outputs
- Diagnostics first: inserted explain_analyze_in_staging before review_index_proposal
- Confidence: model 0.95 x grounding factor 1.00 = 0.95
