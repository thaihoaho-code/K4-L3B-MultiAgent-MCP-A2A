from __future__ import annotations

import asyncio

from conftest import ITEM, ORDER_ID, SELLER, scenario
from student_agent.agent_types import ORDER_AGENT, AgentTask
from student_agent.agents.order_product import run_order_product_agent, run_seller_verification
from student_agent.scoping import select_scoped_record


def order_task(data: dict, task_type: str = "investigate_order", **payload) -> AgentTask:
    scoped = select_scoped_record(
        data["get_customer_history"]["orders"], "2018-03-13T09:00:00-03:00"
    )
    base = {"order_id": ORDER_ID, "window": scoped.window, "scoped_row": scoped.row}
    return AgentTask(
        "L3B_CASE_T01", "T02", "coordinator", ORDER_AGENT, task_type, {**base, **payload}
    )


def test_items_are_scoped_and_deduplicated(store_factory) -> None:
    data = scenario()
    store, _ = store_factory(data)
    result = asyncio.run(run_order_product_agent(order_task(data), store))
    assert result.data["item_ids"] == [ITEM]
    assert result.data["seller_ids"] == [SELLER]
    # the distractor item (freight 99) belongs to another period and is excluded
    assert str(result.data["freight_total"]) == "10.00"
    assert str(result.data["order_value"]) == "89.00"


def test_canceled_order_is_exposed_as_fact(store_factory) -> None:
    data = scenario(status="canceled", delivered_days=None, captures=[("credit_card", "79.00")])
    store, _ = store_factory(data)
    result = asyncio.run(run_order_product_agent(order_task(data), store))
    assert "ORDER_CANCELED" in result.data["root_cause_hints"]


def test_seller_verification_flags_unknown_sellers(store_factory) -> None:
    data = scenario()
    store, _ = store_factory(data)
    task = order_task(data, "verify_seller", seller_ids=[SELLER, "seller-ghost"])
    result = asyncio.run(run_seller_verification(task, store))
    assert result.data["verified_seller_ids"] == [SELLER]
    assert result.data["unknown_seller_ids"] == ["seller-ghost"]
