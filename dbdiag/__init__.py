"""Database Performance Diagnosis Assistant.

Shared core used by all six agents:

- ``models``      data models for scenarios and agent decisions
- ``scenarios``   loading scenario files and fingerprinting them
- ``tools``       read-only data-access functions (also wrapped as LLM tools)
- ``perception``  turns a scenario into percepts for the classical agents
- ``knowledge``   thresholds, safe-action catalogue, verification metrics
- ``safety``      guard that rejects recommendations to execute schema/data changes
- ``agents``      the six agents (one module each)
- ``runner``      runs all agents on one frozen scenario and checks fingerprints
- ``comparison``  comparison table, agreement summary, "why decisions differ"
"""
