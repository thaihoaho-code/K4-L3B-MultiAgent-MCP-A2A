"""Phase 1 contract tests using fake specialists, never fabricated MCP evidence."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from student_agent.a2a import AgentHandler, dispatch, make_task
from student_agent.agent_types import AgentResult, AgentTask
from student_agent.contracts import Contracts
from student_agent.trace import TraceWriter
from student_agent.workflow import run_investigation


def writer(tmp_path: Path) -> TraceWriter:
    root = Path(__file__).resolve().parents[1]
    return TraceWriter(tmp_path / "trace.jsonl", Contracts(root / "contracts" / "schemas"))


def events(trace: TraceWriter) -> list[dict[str, Any]]:
    return [json.loads(line) for line in trace.path.read_text(encoding="utf-8").splitlines()]


def fake_handlers(calls: list[AgentTask]) -> dict[str, AgentHandler]:
    def handler(actor: str) -> AgentHandler:
        async def run(task: AgentTask, gateway: Any, trace: TraceWriter) -> AgentResult:
            calls.append(task)
            data: dict[str, Any] = {}
            if task.task_type == "resolve_entity":
                data = {"resolved_order_ids": ["ORDER_1"], "customer_unique_id": "CUSTOMER_1"}
            return AgentResult(task.case_id, actor, "ok", 0.8, data=data)

        return run

    return {
        "resolve_entity": handler("entity-agent"),
        "investigate_order": handler("order-product-agent"),
        "investigate_shipment": handler("shipment-agent"),
        "investigate_payment": handler("payment-refund-agent"),
        "check_policy": handler("policy-agent"),
        "resolve_conflict": handler("conflict-resolver"),
    }


def test_coordinator_assigns_and_receives_each_dependency(tmp_path: Path) -> None:
    calls: list[AgentTask] = []
    trace = writer(tmp_path)
    state = asyncio.run(
        run_investigation({"case_id": "CASE_001"}, None, trace, fake_handlers(calls))
    )
    assert list(state.results) == [
        "resolve_entity",
        "investigate_order",
        "investigate_shipment",
        "investigate_payment",
        "check_policy",
        "resolve_conflict",
    ]
    assert calls[1].payload["resolved_order_ids"] == ["ORDER_1"]
    assert calls[4].payload["findings"]["investigate_order"] == {}
    assert all(task.case_id == "CASE_001" for task in calls)
    trace_events = events(trace)
    assert [event["event_type"] for event in trace_events] == [
        event for _ in calls for event in ("task_assigned", "handoff")
    ]
    assert all(event["case_id"] == "CASE_001" for event in trace_events)
    assert all(event["target"] == "coordinator" for event in trace_events[1::2])


def test_ambiguous_entity_stops_downstream_calls(tmp_path: Path) -> None:
    calls: list[AgentTask] = []
    handlers = fake_handlers(calls)

    async def ambiguous(task: AgentTask, gateway: Any, trace: TraceWriter) -> AgentResult:
        calls.append(task)
        return AgentResult(task.case_id, "entity-agent", "ambiguous", 0.42)

    handlers["resolve_entity"] = ambiguous
    trace = writer(tmp_path)
    state = asyncio.run(run_investigation({"case_id": "CASE_002"}, None, trace, handlers))
    assert len(calls) == 1
    assert list(state.results) == ["resolve_entity"]
    assert [event["event_type"] for event in events(trace)] == ["task_assigned", "handoff"]
    assert events(trace)[1]["decision_code"] == "ENTITY_AMBIGUOUS"


def test_handoff_rejects_result_from_another_case(tmp_path: Path) -> None:
    async def wrong_case(task: AgentTask, gateway: Any, trace: TraceWriter) -> AgentResult:
        return AgentResult("CASE_999", "entity-agent", "ok", 0.9)

    trace = writer(tmp_path)
    task = make_task(case_id="CASE_003", task_type="resolve_entity", sequence=1, payload={})
    with pytest.raises(ValueError, match="case or actor mismatch"):
        asyncio.run(dispatch(task, wrong_case, None, trace))
    assert [event["event_type"] for event in events(trace)] == ["task_assigned"]


def test_agent_result_rejects_invalid_confidence_and_evidence() -> None:
    with pytest.raises(ValueError, match="confidence"):
        AgentResult("CASE_001", "entity-agent", "ok", float("nan"))
    with pytest.raises(ValueError, match="evidence ref"):
        AgentResult("CASE_001", "entity-agent", "ok", 0.8, evidence_refs=["ev_fake"])
