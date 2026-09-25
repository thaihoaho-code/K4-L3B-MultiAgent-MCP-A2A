from __future__ import annotations

import asyncio

from conftest import ORDER_ID, SELLER, scenario
from student_agent.agent_types import SHIPMENT_AGENT, AgentTask
from student_agent.agents.shipment import classify, run_shipment_agent
from student_agent.scoping import select_scoped_record

OPENED = "2018-03-20T09:00:00-03:00"


def shipment_result(store_factory, data):
    scoped = select_scoped_record(data["get_customer_history"]["orders"], OPENED)
    task = AgentTask(
        "L3B_CASE_T01",
        "T03",
        "coordinator",
        SHIPMENT_AGENT,
        "investigate_shipment",
        {"order_id": ORDER_ID, "window": scoped.window, "scoped_row": scoped.row},
    )
    store, _ = store_factory(data)
    return asyncio.run(run_shipment_agent(task, store))


def late_event(actor: str, day: str = "2018-03-13") -> dict:
    return {
        "event_at": f"{day}T09:00:00-03:00",
        "event_type": "delivered_late",
        "actor": actor,
        "status": "confirmed",
    }


def test_seller_handoff_after_limit_is_seller_delay(store_factory) -> None:
    data = scenario(
        carrier_days=5, limit_days=3, delivered_days=12, shipment_events=[late_event("seller")]
    )
    result = shipment_result(store_factory, data)
    analysis = result.data["shipment_analysis"]
    assert analysis == {
        "verdict": "seller_delay",
        "late_seller_ids": [SELLER],
        "timeline_complete": True,
    }


def test_on_time_handoff_with_late_delivery_is_logistics_delay(store_factory) -> None:
    data = scenario(
        carrier_days=2,
        limit_days=3,
        delivered_days=12,
        shipment_events=[late_event("logistics_provider")],
    )
    analysis = shipment_result(store_factory, data).data["shipment_analysis"]
    assert analysis["verdict"] == "logistics_delay"
    assert analysis["late_seller_ids"] == []


def test_lost_and_returned_events() -> None:
    row = {"order_status": "shipped", "order_delivered_carrier_date": "2018-03-03T09:00:00-03:00"}
    lost = classify(row, [], [{"event_type": "shipment_lost", "status": "confirmed"}])
    returned = classify(row, [], [{"event_type": "returned_to_seller", "status": "confirmed"}])
    assert lost["verdict"] == "lost"
    assert returned["verdict"] == "returned"


def test_event_contradicting_timeline_becomes_conflict_candidate(store_factory) -> None:
    data = scenario(
        carrier_days=2, limit_days=3, delivered_days=12, shipment_events=[late_event("seller")]
    )
    result = shipment_result(store_factory, data)
    assert result.data["shipment_analysis"]["verdict"] == "logistics_delay"
    fields = [item["field"] for item in result.data["conflict_candidates"]]
    assert "delivery_delay_attribution" in fields


def test_missing_delivery_marks_timeline_incomplete(store_factory) -> None:
    data = scenario(status="canceled", delivered_days=None)
    analysis = shipment_result(store_factory, data).data["shipment_analysis"]
    assert analysis["timeline_complete"] is False
    assert analysis["verdict"] == "insufficient_evidence"


def test_out_of_window_events_are_ignored(store_factory) -> None:
    data = scenario(shipment_events=[late_event("seller", day="2019-01-12")])
    analysis = shipment_result(store_factory, data).data["shipment_analysis"]
    assert analysis["verdict"] == "on_time"
