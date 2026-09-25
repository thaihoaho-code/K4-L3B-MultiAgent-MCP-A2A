"""A2A contracts for the typed specialist pipeline on ``main``."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Generic, Protocol, TypeVar

from .mcp_gateway import EvidenceGateway
from .state import (
    ClaimAssessment,
    EntityResult,
    PaymentResult,
    PolicyResult,
    ShipmentResult,
)
from .trace import TraceWriter

ResultT = TypeVar("ResultT")
CASE_ID = re.compile(r"^[A-Z0-9][A-Z0-9_-]{2,63}$")
EVIDENCE_REF = re.compile(r"^ev_[A-Za-z0-9_-]{20,96}$")
AGENT_STATUSES = frozenset(
    {"ok", "ambiguous", "insufficient_evidence", "needs_followup", "error"}
)


def validate_evidence_refs(refs: tuple[str, ...]) -> None:
    """Validate syntax only; MCP audit must establish actual provenance later."""
    if len(refs) != len(set(refs)):
        raise ValueError("evidence_refs must be unique")
    if any(not isinstance(ref, str) or not EVIDENCE_REF.fullmatch(ref) for ref in refs):
        raise ValueError("invalid evidence_ref syntax")


@dataclass(frozen=True)
class AgentTask:
    case_id: str
    task_id: str
    from_actor: str
    to_actor: str
    task_type: str
    payload: dict[str, Any]
    evidence_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not CASE_ID.fullmatch(self.case_id):
            raise ValueError("invalid case_id in A2A task")
        if not self.task_id or not self.from_actor or not self.to_actor or not self.task_type:
            raise ValueError("A2A task identifiers are required")
        if not isinstance(self.payload, dict):
            raise TypeError("A2A task payload must be a dictionary")
        validate_evidence_refs(self.evidence_refs)


@dataclass(frozen=True)
class AgentResult(Generic[ResultT]):
    """Handoff metadata around an existing typed specialist result."""

    case_id: str
    task_id: str
    actor: str
    status: str
    data: ResultT
    evidence_refs: tuple[str, ...]
    decision_code: str
    confidence: float | None = None
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not CASE_ID.fullmatch(self.case_id):
            raise ValueError("invalid case_id in A2A result")
        if not self.task_id or not self.actor or not self.decision_code:
            raise ValueError("A2A result identifiers are required")
        if self.status not in AGENT_STATUSES:
            raise ValueError("invalid internal agent status")
        if self.confidence is not None and (
            isinstance(self.confidence, bool)
            or not isinstance(self.confidence, (int, float))
            or not math.isfinite(self.confidence)
            or not 0 <= self.confidence <= 1
        ):
            raise ValueError("confidence must be a finite number between 0 and 1")
        validate_evidence_refs(self.evidence_refs)


class EntityResolver(Protocol):
    async def resolve(
        self, case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
    ) -> EntityResult: ...


class OrderShipmentInvestigator(Protocol):
    async def investigate(
        self, entity: EntityResult, gateway: EvidenceGateway, trace: TraceWriter
    ) -> ShipmentResult: ...


class PaymentInvestigator(Protocol):
    async def check_transactions(
        self, entity: EntityResult, gateway: EvidenceGateway, trace: TraceWriter
    ) -> PaymentResult: ...


class PolicyResolver(Protocol):
    async def resolve_conflict(
        self,
        case: dict[str, Any],
        entity: EntityResult,
        shipment: ShipmentResult,
        payment: PaymentResult,
        gateway: EvidenceGateway,
        trace: TraceWriter,
    ) -> PolicyResult: ...


class OutputVerifier(Protocol):
    def verify_and_format(
        self,
        case: dict[str, Any],
        entity: EntityResult,
        shipment: ShipmentResult,
        payment: PaymentResult,
        policy: PolicyResult,
        trace: TraceWriter,
        claim_assessments: list[ClaimAssessment] | None = None,
    ) -> dict[str, Any]: ...
