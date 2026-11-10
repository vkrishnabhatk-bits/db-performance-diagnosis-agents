"""Read-only tool functions shared by the LLM agent and perception."""

from __future__ import annotations

import json

import pytest

from dbdiag import tools
from dbdiag.scenarios import fingerprint
from tests.conftest import ALL_IDS, find_key


def _call_every_tool(scenario):
    return [
        tools.get_database_metrics(scenario),
        tools.get_database_metrics(scenario, "latest"),
        tools.get_connection_pool_status(scenario),
        tools.list_queries(scenario),
        tools.inspect_query_plan(scenario),
        *[tools.inspect_query_plan(scenario, q.query_id) for q in scenario.queries],
        tools.search_database_logs(scenario),
        tools.get_index_metadata(scenario),
    ]


@pytest.mark.parametrize("sid", ALL_IDS)
def test_tools_never_expose_ground_truth(all_scenarios, sid):
    # Even when handed the full scenario, no tool output contains the answer.
    scenario = all_scenarios[sid]
    for output in _call_every_tool(scenario):
        assert not find_key(output, "ground_truth")
        assert scenario.ground_truth.notes not in json.dumps(output)


@pytest.mark.parametrize("sid", ALL_IDS)
def test_tools_are_json_serializable_and_do_not_mutate(views, sid):
    scenario = views[sid]
    before = fingerprint(scenario)
    for output in _call_every_tool(scenario):
        json.dumps(output)
        assert "error" not in output
    assert fingerprint(scenario) == before


def test_tool_function_list():
    names = [f.__name__ for f in tools.TOOL_FUNCTIONS]
    for required in ("get_database_metrics", "inspect_query_plan", "search_database_logs", "get_index_metadata"):
        assert required in names


# --------------------------------------------------------------------------- #
def test_metrics_summary_peak_traffic(views):
    out = tools.get_database_metrics(views["C"])
    summary = out["summary"]
    assert summary["windows"] == 8
    assert summary["qps_change_ratio"] == pytest.approx(3.12, abs=0.01)
    assert summary["cpu_max_pct"] == 93
    assert summary["windows_breaching_p95_slo"] == 6
    assert out["slo"]["p95_latency_ms"] == 200


def test_metrics_reports_missing_telemetry(views):
    summary = tools.get_database_metrics(views["E"])["summary"]
    assert "cpu_pct@09:05" in summary["missing_metrics"]
    assert summary["cpu_max_pct"] == 36  # only from windows that reported CPU


def test_metrics_single_window_and_bad_window(views):
    out = tools.get_database_metrics(views["A"], "10:03")
    assert [w["t"] for w in out["windows"]] == ["10:03"]
    bad = tools.get_database_metrics(views["A"], "25:00")
    assert "error" in bad and "latest" in bad["available_windows"]


def test_pool_status_saturated(views):
    summary = tools.get_connection_pool_status(views["B"])["summary"]
    assert summary["windows_fully_used"] == 6
    assert summary["max_waiting"] == 167
    assert summary["total_timeouts"] == 88
    assert summary["max_idle_in_transaction"] == 25


def test_slowest_plan_is_full_scan_with_unindexed_column(views):
    out = tools.inspect_query_plan(views["A"], "slowest")
    assert out["query_id"] == "Q1"
    assert out["plan"]["node"] == "Seq Scan"
    assert out["derived"]["is_full_scan"] is True
    assert out["derived"]["unindexed_filter_columns"] == ["customer_email"]
    assert out["derived"]["rows_examined_per_row_returned"] == 800000.0
    assert out["derived"]["table_rows"] == 2_400_000


def test_composite_index_covers_second_column(views):
    out = tools.inspect_query_plan(views["B"], "Q6")
    assert out["derived"]["unindexed_filter_columns"] == []


def test_stale_estimate_visible_in_plan(views):
    out = tools.inspect_query_plan(views["E"], "Q10")
    assert out["derived"]["estimate_error_ratio"] > 1000
    assert out["derived"]["table_last_analyzed"] == "2026-08-14"


def test_unknown_query_id(views):
    out = tools.inspect_query_plan(views["A"], "Q99")
    assert "error" in out and "Q1" in out["available_query_ids"]


def test_list_queries_ranked_by_total_time(views):
    rows = tools.list_queries(views["A"])["queries"]
    totals = [r["total_ms_per_min"] for r in rows]
    assert totals == sorted(totals, reverse=True)
    assert rows[0]["query_id"] == "Q1"


def test_search_logs_by_pattern_and_level(views):
    out = tools.search_database_logs(views["B"], "leak")
    assert out["total_matches"] == 3
    assert out["category_counts"]["connection_leak"] == 3

    errors = tools.search_database_logs(views["B"], level="ERROR")
    assert errors["total_matches"] == 3
    assert {e["level"] for e in errors["entries"]} == {"ERROR"}

    warn_plus = tools.search_database_logs(views["B"], level="warning")
    assert warn_plus["total_matches"] == 9  # WARNING + ERROR, not LOG


def test_search_logs_regex_and_invalid_regex(views):
    out = tools.search_database_logs(views["E"], "lock timeout|still waiting")
    assert out["total_matches"] == 4
    # An invalid regex falls back to a literal search instead of failing.
    literal = tools.search_database_logs(views["E"], "ShareLock (")
    assert literal["total_matches"] == 0 and "error" not in literal


def test_search_logs_limit_and_bad_level(views):
    out = tools.search_database_logs(views["C"], limit=2)
    assert out["returned"] == 2 and len(out["entries"]) == 2 and out["total_matches"] == 11
    assert "error" in tools.search_database_logs(views["C"], level="LOUD")


def test_index_metadata(views):
    out = tools.get_index_metadata(views["A"], "orders")
    (orders,) = out["tables"]
    assert orders["rows"] == 2_400_000
    assert {ix["name"] for ix in orders["indexes"]} == {"orders_pkey", "orders_created_at_idx"}
    assert all("customer_email" not in ix["columns"] for ix in orders["indexes"])

    everything = tools.get_index_metadata(views["A"])
    assert len(everything["tables"]) == 3

    bad = tools.get_index_metadata(views["A"], "nope")
    assert "error" in bad and "orders" in bad["available_tables"]


def test_logs_are_split_into_windows(views):
    scenario = views["C"]
    windows = [tools.logs_in_window(scenario, i) for i in range(len(scenario.snapshots))]
    assert sum(len(w) for w in windows) == len(scenario.logs)  # every log in exactly one window
    assert all(e.ts.startswith("18:07") for e in windows[-1])


def test_classify_log_categories(views):
    entries = {e.ts: e for e in views["E"].logs}
    assert "lock_wait" in tools.classify_log(entries["09:01:22"])
    assert "missing_telemetry" in tools.classify_log(entries["09:02:00"])
    assert "slow_query" in tools.classify_log(entries["09:04:40"])
