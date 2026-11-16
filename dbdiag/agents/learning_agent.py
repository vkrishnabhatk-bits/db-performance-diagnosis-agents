"""Learning agent: experience-weighted scoring that improves from feedback.

Textbook structure:

- **Performance element** - turns the percept sequence into a *symptom
  signature* (e.g. ``full_scan+high_latency``), then scores every candidate
  option ``BOTTLENECK|action`` as ``evidence_match x success_rate``, where
  ``success_rate = (s + 1) / (s + f + 2)`` (Laplace / Beta(1,1) prior). Counts
  come from the exact signature plus similar past signatures (Jaccard >= 0.5,
  weighted by similarity).
- **Critic** - after the decision, judges it against an outcome: the
  scenario's ground truth (simulated) or a user's thumbs up / down.
- **Learning element** - updates the success/failure counts in the experience
  file and appends to a history log.
- **Problem generator** - with probability ``epsilon`` it explores the
  second-best option instead of the best one, and says so in the trace.

The performance element never sees ground truth: ``decide`` receives the
agent view, and only ``critique_and_learn`` (called after deciding) reads it.
"""

from __future__ import annotations

import json
import random
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dbdiag.agents.base import BaseAgent
from dbdiag.agents.common import (
    evidence_lines,
    fraction,
    render_steps,
    safe_correlation,
    symptom_summary,
    verification_metric,
    with_prerequisites,
)
from dbdiag.knowledge import THRESHOLDS, actions_for, get_action
from dbdiag.models import AgentDecision, Bottleneck, GroundTruth, Scenario
from dbdiag.perception import Percept, percept_sequence

B = Bottleneck

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
SEED_PATH = DATA_DIR / "experience_seed.json"
EXPERIENCE_PATH = DATA_DIR / "experience.json"
SIMILARITY_MIN = 0.5
DEFAULT_EPSILON = 0.1

# Signature feature -> how evidence for each bottleneck is matched.
REQUIRED: dict[Bottleneck, str] = {
    B.MISSING_INDEX_FULL_SCAN: "full_scan",
    B.CONNECTION_POOL_SATURATION: "pool_waiting",
    B.RESOURCE_SATURATION_PEAK_LOAD: "qps_up",
    B.LOCK_CONTENTION: "lock_waits",
    B.STALE_STATISTICS: "stale_stats",
    B.HEALTHY: "healthy",
}
SUPPORTING: dict[Bottleneck, tuple[str, ...]] = {
    B.MISSING_INDEX_FULL_SCAN: ("high_latency",),
    B.CONNECTION_POOL_SATURATION: ("low_cpu", "high_latency"),
    B.RESOURCE_SATURATION_PEAK_LOAD: ("cpu_up", "conn_up", "high_latency"),
    B.LOCK_CONTENTION: ("high_latency",),
    B.STALE_STATISTICS: ("high_latency",),
    B.HEALTHY: (),
}


def option_key(bottleneck: Bottleneck, action_id: str) -> str:
    return f"{bottleneck.value}|{action_id}"


def parse_option(key: str) -> tuple[Bottleneck, str]:
    b, a = key.split("|", 1)
    return Bottleneck(b), a


# --------------------------------------------------------------------------- #
# Performance element helpers
# --------------------------------------------------------------------------- #
def signature_features(percepts: list[Percept]) -> list[str]:
    """Coarse symptom features: persistent (>= half the windows) or trend-based."""
    th = THRESHOLDS
    half = 0.5
    feats: set[str] = set()
    if fraction(percepts, "full_scan_large_table") >= half:
        feats.add("full_scan")
    if fraction(percepts, "latency_slo_breach") >= half:
        feats.add("high_latency")
    if fraction(percepts, "pool_saturated") >= half:
        feats.add("pool_waiting")
    if fraction(percepts, "lock_waits") >= half:
        feats.add("lock_waits")
    if fraction(percepts, "stale_statistics") >= half:
        feats.add("stale_stats")
    if any(p.missing_metrics for p in percepts):
        feats.add("missing_metric")
    if all(p.has("healthy") for p in percepts):
        feats.add("healthy")

    cpus = [p.cpu_pct for p in percepts if p.cpu_pct is not None]
    if cpus and max(cpus) < 50:
        feats.add("low_cpu")
    first, last = percepts[0], percepts[-1]
    if first.qps and last.qps / first.qps >= th.qps_rise_ratio:
        feats.add("qps_up")
    if len(cpus) >= 3 and cpus[-1] - cpus[0] >= 20 and safe_correlation(
        [p.qps for p in percepts], [p.cpu_pct for p in percepts]
    ) >= th.trend_correlation_min:
        feats.add("cpu_up")
    if last.db_conn_utilization - first.db_conn_utilization >= 0.3:
        feats.add("conn_up")
    return sorted(feats)


