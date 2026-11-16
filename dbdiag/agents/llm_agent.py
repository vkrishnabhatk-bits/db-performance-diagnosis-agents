"""LLM agent: a local Qwen3 model (Ollama via LangChain) that calls read-only tools.

- ``make_tools(scenario)`` wraps the functions in ``dbdiag.tools`` as LangChain
  tools, closed over the *same frozen scenario object* the other agents get.
- A **manual tool-calling loop** (at most ``max_iterations`` model turns)
  records every call: name, arguments, result and latency.
- The model must call **at least two distinct tools** and look at each
  evidence group (system metrics, queries/plans/indexes, logs) before it
  concludes. If it answers earlier it is re-prompted (at most
  ``MAX_REPROMPTS`` times) with the groups it skipped; a decision that still
  uses fewer than two tools is marked ``grounded=False``.
- Recommended steps are reordered so that every change comes after the
  diagnostic its preconditions need (the same rule the other agents follow);
  any reordering is reported in the trace.
- The final answer is a JSON object; one repair retry is made if it does not
  parse.
- **Grounding check:** every number and identifier in ``evidence`` is looked
  up in the recorded tool outputs. ``extras.grounding`` lists each item as
  verified or not, and ``extras.grounding_score`` is the verified fraction.
- **Safety:** unsafe recommendations are removed (and reported) before the
  shared ``assert_safe`` guard runs.
- **Ollama unavailable:** ``LLMUnavailableError`` is raised with setup
  instructions. No substitute answer is produced.

The model is not shown the scenario title or description (they name the
answer); like the classical agents it only sees what the tools return.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Literal

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, Field, ValidationError

from dbdiag import tools as T
from dbdiag.agents.base import BaseAgent
from dbdiag.agents.common import step_text, with_prerequisites
from dbdiag.knowledge import ACTIONS, VERIFICATION_METRICS
from dbdiag.models import AgentDecision, Bottleneck, Scenario
from dbdiag.safety import check_text

DEFAULT_MODEL = "qwen3:8b"
DEFAULT_BASE_URL = "http://localhost:11434"
MIN_DISTINCT_TOOLS = 2
MAX_REPROMPTS = 2
# An investigation must touch every group before the answer is accepted.
EVIDENCE_GROUPS: dict[str, frozenset[str]] = {
    "system metrics (get_database_metrics / get_connection_pool_status)": frozenset(
        {"get_database_metrics", "get_connection_pool_status"}
    ),
    "query plans or indexes (inspect_query_plan / get_index_metadata)": frozenset(
        {"inspect_query_plan", "get_index_metadata"}
    ),
    "logs (search_database_logs)": frozenset({"search_database_logs"}),
}
MAX_RESULT_CHARS = 6000  # per tool message sent back to the model


class LLMUnavailableError(RuntimeError):
    """Ollama is not running or the model is not pulled."""


class LLMOutputError(RuntimeError):
    """The model did not return a usable answer."""


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #
def make_tools(scenario: Scenario) -> list[BaseTool]:
    """Read-only LangChain tools bound to ``scenario`` (which must be an agent view)."""

    @tool
    def get_database_metrics(window: str = "all") -> dict:
        """Latency percentiles (p50/p95/p99), QPS, CPU, memory, I/O wait, cache hit ratio,
        DB connections, pool utilization and lock waits per one-minute window, plus a summary
        with trends. window: "all", "latest" or a time such as "10:03"."""
        return T.get_database_metrics(scenario, window)

    @tool
    def get_connection_pool_status(window: str = "all") -> dict:
        """Application connection-pool counters per window: size, active, idle, waiting,
        average wait (ms), acquire timeouts and idle-in-transaction sessions.
        window: "all", "latest" or a time such as "10:03"."""
        return T.get_connection_pool_status(scenario, window)

    @tool
    def list_queries() -> dict:
        """All tracked queries ordered by total time per minute, with their plan node and table."""
        return T.list_queries(scenario)

    @tool
    def inspect_query_plan(query_id: str = "slowest") -> dict:
        """Execution plan for one query: node type, rows examined/returned, planner estimate,
        cost, index used, unindexed filter columns and table statistics.
        query_id: an id such as "Q1", or "slowest" for the highest total time."""
        return T.inspect_query_plan(scenario, query_id)

    @tool
    def search_database_logs(pattern: str = "", level: str = "ANY") -> dict:
        """Search database/application logs with a case-insensitive regex. level is a minimum
        severity: ANY, DEBUG, INFO, LOG, WARNING, ERROR or FATAL. Returns at most 20 entries."""
        return T.search_database_logs(scenario, pattern, level)

    @tool
    def get_index_metadata(table: str = "all") -> dict:
        """Indexes (columns, size, scan counts) and table statistics (rows, last analyzed)
        for one table, or "all" tables."""
        return T.get_index_metadata(scenario, table)

    return [
        get_database_metrics,
        get_connection_pool_status,
        list_queries,
        inspect_query_plan,
        search_database_logs,
        get_index_metadata,
    ]


# --------------------------------------------------------------------------- #
# Prompts
# --------------------------------------------------------------------------- #
def _catalogue() -> str:
    return "\n".join(f"- {a.id} ({a.kind}): {a.title}" for a in ACTIONS.values())


SYSTEM_PROMPT = f"""You are a database performance diagnosis assistant. You investigate a slow
database-backed application using ONLY the read-only tools provided. You never execute anything:
schema or configuration changes are proposals for review in staging, never commands to run.

