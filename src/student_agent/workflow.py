from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any

from .a2a import CaseGateway, dispatch, make_task, validate_handoff
from .agent_types import (
    AgentResult,
    AgentTask,
    EntityResolver,
    OrderShipmentInvestigator,
    OutputVerifier,
    PaymentInvestigator,
    PolicyResolver,
)
from .agents import EntityAgent, OrderShipmentAgent, PaymentAgent, PolicyAgent, VerifierAgent
from .mcp_gateway import EvidenceGateway
from .state import EntityResult, PaymentResult, PolicyResult, ShipmentResult
from .trace import TraceWriter


@dataclass
class CoordinatorState:
    """Per-case state for the typed pipeline."""

    case_id: str
    input_case: dict[str, Any]
    entity: EntityResult | None = None
    shipment: ShipmentResult | None = None
    payment: PaymentResult | None = None
    policy: PolicyResult | None = None
    verification: AgentResult[dict[str, Any]] | None = None
    evidence_refs: list[str] = field(default_factory=list)
    sequence: int = 0
    completed_tasks: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        if self.input_case.get("case_id") != self.case_id:
            raise ValueError("coordinator state case_id differs from input")

    def next_task(
        self, task_type: str, payload: dict[str, Any] | None = None
    ) -> AgentTask:
        self.sequence += 1
        return make_task(
            case_id=self.case_id,
            task_type=task_type,
            sequence=self.sequence,
            payload=payload,
            evidence_refs=tuple(self.evidence_refs),
        )

    def accept(self, task: AgentTask, result: AgentResult[Any]) -> None:
        """Store only one correctly correlated typed reply per assignment."""
        validate_handoff(task, result)
        if task.case_id != self.case_id:
            raise ValueError("cannot add a result from another case")
        if task.task_id in self.completed_tasks:
            raise ValueError("A2A task was already completed")
        expected_types = {
            "resolve_entity": EntityResult,
            "investigate_order_shipment": ShipmentResult,
            "investigate_payment_refund": PaymentResult,
            "decide_policy": PolicyResult,
            "verify_output": dict,
        }
        expected = expected_types[task.task_type]
        if not isinstance(result.data, expected):
            raise TypeError(f"{task.task_type} must return {expected.__name__}")
        target_field = {
            "resolve_entity": "entity",
            "investigate_order_shipment": "shipment",
            "investigate_payment_refund": "payment",
            "decide_policy": "policy",
            "verify_output": "verification",
        }[task.task_type]
        setattr(self, target_field, result if task.task_type == "verify_output" else result.data)
        self.evidence_refs = list(dict.fromkeys([*self.evidence_refs, *result.evidence_refs]))
        self.completed_tasks.add(task.task_id)


def start_case(case: dict[str, Any]) -> CoordinatorState:
    """Create an isolated state before investigating a case."""
    return CoordinatorState(case_id=case["case_id"], input_case=deepcopy(case))


def _check_entity(result: EntityResult) -> None:
    if result.resolution_status not in {"resolved", "ambiguous", "not_found"}:
        raise ValueError("entity agent returned an invalid resolution_status")
    if result.resolution_status == "resolved" and not result.resolved_order_id:
        raise ValueError("resolved entity must include resolved_order_id")
    if result.resolution_status != "resolved" and result.resolved_order_id:
        raise ValueError("unresolved entity cannot select an order")


class _ManagedTrace:
    """Suppress agent-owned coordination events; dispatch owns those events."""

    def __init__(self, writer: TraceWriter, suppressed: set[str] | None = None) -> None:
        self._writer = writer
        self._suppressed = suppressed or set()

    def emit(self, **kwargs: Any) -> dict[str, Any]:
        if kwargs.get("event_type") in self._suppressed:
            return {}
        return self._writer.emit(**kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._writer, name)


def _agent_trace(writer: TraceWriter, *event_types: str) -> _ManagedTrace:
    return _ManagedTrace(writer, {"task_assigned", "handoff", *event_types})


def _invoke_shipment(
    agent: OrderShipmentInvestigator,
    entity: EntityResult,
    gateway: CaseGateway,
    trace: _ManagedTrace,
    case_id: str,
) -> Any:
    try:
        return agent.investigate(entity, gateway, trace, case_id=case_id)
    except TypeError as exc:
        if "case_id" not in str(exc):
            raise
        return agent.investigate(entity, gateway, trace)


def _invoke_payment(
    agent: PaymentInvestigator,
    entity: EntityResult,
    gateway: CaseGateway,
    trace: _ManagedTrace,
    case: dict[str, Any],
) -> Any:
    try:
        return agent.check_transactions(
            entity,
            gateway,
            trace,
            case_id=case["case_id"],
            case=case,
        )
    except TypeError as exc:
        if "case_id" not in str(exc) and "case" not in str(exc):
            raise
        return agent.check_transactions(entity, gateway, trace)


