"""Scenario files, fingerprints and ground-truth isolation."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from dbdiag.models import Bottleneck, Scenario
from dbdiag.knowledge import ACTIONS
from dbdiag.scenarios import (
    SCENARIO_DIR,
    fingerprint,
    list_scenarios,
    load_scenario,
    load_scenario_file,
)
from tests.conftest import ALL_IDS, REQUIRED_IDS


def test_all_scenarios_present_in_display_order(all_scenarios):
    assert tuple(all_scenarios) == ALL_IDS
    assert [s.id for s in list_scenarios()] == list(ALL_IDS)


def test_required_scenarios_and_categories_cover_assignment(all_scenarios):
    for sid in REQUIRED_IDS:
        assert sid in all_scenarios
    categories = {s.category for s in all_scenarios.values()}
    assert {"normal", "critical", "edge"} <= categories


@pytest.mark.parametrize("path", sorted(SCENARIO_DIR.glob("*.json")), ids=lambda p: p.stem)
def test_file_validates_and_name_matches_id(path):
    scenario = load_scenario_file(path)
    assert path.stem.startswith(f"{scenario.id}_")
    assert len(scenario.snapshots) >= 3
    assert scenario.queries and scenario.indexes and scenario.tables and scenario.logs


@pytest.mark.parametrize("sid", ALL_IDS)
def test_ground_truth_is_well_formed(all_scenarios, sid):
    gt = all_scenarios[sid].ground_truth
    assert gt is not None
    assert isinstance(gt.bottleneck, Bottleneck)
    unknown = set(gt.acceptable_actions) - set(ACTIONS)
    assert not unknown, f"ground truth names unknown actions {unknown}"


def test_expected_bottlenecks(all_scenarios):
    expected = {
        "A": Bottleneck.MISSING_INDEX_FULL_SCAN,
        "B": Bottleneck.CONNECTION_POOL_SATURATION,
        "C": Bottleneck.RESOURCE_SATURATION_PEAK_LOAD,
        "N": Bottleneck.HEALTHY,
        "E": Bottleneck.AMBIGUOUS,
    }
    assert {sid: s.ground_truth.bottleneck for sid, s in all_scenarios.items()} == expected


def test_unknown_scenario_lists_available():
    with pytest.raises(KeyError, match="available"):
        load_scenario("Z")


# --------------------------------------------------------------------------- #
# Fingerprint and fairness
# --------------------------------------------------------------------------- #
def test_fingerprint_is_stable_and_unique(all_scenarios):
    prints = {sid: fingerprint(s) for sid, s in all_scenarios.items()}
    assert len(set(prints.values())) == len(prints)
    for sid, s in all_scenarios.items():
        reparsed = Scenario.model_validate_json(s.model_dump_json())
        assert fingerprint(reparsed) == prints[sid]


def test_agent_view_has_same_fingerprint_without_ground_truth(all_scenarios):
    for s in all_scenarios.values():
        view = s.agent_view()
        assert view.ground_truth is None
        assert s.ground_truth is not None  # original untouched
        assert fingerprint(view) == fingerprint(s)


def test_fingerprint_changes_when_data_changes(all_scenarios):
    s = all_scenarios["A"]
    changed = s.model_copy(update={"title": s.title + " (edited)"})
    assert fingerprint(changed) != fingerprint(s)


def test_load_returns_same_object_each_time():
    assert load_scenario("A") is load_scenario("A")


# --------------------------------------------------------------------------- #
# Immutability and validation
# --------------------------------------------------------------------------- #
def test_scenario_is_frozen(all_scenarios):
    s = all_scenarios["A"]
    with pytest.raises(ValidationError):
        s.title = "changed"
    with pytest.raises(ValidationError):
        s.snapshots[0].qps = 0
    assert isinstance(s.snapshots, tuple)


def _raw(sid: str) -> dict:
    path = next(SCENARIO_DIR.glob(f"{sid}_*.json"))
    return json.loads(path.read_text())


def test_rejects_unordered_snapshots():
    data = _raw("A")
    data["snapshots"][0], data["snapshots"][1] = data["snapshots"][1], data["snapshots"][0]
    with pytest.raises(ValidationError, match="strictly increasing"):
        Scenario.model_validate(data)


def test_rejects_plan_on_unknown_table():
    data = _raw("A")
    data["queries"][0]["plan"]["table"] = "no_such_table"
    with pytest.raises(ValidationError, match="unknown table"):
        Scenario.model_validate(data)


def test_rejects_pool_counts_over_size():
    data = _raw("A")
    data["snapshots"][0]["pool"]["idle"] = 999
    with pytest.raises(ValidationError, match="exceeds size"):
        Scenario.model_validate(data)


def test_rejects_unknown_fields():
    data = _raw("A")
    data["surprise"] = 1
    with pytest.raises(ValidationError):
        Scenario.model_validate(data)


def test_missing_telemetry_is_allowed_as_null(all_scenarios):
    e = all_scenarios["E"]
    assert e.snapshots[0].cpu_pct is not None
    assert all(s.cpu_pct is None for s in e.snapshots[2:])
