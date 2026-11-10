"""Safety guard: the assistant recommends, it never executes changes.

``check_text`` flags recommendation text that would change data or schema:

- **Destructive statements** (DROP, TRUNCATE, DELETE FROM, UPDATE ... SET,
  INSERT INTO, GRANT, REVOKE) are always flagged, unless the text clearly
  says not to do them ("do not DROP ...").
- **Schema/maintenance changes** (CREATE INDEX, ALTER TABLE/SYSTEM, REINDEX,
  VACUUM FULL, CLUSTER, ANALYZE) are allowed only as proposals: the text must
  say it is a proposal, draft, for review, or for staging, and must not say
  to run it directly in production.

Read-only diagnostics (EXPLAIN, EXPLAIN ANALYZE, pg_stat_activity, pg_locks)
are always allowed. ``EXPLAIN (ANALYZE ...)`` is not mistaken for ANALYZE.

Ambiguous keywords that are also common English words (ANALYZE, CLUSTER,
DELETE, GRANT, REVOKE, TRUNCATE) only match in upper case, which is how SQL
is written in recommendations; unambiguous multi-word forms match any case.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from dbdiag.models import AgentDecision

_DESTRUCTIVE: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bDROP\s+(TABLE|DATABASE|SCHEMA|INDEX|COLUMN|CONSTRAINT|VIEW)\b", re.I),
    re.compile(r"\bINSERT\s+INTO\b", re.I),
    re.compile(r"\bUPDATE\s+\w+(\.\w+)?\s+SET\b", re.I),
    re.compile(r"\bDELETE\s+FROM\b"),
    re.compile(r"\bTRUNCATE\b"),
    re.compile(r"\bGRANT\b"),
    re.compile(r"\bREVOKE\b"),
)

_SCHEMA_CHANGE: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bCREATE\s+(UNIQUE\s+)?INDEX\b", re.I),
    re.compile(r"\bALTER\s+(TABLE|INDEX|SYSTEM|DATABASE|ROLE)\b", re.I),
    re.compile(r"\bREINDEX\b", re.I),
    re.compile(r"\bVACUUM\s+FULL\b", re.I),
    re.compile(r"\bCLUSTER\b"),
    re.compile(r"\bANALYZE\b"),
)

# Removed before scanning so that read-only EXPLAIN ANALYZE is not flagged.
_EXPLAIN_ANALYZE = re.compile(r"\bEXPLAIN\s*(\(\s*ANALY[SZ]E[^)]*\)|ANALY[SZ]E)", re.I)

_PROPOSAL_QUALIFIER = re.compile(
    r"\b(propos\w*|draft\w*|review\w*|staging|replica|test environment|change request"
    r"|approv\w*|for (the )?DBA|do not (execute|run|apply)|don't (execute|run|apply))\b",
    re.I,
)

_DIRECT_EXECUTION = re.compile(
    r"\b(run|execute|apply)\b[^.]*\b(in|on|against)\s+(the\s+)?prod(uction)?\b"
    r"|\b(immediately|right now|directly)\b[^.]*\b(run|execute|apply)\b"
    r"|\b(run|execute|apply)\b[^.]*\b(immediately|right now|directly)\b",
    re.I,
)

_NEGATION_BEFORE = re.compile(r"\b(do not|don't|never|avoid|without|instead of)\b[^.]{0,40}$", re.I)


@dataclass(frozen=True)
class SafetyIssue:
    text: str
    rule: str  # "destructive" | "unqualified_schema_change" | "direct_execution"
    matched: str
    reason: str


class UnsafeRecommendationError(ValueError):
    def __init__(self, issues: list[SafetyIssue]):
        self.issues = issues
        details = "; ".join(f"{i.rule}: {i.matched!r} in {i.text!r}" for i in issues)
        super().__init__(f"unsafe recommendation(s): {details}")


def _is_negated(text: str, start: int) -> bool:
    return bool(_NEGATION_BEFORE.search(text[:start]))


def check_text(text: str) -> list[SafetyIssue]:
    """Safety issues in one recommendation string (empty list = safe)."""
    scan = _EXPLAIN_ANALYZE.sub("EXPLAIN", text)
    issues: list[SafetyIssue] = []

    for pattern in _DESTRUCTIVE:
        for m in pattern.finditer(scan):
            if not _is_negated(scan, m.start()):
                issues.append(
                    SafetyIssue(text, "destructive", m.group(0), "destructive statements are never recommended")
                )

    schema_matches = [
        m for pattern in _SCHEMA_CHANGE for m in pattern.finditer(scan) if not _is_negated(scan, m.start())
    ]
    if schema_matches:
        direct = any(not _is_negated(scan, m.start()) for m in _DIRECT_EXECUTION.finditer(scan))
        if direct:
            issues.extend(
                SafetyIssue(text, "direct_execution", m.group(0), "schema changes must not be run directly")
                for m in schema_matches
            )
        elif not _PROPOSAL_QUALIFIER.search(scan):
            issues.extend(
                SafetyIssue(
                    text,
                    "unqualified_schema_change",
                    m.group(0),
                    "schema changes must be phrased as a proposal for review/staging",
                )
                for m in schema_matches
            )
    return issues


def is_safe(text: str) -> bool:
    return not check_text(text)


def check_steps(steps: Iterable[str]) -> list[SafetyIssue]:
    return [issue for step in steps for issue in check_text(step)]


def check_decision(decision: AgentDecision) -> list[SafetyIssue]:
    """Issues in a decision's recommended steps and verification metric."""
    return check_steps([*decision.recommended_steps, decision.verification_metric])


def assert_safe(decision: AgentDecision) -> AgentDecision:
    """Return the decision unchanged, or raise ``UnsafeRecommendationError``."""
    issues = check_decision(decision)
    if issues:
        raise UnsafeRecommendationError(issues)
    return decision
