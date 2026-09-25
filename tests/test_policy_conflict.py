from __future__ import annotations

import asyncio
from typing import Any

from student_agent.policy_agent import PolicyAgent
from student_agent.state import EntityResult, PaymentResult, ShipmentResult

EVIDENCE_ENTITY = "ev_entity_12345678901234567890"
EVIDENCE_SHIPMENT = "ev_shipment_12345678901234567890"
EVIDENCE_PAYMENT = "ev_payment_12345678901234567890"
EVIDENCE_POLICY = "ev_policy_12345678901234567890"


class FakeGateway:
    def __init__(self, policy_data: Any = None, tools: list[str] | None = None) -> None:
        self.policy_data = policy_data
        self.tools = tools if tools is not None else ["get_policy"]
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def list_tools(self) -> list[str]:
        return self.tools

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        self.calls.append((tool_name, {"case_id": case_id, **arguments}))
        return {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": EVIDENCE_POLICY,
            "result_hash": "sha256:" + "a" * 64,
            "domain": "policy",
            "data": self.policy_data or {},
        }


class FakeTrace:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def emit(self, **event: Any) -> dict[str, Any]:
        self.events.append(event)
        return event


def run(coroutine: Any) -> Any:
    return asyncio.run(coroutine)


def make_case(*topics: str, **extra: Any) -> dict[str, Any]:
    case: dict[str, Any] = {
        "case_id": "L3B_CASE_TEST",
        "policy_version": "EC_POLICY_V2",
        "customer_request": {
            "claims": [
                {"claim_id": f"claim-{index}", "topic": topic}
                for index, topic in enumerate(topics, start=1)
            ]
        },
    }
    case.update(extra)
    return case


def make_entity() -> EntityResult:
    return EntityResult(
        customer_unique_id="customer-1",
        resolved_order_id="order-1",
        resolution_status="resolved",
        confidence=0.95,
        evidence_refs=[EVIDENCE_ENTITY],
    )


def test_delivered_system_overrides_late_customer_claim() -> None:
    trace = FakeTrace()
    result = run(
        PolicyAgent().resolve_conflict(
            make_case("late_delivery_logistics"),
            make_entity(),
            ShipmentResult(
                verdict="on_time",
                timeline_complete=True,
                evidence_refs=[EVIDENCE_SHIPMENT],
            ),
            PaymentResult(verdict="reconciled", evidence_refs=[EVIDENCE_PAYMENT]),
            FakeGateway({"refund_eligible": False}),
            trace,
        )
    )

    assert result.primary_issue == "unsupported_claim"
    assert result.data_conflicts[0].selected_source == "shipment_event"
    assert result.data_conflicts[0].resolution_code == "AUTHORITATIVE_SHIPMENT_EVENT"
    assert result.resolution_actions == []


def test_payment_ledger_overrides_payment_claim() -> None:
    result = run(
        PolicyAgent().resolve_conflict(
            make_case("payment_mismatch"),
            make_entity(),
            ShipmentResult(
                verdict="on_time",
                timeline_complete=True,
                evidence_refs=[EVIDENCE_SHIPMENT],
            ),
            PaymentResult(verdict="reconciled", evidence_refs=[EVIDENCE_PAYMENT]),
            FakeGateway({"refund_eligible": False}),
            FakeTrace(),
        )
    )

    conflict = next(
        conflict for conflict in result.data_conflicts if conflict.field == "payment_status"
    )
    assert conflict.selected_source == "payment_ledger"
    assert conflict.resolution_code == "PAYMENT_LEDGER_AUTHORITATIVE"


def test_unknown_conflict_is_preserved_as_unresolved() -> None:
    result = run(
        PolicyAgent().resolve_conflict(
            make_case(
                "requested_full_refund",
                conflict_candidates=[
                    {"field": "custom_status", "sources": ["source_a", "source_b"]}
                ],
            ),
            make_entity(),
            ShipmentResult(verdict="insufficient_evidence"),
            PaymentResult(verdict="insufficient_evidence"),
            FakeGateway({"refund_eligible": True}),
            FakeTrace(),
        )
    )

    conflict = next(
        conflict for conflict in result.data_conflicts if conflict.field == "custom_status"
    )
    assert conflict.selected_source is None
    assert conflict.resolution_code == "UNRESOLVED_INSUFFICIENT_EVIDENCE"
    assert result.case_status == "needs_investigation"
    assert result.financial_resolution.recommended_refund_brl == 0.0


