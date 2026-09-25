"""Sang-owned coordinator skeleton for the Phase 2 specialist implementations."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .a2a import AgentHandler, dispatch, make_task
from .agent_types import AgentResult
from .agents.conflict_resolver import resolve_conflicts
from .agents.entity_customer import run_entity_customer_agent
from .agents.order_product import run_order_product_agent
from .agents.payment_refund import run_payment_refund_agent
from .agents.policy import run_policy_agent
from .agents.shipment import run_shipment_agent
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

HANDLERS: dict[str, AgentHandler] = {
    "resolve_entity": run_entity_customer_agent,
    "investigate_order": run_order_product_agent,
    "investigate_shipment": run_shipment_agent,
    "investigate_payment": run_payment_refund_agent,
    "check_policy": run_policy_agent,
    "resolve_conflict": resolve_conflicts,
}


@dataclass
class CaseState:
    """Transient state; create a new instance for every case."""

    case_id: str
    case: dict[str, Any]
    results: dict[str, AgentResult] = field(default_factory=dict)
    evidence_refs: list[str] = field(default_factory=list)

    def add_result(self, task_type: str, result: AgentResult) -> None:
        if result.case_id != self.case_id:
            raise ValueError("cannot add a specialist result from another case")
        self.results[task_type] = result
        self.evidence_refs = list(dict.fromkeys([*self.evidence_refs, *result.evidence_refs]))


async def run_investigation(
    case: dict[str, Any],
    gateway: EvidenceGateway,
    trace: TraceWriter,
    handlers: dict[str, AgentHandler] | None = None,
) -> CaseState:
    """Run the dependency skeleton; specialist decisions stay with their owners."""
    case_id = case["case_id"]
    if not isinstance(case_id, str) or not case_id:
        raise ValueError("case_id must be a nonempty string")
    state = CaseState(case_id=case_id, case=case)
    selected = HANDLERS if handlers is None else handlers

    async def assign(task_type: str, payload: dict[str, Any]) -> AgentResult:
        task = make_task(
            case_id=case_id,
            task_type=task_type,
            sequence=len(state.results) + 1,
            payload=payload,
            evidence_refs=state.evidence_refs,
        )
        result = await dispatch(task, selected[task_type], gateway, trace)
        state.add_result(task_type, result)
        return result

    entity = await assign("resolve_entity", {"case": case})
    if entity.status != "ok":
        return state
    order_ids = entity.data.get("resolved_order_ids")
    if not isinstance(order_ids, list) or not order_ids:
        raise ValueError("resolved entity result must contain resolved_order_ids")

    common = {
        "case": case,
        "resolved_order_ids": order_ids,
        "customer_unique_id": entity.data.get("customer_unique_id"),
    }
    order = await assign("investigate_order", common)
    await assign("investigate_shipment", {**common, "order": order.data})
    await assign("investigate_payment", {**common, "order": order.data})
    findings = {name: result.data for name, result in state.results.items()}
    await assign("check_policy", {"case": case, "findings": findings})
    findings = {name: result.data for name, result in state.results.items()}
    await assign("resolve_conflict", {"case": case, "findings": findings})
    return state


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Phase 2 must assemble and verify a schema-valid output before returning."""
    await run_investigation(case, gateway, trace)
    raise NotImplementedError("Phase 2: assemble, verify and return the L3B output")
