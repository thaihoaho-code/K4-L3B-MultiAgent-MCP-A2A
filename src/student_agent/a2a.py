"""Coordinator-mediated A2A calls and case-scoped evidence handoffs."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from .agent_types import AgentResult, AgentTask
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

ResultT = TypeVar("ResultT")

# Names reflect the actual combined agents in the user-approved pipeline.
TASK_ACTORS = {
    "resolve_entity": "entity-agent",
    "investigate_order_shipment": "order-shipment-agent",
    "investigate_payment_refund": "payment-refund-agent",
    "decide_policy": "policy-agent",
    "verify_output": "verifier",
}
ASSIGNMENT_CODES = {
    "resolve_entity": "RESOLVE_ENTITY",
    "investigate_order_shipment": "INVESTIGATE_ORDER_SHIPMENT",
    "investigate_payment_refund": "INVESTIGATE_PAYMENT",
    "decide_policy": "CHECK_POLICY",
    "verify_output": "VERIFY_OUTPUT",
}


def make_task(
    *,
    case_id: str,
    task_type: str,
    sequence: int,
    payload: dict[str, Any] | None = None,
    evidence_refs: tuple[str, ...] = (),
) -> AgentTask:
    """Build a small, case-correlated assignment envelope."""
    if task_type not in TASK_ACTORS:
        raise ValueError(f"unknown A2A task type: {task_type}")
    if sequence < 1:
        raise ValueError("A2A task sequence must be positive")
    return AgentTask(
        case_id=case_id,
        task_id=f"{case_id}:{sequence}:{task_type}",
        from_actor="coordinator",
        to_actor=TASK_ACTORS[task_type],
        task_type=task_type,
        payload=dict(payload or {}),
        evidence_refs=evidence_refs,
    )


def validate_handoff(task: AgentTask, result: AgentResult[Any]) -> None:
    """Reject a reply from another case, task, or actor."""
    if task.task_type not in TASK_ACTORS or task.from_actor != "coordinator":
        raise ValueError("A2A task route is invalid")
    if task.to_actor != TASK_ACTORS[task.task_type]:
        raise ValueError("A2A task recipient does not match task type")
    if (
        result.case_id != task.case_id
        or result.task_id != task.task_id
        or result.actor != task.to_actor
    ):
        raise ValueError("A2A handoff case, task, or actor mismatch")


def wrap_result(
    task: AgentTask,
    data: ResultT,
    *,
    status: str,
    decision_code: str,
    evidence_refs: tuple[str, ...] = (),
    confidence: float | None = None,
    warnings: tuple[str, ...] = (),
) -> AgentResult[ResultT]:
    """Wrap a typed specialist value without changing its current method signature."""
    result = AgentResult(
        case_id=task.case_id,
        task_id=task.task_id,
        actor=task.to_actor,
        status=status,
        data=data,
        evidence_refs=evidence_refs,
        decision_code=decision_code,
        confidence=confidence,
        warnings=warnings,
    )
    validate_handoff(task, result)
    return result


class CaseGateway(EvidenceGateway):
    """Route all calls through one case and remember server-issued refs."""

    def __init__(self, gateway: EvidenceGateway, case_id: str) -> None:
        self._gateway = gateway
        self.case_id = case_id
        self.observed_refs: dict[str, str] = {}

    async def list_tools(self) -> list[str]:
        return await self._gateway.list_tools()

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        if case_id != self.case_id:
            raise ValueError("MCP call attempted another case_id")
        evidence = await self._gateway.call(tool_name, case_id=case_id, **arguments)
        self.observed_refs[evidence["evidence_ref"]] = tool_name
        return evidence


async def dispatch(
    task: AgentTask,
    invoke: Callable[[], Awaitable[ResultT] | ResultT],
    expected_type: type[ResultT],
    status: Callable[[ResultT], str],
    decision_code: Callable[[ResultT], str],
    gateway: CaseGateway,
    trace: TraceWriter,
    validate: Callable[[ResultT], None] | None = None,
) -> AgentResult[ResultT]:
    """Trace a real assignment and only hand off a valid result."""
    if task.case_id != gateway.case_id or task.to_actor != TASK_ACTORS[task.task_type]:
        raise ValueError("A2A task case or route mismatch")
    trace.emit(
        case_id=task.case_id,
        event_type="task_assigned",
        actor="coordinator",
        target=task.to_actor,
        decision_code=ASSIGNMENT_CODES[task.task_type],
        attributes={"task_id": task.task_id},
    )
    raw = invoke()
    data = await raw if inspect.isawaitable(raw) else raw
    if not isinstance(data, expected_type):
        raise TypeError(f"{task.to_actor} must return {expected_type.__name__}")
    refs = data.get("evidence_refs", []) if isinstance(data, dict) else data.evidence_refs
    if not isinstance(refs, list):
        raise TypeError("specialist evidence_refs must be a list")
    if any(ref not in gateway.observed_refs for ref in refs):
        raise ValueError("specialist evidence_ref was not observed in this case")
    if validate is not None:
        validate(data)
    result = wrap_result(
        task,
        data,
        status=status(data),
        decision_code=decision_code(data),
        evidence_refs=tuple(refs),
        confidence=getattr(data, "confidence", None),
    )
    trace.emit(
        case_id=task.case_id,
        event_type="handoff",
        actor=task.to_actor,
        target="coordinator",
        decision_code=result.decision_code,
        evidence_refs=list(result.evidence_refs[:20]),
        attributes={
            "task_id": task.task_id,
            "status": result.status,
            "evidence_count": len(result.evidence_refs),
            "confidence": result.confidence,
        },
    )
    return result
