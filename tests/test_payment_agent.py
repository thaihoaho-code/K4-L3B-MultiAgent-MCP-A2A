from __future__ import annotations

import asyncio
from typing import Any

from student_agent.payment_agent import PaymentAgent
from student_agent.state import EntityResult


def _ref(label: str) -> str:
    return "ev_" + label.ljust(20, "x")


class FakeTrace:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def emit(self, **event: Any) -> dict[str, Any]:
        self.events.append(event)
        return event


class FakeGateway:
    def __init__(self, payment: dict[str, Any], refund: dict[str, Any] | None = None) -> None:
        self.payment = payment
        self.refund = refund
        self.calls: list[tuple[str, str, str]] = []

    async def list_tools(self) -> list[str]:
        tools = ["get_payment_details"]
        if self.refund is not None:
            tools.append("get_refund_status")
        return tools

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        order_id = arguments["order_id"]
        self.calls.append((tool_name, case_id, order_id))
        if tool_name == "get_payment_details":
            return self.payment
        if self.refund is None:
            raise RuntimeError("refund unavailable")
        return self.refund


def _entity() -> EntityResult:
    return EntityResult(
        customer_unique_id="customer-1",
        resolved_order_id="order-1",
    )


def _run(gateway: FakeGateway, trace: FakeTrace, **kwargs: Any):
    return asyncio.run(
        PaymentAgent().check_transactions(
            _entity(), gateway, trace, case_id="CASE_001", **kwargs
        )
    )


def test_payment_is_reconciled_and_traced() -> None:
    gateway = FakeGateway(
        {
            "evidence_ref": _ref("payment"),
            "data": {
                "order_total_brl": 100,
                "payments": [
                    {"transaction_id": "pay-1", "status": "captured", "amount_brl": 100}
                ],
            },
        }
    )
    trace = FakeTrace()

    result = _run(gateway, trace)

    assert result.verdict == "reconciled"
    assert result.captured_total_brl == 100.0
    assert result.refunded_total_brl == 0.0
    assert result.payment_references == ["pay-1"]
    assert result.evidence_refs == [_ref("payment")]
    assert trace.events[0]["event_type"] == "tool_result_consumed"


def test_duplicate_capture_is_detected_without_using_record_count_alone() -> None:
    gateway = FakeGateway(
        {
            "evidence_ref": _ref("payment"),
            "data": {
                "order_total_brl": 100,
                "payments": [
                    {"transaction_id": "pay-1", "status": "captured", "amount_brl": 100},
                    {"transaction_id": "pay-1", "status": "captured", "amount_brl": 100},
                ],
            },
        }
    )

    result = _run(gateway, FakeTrace())

    assert result.verdict == "duplicate_capture"


def test_pending_refund_reports_remaining_amount() -> None:
    gateway = FakeGateway(
        {
            "evidence_ref": _ref("payment"),
            "data": {
                "order_total_brl": 100,
                "payments": [
                    {"transaction_id": "pay-1", "status": "captured", "amount_brl": 100}
                ],
            },
        },
        {
            "evidence_ref": _ref("refund"),
            "data": {"refunds": [{"refund_id": "refund-1", "status": "pending"}]},
        },
    )
    case = {
        "case_id": "CASE_001",
        "customer_request": {"claims": [{"topic": "requested_full_refund"}]},
    }

    result = _run(gateway, FakeTrace(), case=case)

    assert result.verdict == "refund_pending"
    assert result.refunded_total_brl == 0.0
    assert result.refundable_total_brl == 100.0
    assert len(result.evidence_refs) == 2
