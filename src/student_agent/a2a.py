"""A2A protocol: the coordinator is the only task sender and every reply is validated.

``A2ABus`` emits ``task_assigned`` when a task leaves the coordinator and ``handoff`` only
after the reply is correlated (same case, same task id, expected actor, valid status and
confidence, evidence issued for this case). Each task type runs at most ``max_runs_per_type``
times and a case can issue at most ``max_tasks`` tasks, so agents can never loop.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from .agent_types import COORDINATOR, TASK_ROUTES, AgentResult, AgentTask, Status
from .evidence_cache import CaseEvidenceStore, TraceSink

Handler = Callable[[AgentTask], Awaitable[AgentResult]]


class HandoffError(ValueError):
    pass


@dataclass
class A2ABus:
    case_id: str
    trace: TraceSink
    store: CaseEvidenceStore
    max_tasks: int = 14
    max_runs_per_type: int = 2
    _sequence: int = 0
    _open: dict[str, AgentTask] = field(default_factory=dict)
    _runs: Counter[str] = field(default_factory=Counter)
    results: list[AgentResult] = field(default_factory=list)

    def assign(
        self,
        task_type: str,
        payload: dict[str, Any] | None = None,
        evidence_refs: list[str] | tuple[str, ...] = (),
    ) -> AgentTask:
        route = TASK_ROUTES.get(task_type)
        if route is None:
            raise HandoffError(f"unknown task type {task_type!r}")
        if self._sequence >= self.max_tasks:
            raise HandoffError(f"{self.case_id}: task budget exhausted")
        if self._runs[task_type] >= self.max_runs_per_type:
            raise HandoffError(f"{self.case_id}: {task_type} exceeded its run limit")
        self._sequence += 1
        self._runs[task_type] += 1
        task = AgentTask(
            case_id=self.case_id,
            task_id=f"T{self._sequence:02d}",
            from_actor=COORDINATOR,
            to_actor=route.actor,
            task_type=task_type,
            payload=dict(payload or {}),
            evidence_refs=tuple(dict.fromkeys(evidence_refs)),
        )
        self._open[task.task_id] = task
        self.trace.emit(
            case_id=self.case_id,
            event_type="task_assigned",
            actor=COORDINATOR,
            target=route.actor,
            decision_code=route.assign_code,
            attributes={"task_id": task.task_id},
        )
        return task

    def accept(self, task: AgentTask, result: AgentResult) -> AgentResult:
        self._validate(task, result)
        del self._open[task.task_id]
        self.results.append(result)
        attributes: dict[str, str | int | float | bool | None] = {
            "task_id": task.task_id,
            "status": str(result.status),
            "confidence": result.confidence,
        }
        self.trace.emit(
            case_id=self.case_id,
            event_type="handoff",
            actor=result.actor,
            target=COORDINATOR,
            decision_code=result.decision_code,
            evidence_refs=result.evidence_refs[:20] or None,
            attributes=attributes,
        )
        return result

    async def dispatch(
        self,
        task_type: str,
        handler: Handler,
        payload: dict[str, Any] | None = None,
        evidence_refs: list[str] | tuple[str, ...] = (),
    ) -> AgentResult:
        task = self.assign(task_type, payload, evidence_refs)
        result = await handler(task)
        return self.accept(task, result)

    def _validate(self, task: AgentTask, result: AgentResult) -> None:
        if task.task_id not in self._open:
            raise HandoffError(f"{task.task_id} is not an open task")
        if result.case_id != self.case_id or task.case_id != self.case_id:
            raise HandoffError("handoff case_id does not match the active case")
        if result.task_id != task.task_id:
            raise HandoffError("handoff task_id does not match the assignment")
        if result.actor != task.to_actor:
            raise HandoffError(f"{result.actor} answered a task assigned to {task.to_actor}")
        if not isinstance(result.status, Status):
            raise HandoffError("handoff status is not an A2A status")
        if not 0.0 <= result.confidence <= 1.0:
            raise HandoffError("handoff confidence is outside [0, 1]")
        if not result.decision_code:
            raise HandoffError("handoff requires a decision code")
        foreign = [ref for ref in result.evidence_refs if not self.store.owns(ref)]
        if foreign:
            raise HandoffError(f"handoff cites evidence not issued for this case: {foreign}")
        unconsumed = set(result.evidence_refs) - set(self.store.consumed_refs())
        if unconsumed:
            raise HandoffError(f"handoff cites evidence never consumed: {sorted(unconsumed)}")