def test_seller_delay_maps_to_seller_responsibility() -> None:
    result = run(
        PolicyAgent().resolve_conflict(
            make_case("late_delivery_seller"),
            make_entity(),
            ShipmentResult(
                verdict="seller_delay",
                late_seller_ids=["seller-1"],
                seller_ids=["seller-1"],
                timeline_complete=True,
                evidence_refs=[EVIDENCE_SHIPMENT],
            ),
            PaymentResult(verdict="reconciled", evidence_refs=[EVIDENCE_PAYMENT]),
            FakeGateway({"refund_eligible": False}),
            FakeTrace(),
        )
    )

    assert result.primary_issue == "late_delivery_seller"
    assert result.ranked_causes[0].cause_code == "SELLER_LATE_HANDOFF"
    assert result.responsible_parties[0].party_type == "seller"
    assert result.responsible_parties[0].party_id == "seller-1"


def test_logistics_delay_does_not_assign_seller() -> None:
    result = run(
        PolicyAgent().resolve_conflict(
            make_case("late_delivery_logistics"),
            make_entity(),
            ShipmentResult(
                verdict="logistics_delay",
                seller_ids=["seller-1"],
                timeline_complete=True,
                evidence_refs=[EVIDENCE_SHIPMENT],
            ),
            PaymentResult(verdict="reconciled", evidence_refs=[EVIDENCE_PAYMENT]),
            FakeGateway({"refund_eligible": False}),
            FakeTrace(),
        )
    )

    assert result.ranked_causes[0].cause_code == "LOGISTICS_TRANSIT_DELAY"
    assert result.responsible_parties[0].party_type == "logistics_provider"
    assert result.responsible_parties[0].party_type != "seller"


def test_refund_requires_policy_and_payment_amount() -> None:
    gateway = FakeGateway({"allowed_actions": ["issue_refund"]})
    trace = FakeTrace()
    result = run(
        PolicyAgent().resolve_conflict(
            make_case("requested_full_refund"),
            make_entity(),
            ShipmentResult(
                verdict="logistics_delay",
                timeline_complete=True,
                evidence_refs=[EVIDENCE_SHIPMENT],
            ),
            PaymentResult(
                verdict="reconciled",
                refundable_total_brl=42.5,
                evidence_refs=[EVIDENCE_PAYMENT],
            ),
            gateway,
            trace,
        )
    )

    assert result.financial_resolution.recommended_refund_brl == 42.5
    assert result.financial_resolution.refund_lines[0].reason_code == "POLICY_REFUND"
    assert result.resolution_actions == ["ISSUE_REFUND"]
    assert result.case_status == "action_required"
    assert gateway.calls[0][0] == "get_policy"
    assert any(event["event_type"] == "tool_result_consumed" for event in trace.events)
    assert any(event["event_type"] == "policy_decided" for event in trace.events)


def test_already_refunded_never_gets_second_refund() -> None:
    result = run(
        PolicyAgent().resolve_conflict(
            make_case("requested_full_refund"),
            make_entity(),
            ShipmentResult(
                verdict="on_time",
                timeline_complete=True,
                evidence_refs=[EVIDENCE_SHIPMENT],
            ),
            PaymentResult(
                verdict="refunded",
                refunded_total_brl=42.5,
                refundable_total_brl=0.0,
                evidence_refs=[EVIDENCE_PAYMENT],
            ),
            FakeGateway({"refund_eligible": True}),
            FakeTrace(),
        )
    )

    assert result.financial_resolution.recommended_refund_brl == 0.0
    assert result.resolution_actions == []
    assert result.case_status == "no_action"


def test_missing_policy_evidence_does_not_create_financial_action() -> None:
    result = run(
        PolicyAgent().resolve_conflict(
            make_case("requested_full_refund"),
            make_entity(),
            ShipmentResult(
                verdict="logistics_delay",
                timeline_complete=True,
                evidence_refs=[EVIDENCE_SHIPMENT],
            ),
            PaymentResult(
                verdict="reconciled",
                refundable_total_brl=42.5,
                evidence_refs=[EVIDENCE_PAYMENT],
            ),
            FakeGateway(tools=[]),
            FakeTrace(),
        )
    )

    assert result.financial_resolution.recommended_refund_brl == 0.0
    assert result.resolution_actions == []
    assert result.case_status == "needs_investigation"