Work in two phases.
PHASE 1 - INVESTIGATE: call tools; do not answer yet. You must look at all three evidence groups:
  (a) system metrics: get_database_metrics and/or get_connection_pool_status with window "all"
      (to see trends, not just one window),
  (b) query plans: inspect_query_plan (e.g. query_id "slowest") and/or get_index_metadata
      (list_queries shows which queries exist, but not their plans),
  (c) logs: search_database_logs (e.g. level "WARNING").
  Follow up on anything suspicious (a Seq Scan, a large estimate error, lock or pool errors).
PHASE 2 - ANSWER: only after investigating, reply with the JSON object described below.

Rules:
2. Every evidence item must quote concrete values (numbers, query ids, table names, plan nodes, log
   text) exactly as the tools returned them. Do not invent values.
3. If telemetry is missing or signals conflict, say so, choose AMBIGUOUS and lower your confidence.
   Rising QPS with rising CPU means load; pool waits with low CPU mean the pool; a frequent Seq Scan
   over a large table means a missing index. Decide from the evidence, not from one metric.
4. Choose recommended steps from this catalogue of safe actions, diagnostics first. Write each step
   as "<action_id>: <short reason>":
{_catalogue()}

When you have investigated, reply with ONLY a JSON object (no markdown) with these keys:
{{
  "symptom_summary": "one or two sentences",
  "evidence": ["concrete fact with values from the tools", "..."],
  "bottleneck": one of {[b.value for b in Bottleneck]},
  "recommended_steps": ["action_id: reason", "..."],
  "verification_metric": "what to measure to confirm the fix worked",
  "confidence": a number between 0 and 1
}}"""


def user_prompt(scenario: Scenario) -> str:
    return (
        f"Assess database telemetry set {scenario.id}. The SLO is p95 latency <= "
        f"{scenario.slo.p95_latency_ms:.0f} ms and connection-pool wait <= {scenario.slo.pool_wait_ms:.0f} ms. "
        "Use the tools to decide whether the system is healthy or which bottleneck is most likely. "
        "Do not assume there is a problem: HEALTHY is a valid answer when every metric meets the SLO."
    )


class LLMAnswer(BaseModel):
    """Required shape of the model's final answer (also used for schema-constrained repair)."""

    symptom_summary: str
    evidence: list[str] = Field(min_length=1)
    bottleneck: Literal[tuple(b.value for b in Bottleneck)]  # type: ignore[valid-type]
    recommended_steps: list[str] = Field(min_length=1, description='each "<action_id>: <short reason>"')
    verification_metric: str
    confidence: float = Field(ge=0, le=1)


