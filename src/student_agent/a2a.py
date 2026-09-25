"""Observable A2A assignment and handoff helpers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from .agent_types import AgentResult, AgentTask
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

ACTORS = (
    "coordinator",
    "entity-agent",
    "order-product-agent",
    "shipment-agent",
    "payment-refund-agent",
    "policy-agent",
    "conflict-resolver",
    "verifier",
)

# task_type: (owner, assignment code, default handoff code)
TASKS = {
    "resolve_entity": ("entity-agent", "RESOLVE_ENTITY", "ENTITY_RESOLVED"),
    "investigate_order": ("order-product-agent", "INVESTIGATE_ORDER", "ORDER_ANALYZED"),
    "investigate_shipment": ("shipment-agent", "INVESTIGATE_SHIPMENT", "SHIPMENT_ANALYZED"),
    "investigate_payment": ("payment-refund-agent", "INVESTIGATE_PAYMENT", "PAYMENT_ANALYZED"),
    "check_policy": ("policy-agent", "CHECK_POLICY", "POLICY_CHECKED"),
    "resolve_conflict": ("conflict-resolver", "RESOLVE_CONFLICT", "CONFLICT_RESOLVED"),
    "verify_output": ("verifier", "VERIFY_OUTPUT", "VERIFY_PASS"),
}

AgentHandler = Callable[[AgentTask, EvidenceGateway, TraceWriter], Awaitable[AgentResult]]


def make_task(
    *,
    case_id: str,
    task_type: str,
    sequence: int,
    payload: dict[str, Any],
    evidence_refs: list[str] | None = None,
) -> AgentTask:
    if task_type not in TASKS:
        raise ValueError(f"unknown A2A task type: {task_type}")
    if sequence < 1:
        raise ValueError("A2A sequence must be positive")
    return AgentTask(
        case_id=case_id,
        task_id=f"{case_id}:{sequence}:{task_type}",
        from_actor="coordinator",
        to_actor=TASKS[task_type][0],
        task_type=task_type,
        payload=payload,
        evidence_refs=list(evidence_refs or []),
    )


async def dispatch(
    task: AgentTask,
    handler: AgentHandler,
    gateway: EvidenceGateway,
    trace: TraceWriter,
) -> AgentResult:
    """Trace a real call and reject a reply from the wrong case or actor."""
    owner, assignment_code, handoff_code = TASKS[task.task_type]
    if task.from_actor != "coordinator" or task.to_actor != owner:
        raise ValueError("A2A task routing does not match the task registry")
    trace.emit(
        case_id=task.case_id,
        event_type="task_assigned",
        actor="coordinator",
        target=owner,
        decision_code=assignment_code,
        attributes={"task_id": task.task_id},
    )
    result = await handler(task, gateway, trace)
    if not isinstance(result, AgentResult):
        raise TypeError(f"{owner} must return AgentResult")
    if result.case_id != task.case_id or result.actor != owner:
        raise ValueError("A2A handoff case or actor mismatch")
    decision_code = result.decision_code
    if decision_code is None:
        if result.status == "ok":
            decision_code = handoff_code
        elif task.task_type == "resolve_entity" and result.status == "ambiguous":
            decision_code = "ENTITY_AMBIGUOUS"
        else:
            decision_code = f"{task.task_type.upper()}_{result.status.upper()}"
    trace.emit(
        case_id=task.case_id,
        event_type="handoff",
        actor=owner,
        target="coordinator",
        decision_code=decision_code,
        evidence_refs=result.evidence_refs,
        attributes={"task_id": task.task_id, "status": result.status},
    )
    return result
