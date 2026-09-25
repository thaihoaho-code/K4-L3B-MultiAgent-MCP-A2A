from __future__ import annotations

import asyncio
from decimal import Decimal

from conftest import ORDER_ID, SELLER, scenario
from student_agent.agent_types import POLICY_AGENT, AgentTask
from student_agent.agents.conflict_resolver import check_responsibility, resolve
from student_agent.agents.policy import classify_issue, run_policy_agent


def facts(**overrides):
    base = {
        "entity_status": "resolved",
        "order_id": ORDER_ID,
        "order_status": "delivered",
        "seller_ids": [SELLER],
        "late_seller_ids": [],
        "shipment_verdict": "on_time",
        "payment_verdict": "reconciled",
        "captured": Decimal("89.00"),
        "refundable": Decimal("89.00"),
        "split": False,
        "duplicate_amount": Decimal(0),
        "mismatch_amount": Decimal(0),
        "failed_refund_amount": Decimal(0),
        "freight_total": Decimal("18.00"),
    }
    base.update(overrides)
    return base


def decide(store_factory, case_facts, topic):
    task = AgentTask(
        "L3B_CASE_T01",
        "T05",
        "coordinator",
        POLICY_AGENT,
        "decide_policy",
        {
            "facts": case_facts,
            "claim_topics": [topic, "requested_full_refund"],
            "claims": [
                {"claim_id": "a", "topic": topic},
                {"claim_id": "b", "topic": "requested_full_refund"},
            ],
            "policy_version": "TEST_POLICY",
            "evidence_confidence": 0.95,
        },
    )
    store, _ = store_factory(scenario())
    return asyncio.run(run_policy_agent(task, store))


def test_issue_classification_precedence() -> None:
    assert classify_issue(facts(order_status="canceled"))[0] == "canceled_order_paid"
    assert classify_issue(facts(payment_verdict="refund_failed"))[0] == "refund_failed"
    assert classify_issue(facts(split=True))[0] == "valid_split_payment"
    assert classify_issue(facts(shipment_verdict="logistics_delay"))[0] == "late_delivery_logistics"
    assert classify_issue(facts())[0] == "unsupported_claim"
    assert classify_issue(facts(entity_status="ambiguous"))[0] == "insufficient_evidence"


def test_seller_delay_binds_responsibility_to_evidence_seller(store_factory) -> None:
    result = decide(
        store_factory,
        facts(shipment_verdict="seller_delay", late_seller_ids=[SELLER], refundable=Decimal(18)),
        "late_delivery_seller",
    )
    assert result.data["responsible_parties"] == [{"party_type": "seller", "party_id": SELLER}]
    assert result.data["recommended_refund"] == Decimal(18)
    assert result.confidence > 0.9


def test_no_action_policy_never_recommends_refund(store_factory) -> None:
    result = decide(store_factory, facts(), "unsupported_claim")
    assert result.data["case_status"] == "no_action"
    assert result.data["recommended_refund"] == 0
    assert result.data["refund_lines"] == []


def test_claim_disagreeing_with_evidence_lowers_confidence(store_factory) -> None:
    result = decide(store_factory, facts(), "late_delivery_seller")
    assert result.data["primary_issue"] == "unsupported_claim"
    assert result.confidence <= 0.6


def test_insufficient_evidence_creates_no_action_and_no_policy_call(store_factory) -> None:
    result = decide(store_factory, facts(entity_status="not_found"), "refund_failed")
    assert result.data["recommended_refund"] == 0
    assert result.evidence_refs == []


def test_conflicts_resolved_by_field_precedence() -> None:
    conflicts, unresolved = resolve(
        [
            {
                "field": "order_status",
                "sources": ["get_order", "get_customer_history"],
                "preferred_source": "get_customer_history",
                "rule": "case_window_record",
            },
            {"field": "carrier_name", "sources": ["get_order", "get_shipment_summary"]},
        ]
    )
    assert conflicts[0]["selected_source"] == "get_customer_history"
    assert conflicts[0]["resolution_code"] == "CASE_WINDOW_RECORD_SELECTED"
    assert conflicts[1]["selected_source"] is None
    assert conflicts[1]["resolution_code"] == "UNRESOLVED_INSUFFICIENT_EVIDENCE"
    assert unresolved == 1
    assert all(item["resolution_code"] for item in conflicts)


def test_logistics_delay_never_blames_seller() -> None:
    parties, warnings = check_responsibility(
        [{"party_type": "seller", "party_id": SELLER}],
        [SELLER],
        "logistics_delay",
        "late_delivery_logistics",
    )
    assert parties == [{"party_type": "unknown", "party_id": None}]
    assert warnings