def validate_answer(data: dict[str, Any] | None) -> tuple[dict[str, Any] | None, str]:
    """(validated answer, problem). Bottleneck names are upper-cased before validation."""
    if data is None:
        return None, "not a JSON object"
    data = dict(data)
    if isinstance(data.get("bottleneck"), str):
        data["bottleneck"] = data["bottleneck"].strip().upper()
    try:
        return LLMAnswer.model_validate(data).model_dump(), ""
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(map(str, e['loc'])) or 'answer'}: {e['msg']}" for e in exc.errors())
        return None, problems


# --------------------------------------------------------------------------- #
# Parsing and grounding
# --------------------------------------------------------------------------- #
_THINK = re.compile(r"<think>.*?</think>", re.S)


def message_text(msg: BaseMessage) -> str:
    content = msg.content
    if isinstance(content, list):
        content = "".join(part.get("text", "") if isinstance(part, dict) else str(part) for part in content)
    return _THINK.sub("", content or "").strip()


def parse_json_answer(text: str) -> dict[str, Any] | None:
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


_NUMBER = re.compile(r"(?<![A-Za-z_\d.])(\d[\d,]*(?:\.\d+)?)(?:\s?([kKmM])(?![A-Za-z]))?")
# Identifiers worth checking: query ids (Q7), snake_case names (customer_email),
# hyphenated names (billing-export) and CamelCase class names (SQLTransientConnectionException).
_IDENT = re.compile(
    r"\b(?:Q\d+|[a-z]+(?:_[a-z0-9]+)+|[a-z]+(?:-[a-z0-9]+)+"
    r"|(?=[A-Za-z0-9]*[a-z])(?=[A-Za-z0-9]*[A-Z][A-Za-z0-9]*[A-Z])[A-Z][A-Za-z0-9]+)\b"
)
_PLAN_NODES = ("seq scan", "index scan", "index only scan", "bitmap heap scan", "nested loop", "hash join")


def extract_numbers(text: str) -> list[float]:
    out = []
    for digits, suffix in _NUMBER.findall(text):
        try:
            value = float(digits.replace(",", ""))
        except ValueError:
            continue
        if suffix:
            value *= 1_000 if suffix.lower() == "k" else 1_000_000
        out.append(value)
    return out


def _walk_numbers(obj: Any, out: set[float]) -> None:
    if isinstance(obj, bool) or obj is None:
        return
    if isinstance(obj, (int, float)):
        out.add(float(obj))
        if 0 < abs(obj) <= 1:
            out.add(round(float(obj) * 100, 1))  # ratios quoted as percentages
    elif isinstance(obj, str):
        out.update(extract_numbers(obj))
    elif isinstance(obj, dict):
        for k, v in obj.items():
            _walk_numbers(k, out)
            _walk_numbers(v, out)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _walk_numbers(v, out)


def _number_found(value: float, known: set[float]) -> bool:
    return any(abs(value - k) <= max(0.01 * abs(k), 0.5 if k >= 10 else 0.005) for k in known)


def _ident_found(ident: str, corpus: str) -> bool:
    """Case-insensitive; '-', '_' and ' ' are treated as the same separator."""
    base = ident.lower()
    variants = {base, base.replace("-", "_"), base.replace("-", " "), base.replace("_", " ")}
    return any(v in corpus for v in variants)


def verify_evidence(evidence: list[str], tool_results: list[Any], given_text: str = "") -> list[dict[str, Any]]:
    """Check each evidence item's numbers and identifiers against what the model was given.

    Sources are the tool outputs plus ``given_text`` (the task prompt, e.g. the SLO values).
    """
    known: set[float] = set()
    _walk_numbers([tool_results, given_text], known)
    corpus = (json.dumps(tool_results, default=str) + " " + given_text).lower()
    report = []
    for item in evidence:
        numbers = extract_numbers(item)
        idents = set(_IDENT.findall(item)) | {n for n in _PLAN_NODES if n in item.lower()}
        missing = [n for n in numbers if not _number_found(n, known)]
        missing += sorted(i for i in idents if not _ident_found(i, corpus))
        checked = len(numbers) + len(idents)
        report.append({
            "item": item,
            "verified": checked > 0 and not missing,
            "checked_tokens": checked,
            "unverified_tokens": [str(int(m)) if isinstance(m, float) and m.is_integer() else str(m) for m in missing],
        })
    return report