def _check_output(output: dict[str, Any], state: CoordinatorState, trace: TraceWriter) -> None:
    """Reject output that loses or changes accepted specialist findings."""
    try:
        json.dumps(output, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("verifier output is not strict JSON") from exc
    trace.contracts.validate_output(output, "coordinator output")
    if output["case_id"] != state.case_id:
        raise ValueError("verifier returned a mismatched case_id")
    if set(output["evidence_refs"]) != set(state.evidence_refs):
        raise ValueError("output evidence_refs differ from specialist handoffs")
    entity, shipment, payment, policy = (
        state.entity, state.shipment, state.payment, state.policy
    )
    if entity is None or shipment is None or payment is None or policy is None:
        raise ValueError("verification requires all coordinator result slots")
    if entity.resolved_order_id in entity.rejected_order_ids:
        raise ValueError("resolved order cannot also be a rejected candidate")
    if (
        entity.resolved_order_id
        and shipment.order_ids
        and entity.resolved_order_id not in shipment.order_ids
    ):
        raise ValueError("shipment orders exclude the resolved order")
    if len(policy.resolution_actions) > 8:
        raise ValueError("policy actions exceed output limit; refusing silent truncation")
    for conflict in policy.data_conflicts:
        if (
            conflict.selected_source is not None
            and conflict.selected_source not in conflict.sources
        ):
            raise ValueError("selected conflict source is absent from its sources")
    financial = policy.financial_resolution
    line_total = sum(
        (Decimal(str(line.amount_brl)) for line in financial.refund_lines), Decimal(0)
    )
    if abs(line_total - Decimal(str(financial.recommended_refund_brl))) > Decimal("0.005"):
        raise ValueError("recommended refund differs from refund line total")

    expected = {
        "assessment": {
            "primary_issue": policy.primary_issue,
            "secondary_issues": policy.secondary_issues,
            "case_status": policy.case_status,
            "confidence": round(policy.confidence, 4),
        },
        "affected_entities": {
            "order_ids": shipment.order_ids,
            "item_ids": shipment.item_ids,
            "seller_ids": shipment.seller_ids,
            "payment_references": payment.payment_references,
            "shipment_ids": shipment.shipment_ids,
        },
        "entity_resolution": {
            "status": entity.resolution_status,
            "resolved_order_ids": [entity.resolved_order_id] if entity.resolved_order_id else [],
            "rejected_candidates": entity.rejected_order_ids,
            "confidence": round(entity.confidence, 4),
        },
        "customer_context": {
            "customer_unique_id": entity.customer_unique_id,
            "related_order_ids": entity.related_order_ids,
        },
        "shipment_analysis": {
            "verdict": shipment.verdict,
            "late_seller_ids": shipment.late_seller_ids,
            "timeline_complete": shipment.timeline_complete,
        },
        "payment_analysis": {
            "verdict": payment.verdict,
            "captured_total_brl": payment.captured_total_brl,
            "refunded_total_brl": payment.refunded_total_brl,
            "refundable_total_brl": payment.refundable_total_brl,
        },
        "root_cause_analysis": {
            "ranked_causes": [asdict(cause) for cause in policy.ranked_causes],
            "responsible_parties": [asdict(party) for party in policy.responsible_parties],
        },
        "data_conflicts": [asdict(conflict) for conflict in policy.data_conflicts],
        "financial_resolution": asdict(financial),
        "resolution_actions": policy.resolution_actions,
    }
    for section, value in expected.items():
        if output[section] != value:
            raise ValueError(f"verifier changed specialist result in {section}")
    claim_assessments = output.get("claim_assessments", [])
    if len({claim["claim_id"] for claim in claim_assessments}) != len(claim_assessments):
        raise ValueError("duplicate claim_id in verifier output")
    for claim in claim_assessments:
        if not set(claim["evidence_refs"]).issubset(state.evidence_refs):
            raise ValueError("claim cites evidence absent from accepted handoffs")


async def run_case(
    case: dict[str, Any],
    gateway: EvidenceGateway,
    trace: TraceWriter,
    *,
    entity_agent: EntityResolver | None = None,
    order_shipment_agent: OrderShipmentInvestigator | None = None,
    payment_agent: PaymentInvestigator | None = None,
    policy_agent: PolicyResolver | None = None,
    verifier_agent: OutputVerifier | None = None,
) -> dict[str, Any]:
    """Route actual typed agent calls in dependency order, with observable A2A."""
    state = start_case(case)
    scoped_gateway = CaseGateway(gateway, state.case_id)
    entity_agent = entity_agent or EntityAgent()
    order_shipment_agent = order_shipment_agent or OrderShipmentAgent()
    payment_agent = payment_agent or PaymentAgent()
    policy_agent = policy_agent or PolicyAgent()
    verifier_agent = verifier_agent or VerifierAgent()

    entity_task = state.next_task("resolve_entity")
    entity_reply = await dispatch(
        entity_task,
        lambda: entity_agent.resolve(
            state.input_case,
            scoped_gateway,
            _agent_trace(trace),
        ),
        EntityResult,
        lambda value: {
            "resolved": "ok",
            "ambiguous": "ambiguous",
            "not_found": "insufficient_evidence",
        }[value.resolution_status],
        lambda value: {
            "resolved": "ENTITY_RESOLVED",
            "ambiguous": "ENTITY_AMBIGUOUS",
            "not_found": "ENTITY_NOT_FOUND",
        }[value.resolution_status],
        scoped_gateway,
        trace,
        validate=_check_entity,
    )
    state.accept(entity_task, entity_reply)

    if state.entity is not None and state.entity.resolution_status == "resolved":
        scope = {"resolved_order_id": state.entity.resolved_order_id}
        shipment_task = state.next_task("investigate_order_shipment", scope)
        shipment_reply = await dispatch(
            shipment_task,
            lambda: _invoke_shipment(
                order_shipment_agent,
                state.entity,
                scoped_gateway,
                _agent_trace(trace),
                state.case_id,
            ),
            ShipmentResult,
            lambda value: (
                "insufficient_evidence" if value.verdict == "insufficient_evidence" else "ok"
            ),
            lambda value: (
                "SHIPMENT_INSUFFICIENT_EVIDENCE"
                if value.verdict == "insufficient_evidence"
                else "SHIPMENT_ANALYZED"
            ),
            scoped_gateway,
            trace,
        )
        state.accept(shipment_task, shipment_reply)

        payment_task = state.next_task("investigate_payment_refund", scope)
        payment_reply = await dispatch(
            payment_task,
            lambda: _invoke_payment(
                payment_agent,
                state.entity,
                scoped_gateway,
                _agent_trace(trace),
                state.input_case,
            ),
            PaymentResult,
            lambda value: (
                "insufficient_evidence" if value.verdict == "insufficient_evidence" else "ok"
            ),
            lambda value: (
                "PAYMENT_INSUFFICIENT_EVIDENCE"
                if value.verdict == "insufficient_evidence"
                else "PAYMENT_ANALYZED"
            ),
            scoped_gateway,
            trace,
        )
        state.accept(payment_task, payment_reply)

        policy_task = state.next_task("decide_policy", scope)
        policy_reply = await dispatch(
            policy_task,
            lambda: policy_agent.resolve_conflict(
                state.input_case,
                state.entity,
                state.shipment,
                state.payment,
                scoped_gateway,
                _agent_trace(trace, "policy_decided"),
            ),
            PolicyResult,
            lambda value: (
                "insufficient_evidence"
                if value.primary_issue == "insufficient_evidence"
                else "ok"
            ),
            lambda value: (
                "POLICY_INSUFFICIENT_EVIDENCE"
                if value.primary_issue == "insufficient_evidence"
                else "POLICY_DECIDED"
            ),
            scoped_gateway,
            trace,
        )
        state.accept(policy_task, policy_reply)
        if state.policy is not None and (
            state.policy.primary_issue != "insufficient_evidence"
            and state.policy.evidence_refs
        ):
            trace.emit(
                case_id=state.case_id,
                event_type="policy_decided",
                actor="policy-agent",
                target="coordinator",
                decision_code="POLICY_DECIDED",
                evidence_refs=state.policy.evidence_refs[:20],
            )
    else:
        # No order scope: leave these domains explicitly without evidence.
        state.shipment = ShipmentResult()
        state.payment = PaymentResult()
        state.policy = PolicyResult()

    verifier_task = state.next_task("verify_output")
    try:
        verifier_reply = await dispatch(
            verifier_task,
            lambda: verifier_agent.verify_and_format(
                state.input_case,
                state.entity,
                state.shipment,
                state.payment,
                state.policy,
                _agent_trace(trace, "verification_completed"),
            ),
            dict,
            lambda _value: "ok",
            lambda _value: "VERIFY_CONSISTENCY_PASS",
            scoped_gateway,
            trace,
            validate=lambda output: _check_output(output, state, trace),
        )
    except Exception:
        trace.emit(
            case_id=state.case_id,
            event_type="verification_completed",
            actor="verifier",
            target="coordinator",
            decision_code="VERIFY_FAIL",
            attributes={"scope": "schema_provenance_consistency", "error_count": 1},
        )
        raise
    state.accept(verifier_task, verifier_reply)
    trace.emit(
        case_id=state.case_id,
        event_type="verification_completed",
        actor="verifier",
        target="coordinator",
        decision_code="VERIFY_CONSISTENCY_PASS",
        attributes={"scope": "schema_provenance_consistency", "error_count": 0},
    )
    return verifier_reply.data


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """CLI entrypoint for Sang's coordinator."""
    return await run_case(case, gateway, trace)
