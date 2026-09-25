from __future__ import annotations

import asyncio
import json

import pytest

from conftest import ORDER_ID, SELLER, FakeGateway, make_case, scenario
from student_agent.a2a import A2ABus, HandoffError
from student_agent.agent_types import AgentResult, Status
from student_agent.evidence_cache import CaseEvidenceStore, ToolPermissionError
from student_agent.workflow import solve_case

LATE = {
    "event_at": "2018-03-13T09:00:00-03:00",
    "event_type": "delivered_late",
    "status": "confirmed",
}
REFUND = {
    "event_at": "2018-03-12T09:00:00-03:00",
    "event_type": "refund_requested",
    "amount_brl": "52.00",
    "status": "failed",
}
SCENARIOS = {
    "late_delivery_seller": (
        scenario(
            carrier_days=5,
            delivered_days=12,
            freight="18.00",
            captures=[("credit_card", "18.00")],
            shipment_events=[dict(LATE, actor="seller")],
        ),
        18.0,
        "refund_freight",
    ),
    "late_delivery_logistics": (
        scenario(
            delivered_days=12,
            freight="18.00",
            captures=[("credit_card", "16.00")],
            shipment_events=[dict(LATE, actor="logistics_provider")],
        ),
        16.0,
        "refund_freight",
    ),
    "canceled_order_paid": (
        scenario(status="canceled", delivered_days=None, captures=[("credit_card", "79.00")]),
        79.0,
        "issue_refund",
    ),
    "duplicate_charge": (
        scenario(captures=[("credit_card", "64.00"), ("voucher", "64.00")]),
        64.0,
        "refund_duplicate_charge",
    ),
    "valid_split_payment": (
        scenario(captures=[("credit_card", "44.50"), ("voucher", "44.50")]),
        0.0,
        "document_no_action",
    ),
    "refund_failed": (
        scenario(captures=[("credit_card", "52.00")], refunds=[REFUND]),
        52.0,
        "retry_refund",
    ),
    "unsupported_claim": (scenario(), 0.0, "document_no_action"),
}


def read_trace(trace) -> list[dict]:
    return [json.loads(line) for line in trace.path.read_text().splitlines()]


@pytest.mark.parametrize("issue", sorted(SCENARIOS))
def test_end_to_end_issue_resolution(issue, trace, contracts) -> None:
    data, refund, action = SCENARIOS[issue]
    case = make_case(topic=issue)
    gateway = FakeGateway(data)
    output = asyncio.run(solve_case(case, gateway, trace))
    contracts.validate_output(output, "output")
    assert output["case_id"] == case["case_id"]
    assert output["assessment"]["primary_issue"] == issue
    assert output["financial_resolution"]["recommended_refund_brl"] == refund
    assert output["resolution_actions"] == [action]
    assert output["entity_resolution"]["resolved_order_ids"] == [ORDER_ID]
    assert len(gateway.calls) <= 9
    assert len({(name, tuple(sorted(args.items()))) for name, args in gateway.calls}) == len(
        gateway.calls
    ), "no tool is called twice with the same arguments"
    if issue == "late_delivery_seller":
        assert output["shipment_analysis"]["late_seller_ids"] == [SELLER]
        assert output["root_cause_analysis"]["responsible_parties"] == [
            {"party_type": "seller", "party_id": SELLER}
        ]


def test_trace_lifecycle_and_evidence_linkage(trace) -> None:
    case = make_case(topic="late_delivery_seller")
    data = SCENARIOS["late_delivery_seller"][0]
    output = asyncio.run(solve_case(case, FakeGateway(data), trace))
    events = read_trace(trace)
    kinds = [event["event_type"] for event in events]
    for required in (
        "task_assigned",
        "handoff",
        "tool_result_consumed",
        "policy_decided",
        "verification_completed",
    ):
        assert required in kinds
    assert kinds.index("verification_completed") > kinds.index("policy_decided")
    consumed = {
        ref
        for event in events
        if event["event_type"] == "tool_result_consumed"
        for ref in event["evidence_refs"]
    }
    assert set(output["evidence_refs"]) == consumed
    actors = {event["actor"] for event in events}
    assert {
        "coordinator",
        "entity-agent",
        "order-product-agent",
        "shipment-agent",
        "payment-refund-agent",
        "policy-agent",
        "conflict-resolver",
        "verifier",
    } <= actors
    verdicts = [
        event["decision_code"]
        for event in events
        if event["event_type"] == "verification_completed"
    ]
    assert verdicts == ["VERIFY_PASS"]
    text = trace.path.read_text()
    assert "sk-team-" not in text


def test_unresolved_entity_skips_specialist_calls(trace, contracts) -> None:
    data = scenario()
    data["get_customer_history"]["orders"] = []
    case = make_case(candidates=["candidate-a", "candidate-b"])
    case["customer_request"]["claimed_order_id"] = "candidate-a"
    gateway = FakeGateway(data)
    output = asyncio.run(solve_case(case, gateway, trace))
    contracts.validate_output(output, "output")
    assert output["entity_resolution"]["status"] == "not_found"
    assert output["assessment"]["primary_issue"] == "insufficient_evidence"
    assert output["assessment"]["case_status"] == "needs_investigation"
    assert gateway.tools() == ["get_customer_history"]


def test_mcp_outage_degrades_without_crashing(trace, contracts) -> None:
    gateway = FakeGateway({})
    output = asyncio.run(solve_case(make_case(), gateway, trace))
    contracts.validate_output(output, "output")
    assert output["assessment"]["primary_issue"] == "insufficient_evidence"
    assert output["evidence_refs"] == []


def test_a2a_rejects_misrouted_or_foreign_handoffs(trace) -> None:
    store = CaseEvidenceStore("L3B_CASE_T01", FakeGateway(scenario()), trace)
    bus = A2ABus("L3B_CASE_T01", trace, store)
    task = bus.assign("resolve_entity")
    wrong_actor = AgentResult(
        "L3B_CASE_T01", task.task_id, "shipment-agent", Status.OK, 0.9, decision_code="X"
    )
    with pytest.raises(HandoffError):
        bus.accept(task, wrong_actor)
    foreign = AgentResult.reply(
        task, status=Status.OK, confidence=0.9, decision_code="X", evidence_refs=["ev_" + "x" * 24]
    )
    with pytest.raises(HandoffError):
        bus.accept(task, foreign)


def test_a2a_loop_guard_and_least_privilege(trace) -> None:
    store = CaseEvidenceStore("L3B_CASE_T01", FakeGateway(scenario()), trace)
    bus = A2ABus("L3B_CASE_T01", trace, store, max_runs_per_type=2)
    bus.assign("verify_output")
    bus.assign("verify_output")
    with pytest.raises(HandoffError):
        bus.assign("verify_output")
    with pytest.raises(ToolPermissionError):
        asyncio.run(store.fetch("shipment-agent", "get_policy", policy_version="x"))