def evidence_match(features: set[str]) -> dict[Bottleneck, float]:
    match: dict[Bottleneck, float] = {}
    for b, req in REQUIRED.items():
        if req not in features:
            match[b] = 0.0
            continue
        sup = SUPPORTING[b]
        match[b] = 1.0 if not sup else 0.6 + 0.4 * sum(s in features for s in sup) / len(sup)
    competing = sum(1 for b, m in match.items() if b != B.HEALTHY and m > 0)
    match[B.AMBIGUOUS] = 0.5 * ("missing_metric" in features) + 0.5 * (competing >= 2)
    return match


def jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a | b else 1.0


@dataclass(frozen=True)
class Candidate:
    option: str
    bottleneck: Bottleneck
    action: str
    match: float
    successes: float
    failures: float
    rate: float
    score: float


# --------------------------------------------------------------------------- #
class LearningAgent(BaseAgent):
    name = "Learning"
    mechanism = "Scores (bottleneck, action) options by evidence match x success rate learned from past outcomes."

    def __init__(
        self,
        experience_path: Path | str = EXPERIENCE_PATH,
        seed_path: Path | str = SEED_PATH,
        epsilon: float = DEFAULT_EPSILON,
        rng_seed: int | None = None,
    ):
        self.experience_path = Path(experience_path)
        self.seed_path = Path(seed_path)
        self.epsilon = epsilon
        self.rng = random.Random(rng_seed)
        self.experience = self._load()

    # ---- experience store -------------------------------------------------
    def _empty(self) -> dict[str, Any]:
        return {"counts": {}, "history": []}

    def _load(self) -> dict[str, Any]:
        if not self.experience_path.exists():
            self.reset()
        try:
            data = json.loads(self.experience_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = self._empty()
        data.setdefault("counts", {})
        data.setdefault("history", [])
        return data

    def save(self) -> None:
        self.experience_path.parent.mkdir(parents=True, exist_ok=True)
        self.experience_path.write_text(json.dumps(self.experience, indent=2, sort_keys=True), encoding="utf-8")

    def reset(self) -> None:
        """Restore the experience file from the committed seed."""
        self.experience_path.parent.mkdir(parents=True, exist_ok=True)
        if self.seed_path.exists():
            shutil.copyfile(self.seed_path, self.experience_path)
        else:
            self.experience_path.write_text(json.dumps(self._empty()), encoding="utf-8")
        self.experience = json.loads(self.experience_path.read_text(encoding="utf-8"))
        self.experience.setdefault("counts", {})
        self.experience.setdefault("history", [])

    def counts_for(self, signature: str, option: str) -> tuple[float, float, list[str]]:
        """Similarity-weighted (successes, failures) and which stored signatures contributed."""
        sig = set(signature.split("+")) if signature else set()
        s = f = 0.0
        used: list[str] = []
        for stored, options in self.experience["counts"].items():
            sim = jaccard(sig, set(stored.split("+")) if stored else set())
            if sim < SIMILARITY_MIN or option not in options:
                continue
            s += sim * options[option].get("success", 0)
            f += sim * options[option].get("failure", 0)
            used.append(f"{stored} (sim {sim:.2f})")
        return s, f, used

    # ---- performance element ---------------------------------------------
    def rank(self, signature: str, features: set[str]) -> list[Candidate]:
        match = evidence_match(features)
        cands = []
        for b, m in match.items():
            if m <= 0:
                continue
            for a in actions_for(b):
                key = option_key(b, a.id)
                s, f, _ = self.counts_for(signature, key)
                rate = (s + 1) / (s + f + 2)
                cands.append(Candidate(key, b, a.id, m, s, f, rate, m * rate))
        cands.sort(key=lambda c: (-c.score, -get_action(c.action).benefit_for(c.bottleneck), c.option))
        return cands

    def decide(self, scenario: Scenario) -> AgentDecision:
        percepts = percept_sequence(scenario)
        features = signature_features(percepts)
        signature = "+".join(features) or "none"
        feats = set(features)
        ranked = self.rank(signature, feats)
        trace = [f"Signature: {signature}"]
        trace.append(
            "Evidence match: " + ", ".join(f"{b.value}={m:.2f}" for b, m in evidence_match(feats).items() if m > 0)
        )
        for c in ranked[:4]:
            trace.append(
                f"  {c.option}: match {c.match:.2f} x rate {c.rate:.2f} "
                f"({c.successes:.1f} successes / {c.failures:.1f} failures) = {c.score:.3f}"
            )

        best = ranked[0]
        chosen, explored = best, False
        if len(ranked) > 1 and self.epsilon > 0 and self.rng.random() < self.epsilon:
            chosen, explored = ranked[1], True
            trace.append(f"Problem generator: exploring second-best {chosen.option} instead of {best.option} (epsilon={self.epsilon})")

        # How did experience change the ranking compared with the prior alone?
        prior = sorted(ranked, key=lambda c: (-c.match, -get_action(c.action).benefit_for(c.bottleneck), c.option))
        runner_up = next((c for c in ranked if c.option != chosen.option), None)
        _, _, used = self.counts_for(signature, chosen.option)
        explanation = (
            f"{chosen.action} has {chosen.successes:.1f} successes / {chosen.failures:.1f} failures "
            f"(rate {chosen.rate:.2f}) for signature {signature}"
            + (f" from {', '.join(used)}" if used else " (no matching history: prior 0.50)")
        )
        if runner_up:
            explanation += f"; next best {runner_up.option} rate {runner_up.rate:.2f}"
        if prior[0].option != best.option:
            explanation += f". Experience changed the choice: the prior alone would pick {prior[0].option}"
        else:
            explanation += ". Experience agrees with the prior ranking"
        trace.append(explanation)

        actions = with_prerequisites(chosen.action)
        latest = percepts[-1]
        bottleneck = chosen.bottleneck
        return self.make_decision(
            scenario,
            symptom_summary=symptom_summary(latest, bottleneck, f"Signature {signature}"),
            evidence=[f"Symptom signature: {signature}", f"Experience: {explanation}"] + evidence_lines(latest),
            bottleneck=bottleneck,
            recommended_steps=render_steps(actions, latest),
            verification_metric=verification_metric(bottleneck, latest),
            confidence=chosen.score,
            reasoning_trace=trace,
            extras={
                "action_ids": actions,
                "signature": signature,
                "option": chosen.option,
                "chosen_action": chosen.action,
                "explored": explored,
                "success_rate": round(chosen.rate, 3),
                "counts_used": {"success": round(chosen.successes, 2), "failure": round(chosen.failures, 2)},
                "ranking": [
                    {"option": c.option, "match": round(c.match, 2), "rate": round(c.rate, 3), "score": round(c.score, 3)}
                    for c in ranked[:8]
                ],
                "mechanism_detail": f"{chosen.option} rate {chosen.rate:.2f}" + (" (explored)" if explored else ""),
                "why": explanation + (" (exploration step)" if explored else ""),
            },
        )

    # ---- critic and learning element -------------------------------------
    @staticmethod
    def critique(decision: AgentDecision, ground_truth: GroundTruth) -> tuple[bool, str]:
        action = decision.extras.get("chosen_action")
        ok_b = decision.bottleneck == ground_truth.bottleneck
        ok_a = action in ground_truth.acceptable_actions
        reason = (
            f"bottleneck {decision.bottleneck.value} {'==' if ok_b else '!='} {ground_truth.bottleneck.value}; "
            f"action {action} {'is' if ok_a else 'is not'} acceptable"
        )
        return ok_b and ok_a, reason

    def learn(self, signature: str, option: str, success: bool, *, scenario_id: str, source: str) -> dict[str, Any]:
        bucket = self.experience["counts"].setdefault(signature, {}).setdefault(option, {"success": 0, "failure": 0})
        before = dict(bucket)
        bucket["success" if success else "failure"] += 1
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "scenario": scenario_id,
            "signature": signature,
            "option": option,
            "outcome": "success" if success else "failure",
            "source": source,
            "before": before,
            "after": dict(bucket),
        }
        self.experience["history"].append(entry)
        self.save()
        return entry

    def feedback(self, decision: AgentDecision, success: bool, source: str = "user") -> dict[str, Any]:
        return self.learn(
            decision.extras["signature"], decision.extras["option"], success,
            scenario_id=decision.scenario_id, source=source,
        )

    def critique_and_learn(self, decision: AgentDecision, ground_truth: GroundTruth) -> dict[str, Any]:
        success, reason = self.critique(decision, ground_truth)
        entry = self.feedback(decision, success, source="critic (ground truth)")
        entry["reason"] = reason
        return entry
