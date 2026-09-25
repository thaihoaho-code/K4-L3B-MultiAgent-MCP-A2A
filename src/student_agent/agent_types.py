"""Shared, case-scoped messages exchanged by the L3B agents."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Literal

AgentStatus = Literal["ok", "ambiguous", "insufficient_evidence", "needs_followup", "error"]
STATUSES = frozenset({"ok", "ambiguous", "insufficient_evidence", "needs_followup", "error"})
EVIDENCE_REF = re.compile(r"^ev_[A-Za-z0-9_-]{20,96}$")


@dataclass(frozen=True)
class AgentTask:
    case_id: str
    task_id: str
    from_actor: str
    to_actor: str
    task_type: str
    payload: dict[str, Any] = field(default_factory=dict)
    evidence_refs: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.case_id or not self.task_id or not self.from_actor or not self.to_actor:
            raise ValueError("A2A task requires case, task and actor identifiers")
        if len(set(self.evidence_refs)) != len(self.evidence_refs):
            raise ValueError("A2A task evidence refs must be unique")
        if any(not EVIDENCE_REF.fullmatch(ref) for ref in self.evidence_refs):
            raise ValueError("A2A task contains an invalid evidence ref")


@dataclass(frozen=True)
class AgentResult:
    case_id: str
    actor: str
    status: AgentStatus
    confidence: float
    data: dict[str, Any] = field(default_factory=dict)
    evidence_refs: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    decision_code: str | None = None

    def __post_init__(self) -> None:
        if not self.case_id or not self.actor:
            raise ValueError("A2A result requires case_id and actor")
        if self.status not in STATUSES:
            raise ValueError(f"invalid agent status: {self.status}")
        if isinstance(self.confidence, bool) or not isinstance(
            self.confidence, (int, float)
        ) or not math.isfinite(
            self.confidence
        ) or not 0 <= self.confidence <= 1:
            raise ValueError("agent confidence must be a finite number from 0 to 1")
        if not isinstance(self.data, dict):
            raise TypeError("agent data must be a dictionary")
        if len(self.evidence_refs) > 20 or len(set(self.evidence_refs)) != len(
            self.evidence_refs
        ):
            raise ValueError("agent result may contain at most 20 unique evidence refs")
        if any(not EVIDENCE_REF.fullmatch(ref) for ref in self.evidence_refs):
            raise ValueError("agent result contains an invalid evidence ref")
