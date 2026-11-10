"""Percepts and symptoms for the classical agents."""

from __future__ import annotations

import dataclasses

import pytest

from dbdiag.knowledge import Thresholds
from dbdiag.perception import ALL_SYMPTOMS, perceive, percept_sequence


def test_latest_percept_symptoms(views):
    a = perceive(views["A"])
    assert {"full_scan_large_table", "unindexed_filter", "latency_slo_breach"} <= a.symptoms
    assert "pool_saturated" not in a.symptoms

    b = perceive(views["B"])
    assert {"pool_saturated", "pool_timeouts", "idle_in_transaction_high", "connection_leak_logs"} <= b.symptoms
    assert "cpu_high" not in b.symptoms and "full_scan_large_table" not in b.symptoms

    c = perceive(views["C"])
    assert {"cpu_high", "db_conn_high", "latency_slo_breach"} <= c.symptoms
    # The trap: at the peak the pool also looks saturated.
    assert "pool_saturated" in c.symptoms

    n = perceive(views["N"])
    assert n.symptoms == frozenset({"healthy"})

    e = perceive(views["E"])
    assert {"missing_metrics", "stale_statistics", "lock_waits", "p99_spike"} <= e.symptoms
    assert e.cpu_pct is None


def test_small_table_seq_scan_is_not_a_problem(views):
    n = perceive(views["N"])
    plan = next(p for p in n.plans if p.query_id == "Q12")
    assert plan.is_full_scan and not plan.is_large_table_scan


def test_sequence_shows_peak_build_up(views):
    seq = percept_sequence(views["C"])
    assert len(seq) == 8
    assert [p.window_index for p in seq] == list(range(8))
    assert seq[0].symptoms == frozenset({"healthy"})
    assert "cpu_high" not in seq[3].symptoms and "cpu_high" in seq[-1].symptoms
    first_pool = next(i for i, p in enumerate(seq) if "pool_saturated" in p.symptoms)
    first_breach = next(i for i, p in enumerate(seq) if "latency_slo_breach" in p.symptoms)
    assert first_breach < first_pool  # latency rose long before the pool queued


def test_sequence_last_equals_perceive(views):
    for scenario in views.values():
        assert percept_sequence(scenario)[-1] == perceive(scenario)


def test_window_logs_only(views):
    first = perceive(views["B"], window=0)
    assert first.t == "14:00"
    assert first.logs.count("pool_exhausted") == 1
    assert first.logs.count("connection_leak") == 0  # first leak log is at 14:01


def test_plans_ranked_and_top_plan(views):
    a = perceive(views["A"])
    assert a.top_plan.query_id == "Q1"
    totals = [p.total_ms_per_min for p in a.plans]
    assert totals == sorted(totals, reverse=True)


def test_thresholds_are_configurable(views):
    strict = Thresholds(cpu_high_pct=50)
    assert "cpu_high" in perceive(views["A"], thresholds=strict).symptoms
    assert "cpu_high" not in perceive(views["A"]).symptoms


def test_percept_is_frozen_and_has_rejects_unknown(views):
    p = perceive(views["A"])
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.qps = 0
    with pytest.raises(KeyError):
        p.has("made_up_symptom")


def test_all_symptoms_are_known(views):
    for scenario in views.values():
        for p in percept_sequence(scenario):
            assert p.symptoms <= ALL_SYMPTOMS
