# Summary: all agents on all scenarios

Cells show ✔/✘ against the expected bottleneck, the diagnosis and the confidence.

| Agent | A | B | C | N | E | Bottleneck correct | Acceptable action |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Simple Reflex | ✔ MISSING_INDEX_FULL_SCAN (0.80) | ✔ CONNECTION_POOL_SATURATION (0.80) | ✘ CONNECTION_POOL_SATURATION (0.80) | ✔ HEALTHY (0.90) | ✘ LOCK_CONTENTION (0.70) | 3/5 | 4/5 |
| Model-Based Reflex | ✔ MISSING_INDEX_FULL_SCAN (0.95) | ✔ CONNECTION_POOL_SATURATION (0.95) | ✔ RESOURCE_SATURATION_PEAK_LOAD (0.94) | ✔ HEALTHY (0.90) | ✔ AMBIGUOUS (0.28) | 5/5 | 5/5 |
| Goal-Based | ✔ MISSING_INDEX_FULL_SCAN (0.94) | ✔ CONNECTION_POOL_SATURATION (0.95) | ✔ RESOURCE_SATURATION_PEAK_LOAD (0.71) | ✔ HEALTHY (0.91) | ✔ AMBIGUOUS (0.41) | 5/5 | 5/5 |
| Utility-Based | ✔ MISSING_INDEX_FULL_SCAN (0.94) | ✔ CONNECTION_POOL_SATURATION (0.95) | ✔ RESOURCE_SATURATION_PEAK_LOAD (0.71) | ✔ HEALTHY (0.91) | ✔ AMBIGUOUS (0.41) | 5/5 | 5/5 |
| Learning | ✔ MISSING_INDEX_FULL_SCAN (0.82) | ✔ CONNECTION_POOL_SATURATION (0.75) | ✔ RESOURCE_SATURATION_PEAK_LOAD (0.71) | ✔ HEALTHY (0.88) | ✔ AMBIGUOUS (0.77) | 5/5 | 5/5 |
| LLM (Ollama + LangChain) | ✔ MISSING_INDEX_FULL_SCAN (0.95) | ✔ CONNECTION_POOL_SATURATION (0.88) | ✘ CONNECTION_POOL_SATURATION (0.85) | ✔ HEALTHY (0.93) | ✔ AMBIGUOUS (0.70) | 4/5 | 4/5 |

## Agreement and disagreements

- **A** (normal): 6/6 agents agree on MISSING_INDEX_FULL_SCAN.
- **B** (critical): 6/6 agents agree on CONNECTION_POOL_SATURATION.
- **C** (edge): 4/6 agents agree on RESOURCE_SATURATION_PEAK_LOAD; others: CONNECTION_POOL_SATURATION (2).
- **N** (normal): 6/6 agents agree on HEALTHY.
- **E** (edge): 5/6 agents agree on AMBIGUOUS; others: LOCK_CONTENTION (1).
