from __future__ import annotations

import pytest

from dbdiag.models import Scenario
from dbdiag.scenarios import load_all_scenarios

REQUIRED_IDS = ("A", "B", "C")
ALL_IDS = ("A", "B", "C", "N", "E")


@pytest.fixture(scope="session")
def all_scenarios() -> dict[str, Scenario]:
    """Full scenarios (with ground truth), keyed by id."""
    return load_all_scenarios()


@pytest.fixture(scope="session")
def views(all_scenarios) -> dict[str, Scenario]:
    """What agents receive: scenarios without ground truth."""
    return {sid: s.agent_view() for sid, s in all_scenarios.items()}


def find_key(obj, key: str) -> bool:
    """True if ``key`` appears as a dict key anywhere in a nested structure."""
    if isinstance(obj, dict):
        return key in obj or any(find_key(v, key) for v in obj.values())
    if isinstance(obj, (list, tuple)):
        return any(find_key(v, key) for v in obj)
    return False
