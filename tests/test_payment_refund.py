from __future__ import annotations

import asyncio
from decimal import Decimal

from conftest import ORDER_ID, scenario
from student_agent.agent_types import PAYMENT_AGENT, AgentTask
from student_agent.agents.payment_refund import run_payment_refund_agent
from student_agent.scoping import select_scoped_record

OPENED = "2018-03-13T09:00:00-03:00"


def payment(store_factory, data, topics=("unsupported_claim",), order_value="89.00"):
    scoped = select_scoped_record(data["get_customer_history"]["orders"], OPENED)
    task = AgentTask(
        "L3B_CASE_T01",
        "T04",
        "coordinator",
        PAYMENT_AGENT,
        "investigate_payment",
        {
            "order_id": ORDER_ID,
            "window": scoped.window,
            "order_value": Decimal(order_value) if order_value else None,
            "claim_topics": list(topics),
        },
    )
    store, gateway = store_factory(data)
    return asyncio.run(run_payment_refund_agent(task, store)), gateway


def refund_event(status: str, amount: str = "89.00") -> dict:
    return {
        "event_at": "2018-03-12T09:00:00-03:00",
        "event_type": "refund_requested",
        "amount_brl": amount,
        "status": status,
    }


def test_single_capture_is_reconciled_and_skips_refund_lookup(store_factory) -> None:
    result, gateway = payment(store_factory, scenario())
    analysis = result.data["payment_analysis"]
    assert analysis == {
        "verdict": "reconciled",
        "captured_total_brl": 89.0,
        "refunded_total_brl": 0.0,
        "refundable_total_brl": 89.0,
    }
    assert "get_refund_timeline" not in gateway.tools()
    assert result.data["payment_references"] == [f"{ORDER_ID}:1"]


def test_valid_split_payment_is_not_duplicate(store_factory) -> None:
    data = scenario(captures=[("credit_card", "44.50"), ("voucher", "44.50")])
    result, _ = payment(store_factory, data)
    assert result.data["payment_analysis"]["verdict"] == "reconciled"
    assert result.data["facts"]["split"] is True
    assert len(result.data["payment_references"]) == 2


def test_repeated_capture_above_order_value_is_duplicate(store_factory) -> None:
    data = scenario(captures=[("credit_card", "64.00"), ("voucher", "64.00")])
    result, _ = payment(store_factory, data)
    assert result.data["payment_analysis"]["verdict"] == "duplicate_capture"
    assert result.data["facts"]["duplicate_amount"] == Decimal("64.00")


def test_open_reconciliation_mismatch_is_capture_mismatch(store_factory) -> None:
    mismatch = {
        "event_at": "2018-03-01T12:00:00-03:00",
        "event_type": "reconciliation_mismatch",
        "amount_brl": "35.00",
        "status": "open",
    }
    data = scenario(captures=[("credit_card", "35.00")], payment_events=[mismatch])
    result, _ = payment(store_factory, data)
    assert result.data["payment_analysis"]["verdict"] == "capture_mismatch"


def test_refund_states(store_factory) -> None:
    pending, gateway = payment(
        store_factory, scenario(refunds=[refund_event("pending")]), topics=["refund_pending"]
    )
    failed, _ = payment(
        store_factory, scenario(refunds=[refund_event("failed", "52.00")]), topics=["refund_failed"]
    )
    done, _ = payment(
        store_factory, scenario(refunds=[refund_event("completed")]), topics=["refund_pending"]
    )
    assert pending.data["payment_analysis"]["verdict"] == "refund_pending"
    assert "get_refund_timeline" in gateway.tools()
    assert failed.data["payment_analysis"]["verdict"] == "refund_failed"
    assert done.data["payment_analysis"] == {
        "verdict": "refunded",
        "captured_total_brl": 89.0,
        "refunded_total_brl": 89.0,
        "refundable_total_brl": 0.0,
    }


def test_missing_payment_evidence_is_insufficient(store_factory) -> None:
    data = scenario()
    data["get_payment_timeline"] = None
    data["get_order_payments"] = None
    result, _ = payment(store_factory, data)
    assert result.data["payment_analysis"]["verdict"] == "insufficient_evidence"
    assert result.data["payment_analysis"]["captured_total_brl"] is None


def test_out_of_window_captures_are_excluded(store_factory) -> None:
    result, _ = payment(store_factory, scenario())  # the distractor holds a 52.00 capture
    assert result.data["payment_analysis"]["captured_total_brl"] == 89.0
    assert all(
        value >= 0 for value in result.data["payment_analysis"].values() if isinstance(value, float)
    )


def test_simultaneous_transactions_are_separated_by_payment_sequence(store_factory) -> None:
    # A 52.00 single payment and a 44.50 + 44.50 split share the exact same timestamps.
    data = scenario(captures=[("credit_card", "52.00")], distractor=False)
    timeline = data["get_payment_timeline"]
    timeline["payments"] += [
        {
            "order_id": ORDER_ID,
            "payment_sequential": "1",
            "payment_type": "credit_card",
            "payment_installments": "1",
            "payment_value": "44.50",
        },
        {
            "order_id": ORDER_ID,
            "payment_sequential": "2",
            "payment_type": "voucher",
            "payment_installments": "1",
            "payment_value": "44.50",
        },
    ]
    timeline["events"] += [
        {
            "event_at": "2018-03-01T10:00:00-03:00",
            "event_type": "captured",
            "amount_brl": "44.50",
            "status": "confirmed",
        },
        {
            "event_at": "2018-03-01T11:00:00-03:00",
            "event_type": "captured",
            "amount_brl": "44.50",
            "status": "confirmed",
        },
    ]
    result, _ = payment(store_factory, data, topics=["valid_split_payment"])
    assert result.data["payment_analysis"]["verdict"] == "reconciled"
    assert result.data["payment_analysis"]["captured_total_brl"] == 89.0
    assert result.data["facts"]["split"] is True
    assert result.data["conflict_candidates"][0]["field"] == "payment_transaction"
    assert result.confidence <= 0.7
