from __future__ import annotations

import asyncio
from typing import Any

from student_agent.entity_agent import EntityAgent
from student_agent.order_shipment_agent import OrderShipmentAgent
from student_agent.policy_agent import PolicyAgent
from student_agent.state import EntityResult, PaymentResult, ShipmentResult


def _ref(label: str) -> str:
    return "ev_" + label.ljust(20, "x")


class FakeTrace:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def emit(self, **event: Any) -> dict[str, Any]:
        self.events.append(event)
        return event


class FakeGateway:
    def __init__(self, responses: dict[str, dict[str, Any]]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    async def list_tools(self) -> list[str]:
        return list(self.responses)

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        self.calls.append((tool_name, case_id, arguments))
        response = self.responses[tool_name]
        if isinstance(response, list):
            return response.pop(0)
        return response


def _run(awaitable: Any) -> Any:
    return asyncio.run(awaitable)


def test_entity_uses_customer_history_and_order_evidence() -> None:
    gateway = FakeGateway(
        {
            "get_customer_history": {
                "evidence_ref": _ref("history"),
                "data": {
                    "customer_unique_id": "customer-1",
                    "orders": [
                        {"order_id": "order-1"},
                        {"order_id": "order-2"},
                    ],
                },
            },
            "get_order": {
                "evidence_ref": _ref("order"),
                "data": {"order_id": "order-1", "order_status": "delivered"},
            },
        }
    )
    trace = FakeTrace()
    result = _run(
        EntityAgent().resolve(
            {
                "case_id": "CASE_001",
                "customer_unique_id_hint": "customer-1",
                "customer_request": {"claimed_order_id": "order-1"},
            },
            gateway,
            trace,
        )
    )

    assert result.resolution_status == "resolved"
    assert result.resolved_order_id == "order-1"
    assert result.related_order_ids == ["order-2"]
    assert result.evidence_refs == [_ref("history"), _ref("order")]
    assert gateway.calls[0][2] == {"customer_unique_id": "customer-1"}
    assert gateway.calls[1][2] == {"order_id": "order-1"}


def test_entity_does_not_pick_between_multiple_candidates() -> None:
    gateway = FakeGateway(
        {
            "get_customer_history": {
                "evidence_ref": _ref("history"),
                "data": {
                    "customer_unique_id": "customer-1",
                    "orders": [{"order_id": "order-1"}, {"order_id": "order-2"}],
                },
            }
        }
    )
    result = _run(
        EntityAgent().resolve(
            {
                "case_id": "CASE_001",
                "customer_unique_id_hint": "customer-1",
                "candidate_order_ids": ["order-1", "order-2"],
            },
            gateway,
            FakeTrace(),
        )
    )

    assert result.resolution_status == "ambiguous"
    assert result.resolved_order_id is None
    assert gateway.calls == [
        ("get_customer_history", "CASE_001", {"customer_unique_id": "customer-1"})
    ]


def test_shipment_uses_real_mcp_tool_names_and_classifies_seller_delay() -> None:
    gateway = FakeGateway(
        {
            "get_shipment_summary": {
                "evidence_ref": _ref("shipment"),
                "data": {
                    "order_id": "order-1",
                    "shipment_id": "shipment-1",
                    "status": "delivered",
                    "is_late": True,
                    "delay_source": "seller",
                    "seller_id": "seller-1",
                    "delivered_at": "2025-01-05T00:00:00Z",
                    "expected_delivery_date": "2025-01-03",
                },
            },
            "get_order_items": {
                "evidence_ref": _ref("items"),
                "data": {"items": [{"order_item_id": "item-1", "seller_id": "seller-1"}]},
            },
        }
    )
    result = _run(
        OrderShipmentAgent().investigate(
            EntityResult("customer-1", "order-1"),
            gateway,
            FakeTrace(),
            case_id="CASE_001",
        )
    )

    assert result.verdict == "seller_delay"
    assert result.timeline_complete is True
    assert result.late_seller_ids == ["seller-1"]
    assert result.item_ids == ["item-1"]
    assert result.shipment_ids == ["shipment-1"]


def test_policy_maps_payment_issue_and_consumes_policy_evidence() -> None:
    gateway = FakeGateway(
        {
            "get_policy": {
                "evidence_ref": _ref("policy"),
                "data": {"refund_allowed": True, "max_refund_brl": 80},
            }
        }
    )
    result = _run(
        PolicyAgent().resolve_conflict(
            {
                "case_id": "CASE_001",
                "policy_version": "2025-01",
                "customer_request": {"claims": [{"topic": "requested_full_refund"}]},
            },
            EntityResult("customer-1", "order-1", resolution_status="resolved"),
            ShipmentResult(),
            PaymentResult(
                verdict="refund_failed",
                refundable_total_brl=100,
                evidence_refs=[_ref("payment")],
            ),
            gateway,
            FakeTrace(),
        )
    )

    assert result.primary_issue == "refund_failed"
    assert result.financial_resolution.recommended_refund_brl == 80.0
    assert result.evidence_refs == [_ref("payment"), _ref("policy")]
