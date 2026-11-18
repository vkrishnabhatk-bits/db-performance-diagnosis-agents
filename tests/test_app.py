"""Smoke tests for the Streamlit UI (headless, via streamlit.testing.AppTest).

Ollama is not needed: the URL points at a closed port, so the LLM toggle
defaults to off and only the five classical agents run.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from dbdiag.scenarios import load_all_scenarios

APP = str(Path(__file__).resolve().parents[1] / "app.py")
SCENARIO_IDS = list(load_all_scenarios())
DEAD_OLLAMA = "http://127.0.0.1:9"


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("DBDIAG_EXPERIENCE", str(tmp_path / "experience.json"))
    at = AppTest.from_file(APP, default_timeout=60)
    at.run()
    url = next(t for t in at.text_input if t.label == "Ollama URL")
    url.set_value(DEAD_OLLAMA).run()
    return at


def _button(at, label):
    return next(b for b in at.button if b.label == label)


def test_loads_without_running_agents(app):
    assert not app.exception
    assert any("Run all agents" in i.value for i in app.info)
    toggle = next(t for t in app.toggle if t.label == "Include LLM agent")
    assert toggle.value is False  # Ollama unreachable -> LLM off by default
    assert any("not reachable" in w.value for w in app.warning)


@pytest.mark.parametrize("sid", SCENARIO_IDS)
def test_run_all_agents_renders_every_tab(app, sid):
    app.radio[0].set_value(sid).run()
    _button(app, "▶ Run all agents").click().run()
    assert not app.exception
    assert any("Fairness check" in s.value for s in app.success)
    assert len(app.metric) >= 5 + 2  # header KPIs + comparison metrics
    markdown = " ".join(m.value for m in app.markdown)
    for agent in ("Simple Reflex", "Model-Based Reflex", "Goal-Based", "Utility-Based", "Learning"):
        assert agent in markdown
    assert "Why the decisions differ" in markdown


def test_feedback_updates_experience_and_resets(app):
    _button(app, "▶ Run all agents").click().run()
    _button(app, "👍 It worked").click().run()
    assert not app.exception
    assert any("Recorded success" in m.value for m in app.markdown)
    assert any(m.label == "Successes" for m in app.metric)

    _button(app, "↺ Reset experience to seed").click().run()
    assert not app.exception
    assert not any("Recorded success" in m.value for m in app.markdown)


def test_scoreboard_covers_all_scenarios(app):
    _button(app, "Score classical agents on every scenario").click().run()
    assert not app.exception
    board = next(df.value for df in app.dataframe if "Bottleneck correct" in df.value.columns)
    assert set(SCENARIO_IDS) <= set(board.columns)
    assert len(board) == 5
