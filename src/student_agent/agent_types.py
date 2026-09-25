"""Shared A2A contract: actor names, task routes, decision codes and result envelopes.

Every agent receives an :class:`AgentTask` from the coordinator and answers with exactly one
:class:`AgentResult`. Results carry facts, confidence, evidence refs, warnings and a decision
code only; they never carry prompts or private reasoning.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

COORDINATOR = "coordinator"
ENTITY_AGENT = "entity-agent"
ORDER_AGENT = "order-product-agent"
SHIPMENT_AGENT = "shipment-agent"
PAYMENT_AGENT = "payment-refund-agent"
POLICY_AGENT = "policy-agent"
CONFLICT_RESOLVER = "conflict-resolver"
VERIFIER = "verifier"

ACTORS = (
    COORDINATOR,
    ENTITY_AGENT,
    ORDER_AGENT,
    SHIPMENT_AGENT,
    PAYMENT_AGENT,
    POLICY_AGENT,
    CONFLICT_RESOLVER,
    VERIFIER,
)


class Status(StrEnum):
    OK = "ok"
    AMBIGUOUS = "ambiguous"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    NEEDS_FOLLOWUP = "needs_followup"
    ERROR = "error"


@dataclass(frozen=True)
class TaskRoute:
    """Which actor owns a task type and the decision code used when it is assigned."""

    actor: str
    assign_code: str


TASK_ROUTES: dict[str, TaskRoute] = {
    "resolve_entity": TaskRoute(ENTITY_AGENT, "RESOLVE_ENTITY"),
    "investigate_order": TaskRoute(ORDER_AGENT, "INVESTIGATE_ORDER"),
    "verify_seller": TaskRoute(ORDER_AGENT, "VERIFY_SELLER"),
    "investigate_shipment": TaskRoute(SHIPMENT_AGENT, "INVESTIGATE_SHIPMENT"),
    "investigate_payment": TaskRoute(PAYMENT_AGENT, "INVESTIGATE_PAYMENT"),
    "decide_policy": TaskRoute(POLICY_AGENT, "CHECK_POLICY"),
    "resolve_conflicts": TaskRoute(CONFLICT_RESOLVER, "RESOLVE_CONFLICT"),
    "verify_output": TaskRoute(VERIFIER, "VERIFY_OUTPUT"),
}

# Least privilege: an actor may only call the MCP tools listed here.
TOOL_PERMISSIONS: dict[str, frozenset[str]] = {
    COORDINATOR: frozenset(),
    ENTITY_AGENT: frozenset({"get_customer_history", "get_order"}),
    ORDER_AGENT: frozenset({"get_order_items", "get_product_context", "get_sellers"}),
    SHIPMENT_AGENT: frozenset({"get_shipment_summary"}),
    PAYMENT_AGENT: frozenset({"get_payment_timeline", "get_order_payments", "get_refund_timeline"}),
    POLICY_AGENT: frozenset({"get_policy"}),
    CONFLICT_RESOLVER: frozenset(),
    VERIFIER: frozenset(),
}

PRIMARY_ISSUES = (
    "canceled_order_paid",
    "unavailable_order_paid",
    "late_delivery_seller",
    "late_delivery_logistics",
    "valid_split_payment",
    "payment_mismatch",
    "duplicate_charge",
    "refund_pending",
    "refund_failed",
    "unsupported_claim",
    "insufficient_evidence",
)


@dataclass(frozen=True)
class AgentTask:
    """A2A message envelope sent by the coordinator to one specialist."""

    case_id: str
    task_id: str
    from_actor: str
    to_actor: str
    task_type: str
    payload: dict[str, Any] = field(default_factory=dict)
    evidence_refs: tuple[str, ...] = ()


@dataclass
class AgentResult:
    """A2A reply envelope returned by a specialist to the coordinator."""

    case_id: str
    task_id: str
    actor: str
    status: Status
    confidence: float
    data: dict[str, Any] = field(default_factory=dict)
    evidence_refs: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    decision_code: str | None = None

    @classmethod
    def reply(
        cls,
        task: AgentTask,
        *,
        status: Status,
        confidence: float,
        decision_code: str,
        data: dict[str, Any] | None = None,
        evidence_refs: list[str] | None = None,
        warnings: list[str] | None = None,
    ) -> AgentResult:
        return cls(
            case_id=task.case_id,
            task_id=task.task_id,
            actor=task.to_actor,
            status=status,
            confidence=round(min(max(confidence, 0.0), 1.0), 4),
            data=data or {},
            evidence_refs=list(dict.fromkeys(evidence_refs or [])),
            warnings=list(dict.fromkeys(warnings or [])),
            decision_code=decision_code,
        )
