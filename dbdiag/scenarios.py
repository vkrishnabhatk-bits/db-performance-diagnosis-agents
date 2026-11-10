"""Load scenario files and fingerprint them.

``load_scenario`` returns the full scenario, including its ground truth. Agents
must be given ``scenario.agent_view()`` instead (the runner does this), so the
expected answer never reaches them.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dbdiag.models import Scenario

SCENARIO_DIR = Path(__file__).resolve().parent.parent / "data" / "scenarios"

# Required scenarios first, then the extra ones.
DISPLAY_ORDER: tuple[str, ...] = ("A", "B", "C", "N", "E")


@dataclass(frozen=True)
class ScenarioSummary:
    id: str
    title: str
    category: str
    description: str


def load_scenario_file(path: Path | str) -> Scenario:
    """Parse and validate one scenario JSON file."""
    text = Path(path).read_text(encoding="utf-8")
    return Scenario.model_validate_json(text)


@lru_cache(maxsize=8)
def _load_all(directory: Path) -> dict[str, Scenario]:
    scenarios: dict[str, Scenario] = {}
    for path in sorted(directory.glob("*.json")):
        scenario = load_scenario_file(path)
        if scenario.id in scenarios:
            raise ValueError(f"duplicate scenario id {scenario.id!r} in {path.name}")
        scenarios[scenario.id] = scenario
    return scenarios


def _order_key(scenario_id: str) -> tuple[int, str]:
    if scenario_id in DISPLAY_ORDER:
        return (DISPLAY_ORDER.index(scenario_id), scenario_id)
    return (len(DISPLAY_ORDER), scenario_id)


def load_all_scenarios(directory: Path | str = SCENARIO_DIR) -> dict[str, Scenario]:
    """All scenarios in display order, keyed by id (full objects, with ground truth)."""
    loaded = _load_all(Path(directory).resolve())
    return {sid: loaded[sid] for sid in sorted(loaded, key=_order_key)}


def list_scenarios(directory: Path | str = SCENARIO_DIR) -> list[ScenarioSummary]:
    return [
        ScenarioSummary(s.id, s.title, s.category, s.description)
        for s in load_all_scenarios(directory).values()
    ]


def load_scenario(scenario_id: str, directory: Path | str = SCENARIO_DIR) -> Scenario:
    """Full scenario (with ground truth). Give agents ``.agent_view()``."""
    scenarios = load_all_scenarios(directory)
    try:
        return scenarios[scenario_id]
    except KeyError:
        raise KeyError(f"unknown scenario {scenario_id!r}; available: {list(scenarios)}") from None


def fingerprint(scenario: Scenario) -> str:
    """SHA-256 of the scenario's canonical JSON, excluding the ground truth.

    The full scenario and its agent view have the same fingerprint, so every
    agent's decision can be checked against the scenario that was selected.
    """
    payload = scenario.model_dump(mode="json", exclude={"ground_truth"})
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