_ACTION_PREFIX = re.compile(r"^\s*\[?([a-z_]+)\]?\s*[:\-]?\s*(.*)$", re.S)


def map_step(step: str) -> tuple[str | None, str]:
    """(action_id or None, rendered step text)."""
    m = _ACTION_PREFIX.match(step)
    if m and m.group(1) in ACTIONS:
        reason = m.group(2).strip()
        return m.group(1), step_text(m.group(1)) + (f" Model's reason: {reason}" if reason else "")
    return None, step.strip()


def _step_action(text: str) -> str | None:
    m = re.match(r"^\[([a-z_]+)\]", text)
    return m.group(1) if m and m.group(1) in ACTIONS else None


# --------------------------------------------------------------------------- #
# Agent
# --------------------------------------------------------------------------- #
class LLMAgent(BaseAgent):
    name = "LLM (Ollama + LangChain)"
    mechanism = "A local Qwen3 model calls read-only tools in a loop; its evidence is checked against the tool outputs."

    def __init__(
        self,
        model_name: str = DEFAULT_MODEL,
        base_url: str = DEFAULT_BASE_URL,
        temperature: float = 0.0,
        max_iterations: int = 8,
        think: bool = False,
        llm: Any | None = None,
    ):
        self.model_name = model_name
        self.base_url = base_url
        self.temperature = temperature
        self.max_iterations = max_iterations
        self.think = think  # Qwen3 thinking mode: better reasoning, several times slower
        self._llm = llm  # injected chat model (tests); otherwise ChatOllama

    def _chat_model(self):
        if self._llm is not None:
            return self._llm
        from langchain_ollama import ChatOllama

        return ChatOllama(
            model=self.model_name,
            base_url=self.base_url,
            temperature=self.temperature,
            reasoning=self.think,
        )

    def _invoke(self, model, messages: list[BaseMessage]) -> AIMessage:
        try:
            return model.invoke(messages)
        except Exception as exc:  # connection refused, model not pulled, ...
            text = f"{type(exc).__name__}: {exc}"
            if any(s in text.lower() for s in ("connect", "refused", "not found", "pull", "responseerror", "timeout")):
                raise LLMUnavailableError(
                    f"Ollama is not reachable at {self.base_url} or model '{self.model_name}' is not available "
                    f"({text}). Start it with `ollama serve` and run `ollama pull {self.model_name}`."
                ) from exc
            raise

    def _repair(self, chat, messages: list[BaseMessage]) -> tuple[dict[str, Any] | None, str]:
        """One repair call. Uses schema-constrained output when the model supports it."""
        structured = getattr(chat, "with_structured_output", None)
        if structured is not None and self._llm is None:
            try:
                result = self._invoke(structured(LLMAnswer, method="json_schema"), messages)
            except LLMUnavailableError:
                raise
            except Exception as exc:  # schema output failed: fall back to text
                result = None
                messages.append(AIMessage(content=f"(structured output failed: {exc})"))
            if isinstance(result, LLMAnswer):
                data = result.model_dump()
                return data, json.dumps(data)
        ai = self._invoke(chat, messages)
        text = message_text(ai)
        return parse_json_answer(text), text

    def decide(self, scenario: Scenario) -> AgentDecision:
        tools = make_tools(scenario)
        by_name = {t.name: t for t in tools}
        chat = self._chat_model()
        bound = chat.bind_tools(tools)

        messages: list[BaseMessage] = [SystemMessage(SYSTEM_PROMPT), HumanMessage(user_prompt(scenario))]
        calls: list[dict[str, Any]] = []
        trace: list[str] = [f"Model {self.model_name}, {len(tools)} read-only tools, max {self.max_iterations} turns"]
        reprompts = 0
        final_text: str | None = None
        iterations = 0

        for iterations in range(1, self.max_iterations + 1):
            ai = self._invoke(bound, messages)
            messages.append(ai)
            if ai.tool_calls:
                for tc in ai.tool_calls:
                    t0 = time.perf_counter()
                    if tc["name"] in by_name:
                        try:
                            result = by_name[tc["name"]].invoke(tc.get("args") or {})
                        except Exception as exc:  # bad arguments from the model
                            result = {"error": f"{type(exc).__name__}: {exc}"}
                    else:
                        result = {"error": f"unknown tool {tc['name']!r}", "available": list(by_name)}
                    ms = round((time.perf_counter() - t0) * 1000, 2)
                    calls.append({"name": tc["name"], "args": tc.get("args") or {}, "result": result, "latency_ms": ms})
                    trace.append(f"Turn {iterations}: {tc['name']}({json.dumps(tc.get('args') or {})}) -> {ms} ms")
                    content = json.dumps(result, default=str)[:MAX_RESULT_CHARS]
                    messages.append(ToolMessage(content=content, tool_call_id=tc.get("id") or tc["name"]))
                continue

            distinct = {c["name"] for c in calls if "error" not in c["result"]}
            missing = [g for g, names in EVIDENCE_GROUPS.items() if not names & distinct]
            if (len(distinct) < MIN_DISTINCT_TOOLS or missing) and reprompts < MAX_REPROMPTS:
                reprompts += 1
                trace.append(
                    f"Turn {iterations}: answered after {len(distinct)} distinct tool(s), missing "
                    f"{len(missing)} evidence group(s); re-prompt {reprompts}/{MAX_REPROMPTS}"
                )
                messages.append(HumanMessage(
                    f"Do not answer yet. You have used {len(distinct)} distinct tool(s) "
                    f"({', '.join(sorted(distinct)) or 'none'}). Call at least {MIN_DISTINCT_TOOLS} different "
                    f"tools and look at every evidence group. Not yet checked: {'; '.join(missing) or 'none'}. "
                    "Call those tools now; answer with the JSON object only after reading their results."
                ))
                continue
            final_text = message_text(ai)
            break

        if final_text is None:
            trace.append("Turn limit reached; asking for the final answer")
            messages.append(HumanMessage("Stop calling tools now and reply with only the final JSON object."))
            ai = self._invoke(chat, messages)
            messages.append(ai)
            final_text = message_text(ai)

        answer, problem = validate_answer(parse_json_answer(final_text))
        if answer is None:
            trace.append(f"Answer did not match the required JSON ({problem[:160]}); one repair retry")
            messages.append(HumanMessage(
                "Your answer does not match the required format (" + problem[:300] + "). Keep your diagnosis "
                "but reply with ONLY a JSON object with exactly these keys: symptom_summary, evidence (list), "
                f"bottleneck (one of {[b.value for b in Bottleneck]}), recommended_steps (list of "
                '"<action_id>: <reason>"), verification_metric, confidence (0-1).'
            ))
            answer, final_text = self._repair(chat, messages)
            answer, problem = validate_answer(answer)
        if answer is None:
            raise LLMOutputError(f"model did not return a valid answer after one repair retry ({problem}): {final_text[:300]!r}")

        return self._to_decision(scenario, answer, calls, trace, reprompts, iterations, final_text)

    @staticmethod
    def _diagnostics_first(steps: list[str], action_ids: list[str], trace: list[str]) -> tuple[list[str], list[str]]:
        """Insert (or move up) the diagnostics a change needs, so no change precedes its diagnostic."""
        items: list[tuple[str | None, str]] = []  # (action id or None, step text), in order
        for text in steps:
            action_id = _step_action(text)
            if action_id is None:
                items.append((None, text))
                continue
            if any(a == action_id for a, _ in items):
                continue  # already present (inserted earlier as a prerequisite)
            provided = {f for a, _ in items if a for f in ACTIONS[a].effects}
            for pre in with_prerequisites(action_id)[:-1]:
                if ACTIONS[pre].effects <= provided:
                    continue
                later = [(a, t) for a, t in items if a == pre]
                items = [(a, t) for a, t in items if a != pre]
                items.append(later[0] if later else (pre, step_text(pre)))
                provided |= ACTIONS[pre].effects
                trace.append(f"Diagnostics first: inserted {pre} before {action_id}")
            items.append((action_id, text))
        # Diagnostics listed *after* the change that needs them were handled above by
        # inserting them first; drop their later duplicates.
        seen: set[str] = set()
        ordered: list[tuple[str | None, str]] = []
        for a, t in items:
            if a is not None and a in seen:
                continue
            if a is not None:
                seen.add(a)
            ordered.append((a, t))
        return [t for _, t in ordered], [a for a, _ in ordered if a is not None]

    # ---------------------------------------------------------------------- #
    def _to_decision(self, scenario, answer, calls, trace, reprompts, iterations, raw) -> AgentDecision:
        distinct = sorted({c["name"] for c in calls if "error" not in c["result"]})
        grounded = len(distinct) >= MIN_DISTINCT_TOOLS
        covered = [g.split(" (")[0] for g, names in EVIDENCE_GROUPS.items() if names & set(distinct)]

        try:
            bottleneck = Bottleneck(str(answer.get("bottleneck", "")).strip().upper())
        except ValueError:
            trace.append(f"Unknown bottleneck {answer.get('bottleneck')!r}; recorded as AMBIGUOUS")
            bottleneck = Bottleneck.AMBIGUOUS

        evidence = [str(e) for e in answer.get("evidence") or [] if str(e).strip()]
        grounding = verify_evidence(evidence, [c["result"] for c in calls], given_text=user_prompt(scenario))
        score = sum(g["verified"] for g in grounding) / len(grounding) if grounding else 0.0
        trace.append(f"Grounding: {sum(g['verified'] for g in grounding)}/{len(grounding)} evidence items verified against tool outputs")

        steps, action_ids, removed = [], [], []
        for raw_step in answer.get("recommended_steps") or []:
            action_id, text = map_step(str(raw_step))
            if check_text(text):
                removed.append(text)
                continue
            steps.append(text)
            if action_id:
                action_ids.append(action_id)
        if removed:
            trace.append(f"Safety: removed {len(removed)} unsafe recommendation(s)")
        steps, action_ids = self._diagnostics_first(steps, action_ids, trace)
        if not steps:
            steps = ["No safe recommendation was returned by the model; review the evidence manually."]

        metric = str(answer.get("verification_metric") or "").strip() or VERIFICATION_METRICS[bottleneck]
        if check_text(metric):
            removed.append(metric)
            metric = VERIFICATION_METRICS[bottleneck]

        try:
            model_conf = float(answer.get("confidence", 0.5))
        except (TypeError, ValueError):
            model_conf = 0.5
        model_conf = max(0.0, min(1.0, model_conf))
        confidence = model_conf * (0.5 + 0.5 * score) * (1.0 if grounded else 0.5)
        trace.append(
            f"Confidence: model {model_conf:.2f} x grounding factor {0.5 + 0.5 * score:.2f}"
            + ("" if grounded else " x 0.5 (fewer than 2 distinct tools)")
            + f" = {confidence:.2f}"
        )

        return self.make_decision(
            scenario,
            symptom_summary=str(answer.get("symptom_summary") or "").strip() or "(no summary returned)",
            evidence=evidence,
            bottleneck=bottleneck,
            recommended_steps=steps,
            verification_metric=metric,
            confidence=confidence,
            reasoning_trace=trace,
            extras={
                "action_ids": action_ids,
                "model": self.model_name,
                "tool_calls": calls,
                "distinct_tools": distinct,
                "grounded": grounded,
                "grounding": grounding,
                "grounding_score": round(score, 3),
                "model_confidence": model_conf,
                "reprompted": reprompts > 0,
                "reprompts": reprompts,
                "evidence_groups_covered": covered,
                "iterations": iterations,
                "unsafe_removed": removed,
                "raw_answer": raw,
                "mechanism_detail": f"{len(calls)} tool calls ({len(distinct)} distinct), grounding {score:.2f}",
                "why": (
                    f"the model read {', '.join(distinct) or 'no tools'} and concluded {bottleneck.value}; "
                    f"{sum(g['verified'] for g in grounding)}/{len(grounding)} of its evidence items match tool outputs"
                ),
            },
        )
