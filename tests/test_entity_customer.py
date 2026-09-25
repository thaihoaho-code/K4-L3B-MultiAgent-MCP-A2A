from __future__ import annotations

import asyncio

import pytest

from conftest import ORDER_ID, FakeGateway, make_case, scenario
from student_agent.agent_types import ENTITY_AGENT, AgentTask
from student_agent.agents.entity_customer import rank_candidates, run_entity_customer_agent
from student_agent.evidence_cache import CaseEvidenceStore, CrossCaseEvidenceError

OTHER_ID = "fedcba9876543210fedcba9876543210"


def entity_task(case: dict, case_id: str | None = None) -> AgentTask:
    request = case["customer_request"]
    return AgentTask(
        case_id=case_id or case["case_id"],
        task_id="T01",
        from_actor="coordinator",
        to_actor=ENTITY_AGENT,
        task_type="resolve_entity",
        payload={
            "claimed_order_id": request["claimed_order_id"],
            "candidate_order_ids": case["candidate_order_ids"],
            "customer_unique_id_hint": case["customer_unique_id_hint"],
            "opened_at": case["opened_at"],
        },
    )


def run(store, case):
    return asyncio.run(run_entity_customer_agent(entity_task(case), store))


def test_exact_candidate_resolves_and_rejects_decoy(store_factory) -> None:
    store, gateway = store_factory(scenario())
    result = run(store, make_case())
    assert result.data["status"] == "resolved"
    assert result.data["resolved_order_ids"] == [ORDER_ID]
    assert result.data["rejected_candidates"] == ["candidate-decoy"]
    assert result.confidence >= 0.9
    assert result.data["customer_unique_id"] == "customer-test"
    assert result.data["related_order_ids"] == [ORDER_ID]
    # malformed decoy is rejected without spending an MCP call on it
    assert [name for name, _ in gateway.calls] == ["get_customer_history", "get_order"]


def test_two_equally_supported_candidates_are_ambiguous(store_factory) -> None:
    data = scenario()
    data["get_customer_history"]["orders"].append(dict(data["get_order"], order_id=OTHER_ID))
    store, gateway = store_factory(data, known_orders=(OTHER_ID,))
    case = make_case(candidates=[ORDER_ID, OTHER_ID])
    case["customer_request"]["claimed_order_id"] = None
    result = run(store, case)
    assert result.data["status"] == "ambiguous"
    assert result.data["resolved_order_ids"] == []
    assert result.confidence <= 0.6
    assert [name for name, _ in gateway.calls].count("get_order") == 2


def test_no_plausible_candidate_is_not_found(store_factory) -> None:
    data = scenario()
    data["get_customer_history"]["orders"] = []
    store, _ = store_factory(data)
    case = make_case(candidates=["candidate-a", "candidate-b"])
    case["customer_request"]["claimed_order_id"] = "candidate-a"
    result = run(store, case)
    assert result.data["status"] == "not_found"
    assert result.data["resolved_order_ids"] == []
    assert result.data["customer_unique_id"] is None


def test_fuzzy_candidate_maps_to_history_order() -> None:
    typo = ORDER_ID[:-1] + "0"
    ranked = rank_candidates([typo, "candidate-x"], typo, [ORDER_ID])
    assert ranked[0].canonical == ORDER_ID
    assert "history_fuzzy" in ranked[0].signals


def test_scoped_record_is_latest_before_case_opened(store_factory) -> None:
    store, _ = store_factory(scenario())
    result = run(store, make_case())
    scoped = result.data["scoped"]
    assert scoped.row["order_purchase_timestamp"].startswith("2018-03-01")
    assert result.data["conflict_candidates"][0]["field"] == "order_purchase_timestamp"


def test_evidence_from_another_case_is_rejected(trace) -> None:
    gateway = FakeGateway(scenario())
    first = CaseEvidenceStore("L3B_CASE_A01", gateway, trace)
    second = CaseEvidenceStore("L3B_CASE_B01", gateway, trace)
    evidence = asyncio.run(first.fetch(ENTITY_AGENT, "get_order", order_id=ORDER_ID))
    with pytest.raises(CrossCaseEvidenceError):
        second.consume(ENTITY_AGENT, evidence)


def test_record_not_yet_due_at_case_opening_is_not_in_scope() -> None:
    from conftest import order_row
    from student_agent.scoping import select_scoped_record

    due = order_row("2018-08-05", order_status="canceled", order_delivered_customer_date=None)
    in_transit = order_row("2018-08-14")  # estimated 2018-08-24, after the case opened
    scoped = select_scoped_record([due, in_transit], "2018-08-17T09:00:00-03:00")
    assert scoped.row["order_status"] == "canceled"
    assert scoped.window.end is not None
