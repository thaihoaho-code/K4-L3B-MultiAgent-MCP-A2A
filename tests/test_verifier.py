from __future__ import annotations

import asyncio
import copy

import pytest

from conftest import ORDER_ID, FakeGateway, make_case, scenario
from student_agent.agent_types import ENTITY_AGENT, VERIFIER, AgentTask
from student_agent.agents.verifier import check_invariants, run_verifier
from student_agent.evidence_cache import CallBudgetExceeded, CaseEvidenceStore
from student_agent.workflow import solve_case


@pytest.fixture
def solved(trace):
    case = make_case(topic="unsupported_claim")
    output = asyncio.run(solve_case(case, FakeGateway(scenario()), trace))
    return case, output


def test_valid_output_passes(solved) -> None:
    case, output = solved
    assert check_invariants(output, case) == ([], [])


@pytest.mark.parametrize(
    ("mutate", "error"),
    [
        (
            lambda o: o["entity_resolution"]["rejected_candidates"].append(ORDER_ID),
            "resolved_and_rejected_overlap",
        ),
        (
            lambda o: o["payment_analysis"].update(captured_total_brl=-1.0),
            "negative_captured_total_brl",
        ),
        (
            lambda o: (
                o["payment_analysis"].update(verdict="refunded"),
                o["financial_resolution"].update(
                    recommended_refund_brl=5.0,
                    refund_lines=[{"reason_code": "X", "amount_brl": 5.0, "entity_id": None}],
                ),
            ),
            "refund_after_completed_refund",
        ),
        (lambda o: o["evidence_refs"].append(o["evidence_refs"][0]), "duplicate_evidence_refs"),
        (
            lambda o: o["financial_resolution"].update(
                recommended_refund_brl=10.0,
                refund_lines=[{"reason_code": "X", "amount_brl": 10.0, "entity_id": None}],
            ),
            "no_action_with_refund",
        ),
        (lambda o: o["resolution_actions"].append(o["resolution_actions"][0]), "duplicate_actions"),
    ],
)
def test_invariant_violations_are_reported(solved, mutate, error) -> None:
    case, output = solved
    broken = copy.deepcopy(output)
    mutate(broken)
    errors, _ = check_invariants(broken, case)
    assert error in errors


def test_schema_failures_block_finalization(solved, trace, contracts) -> None:
    case, output = solved
    broken = copy.deepcopy(output)
    del broken["payment_analysis"]
    store = CaseEvidenceStore(case["case_id"], FakeGateway(scenario()), trace)
    task = AgentTask(
        case["case_id"],
        "T09",
        "coordinator",
        VERIFIER,
        "verify_output",
        {"output": broken, "case": case, "contracts": contracts},
    )
    result = asyncio.run(run_verifier(task, store))
    assert result.data["valid"] is False
    assert result.decision_code == "VERIFY_FAIL"


def test_confidence_out_of_range_is_schema_error(solved, contracts) -> None:
    _, output = solved
    broken = copy.deepcopy(output)
    broken["assessment"]["confidence"] = 1.5
    with pytest.raises(ValueError):
        contracts.validate_output(broken, "draft")


def test_cache_is_case_scoped_and_reuses_calls(trace) -> None:
    gateway = FakeGateway(scenario())
    first = CaseEvidenceStore("L3B_CASE_A01", gateway, trace)
    second = CaseEvidenceStore("L3B_CASE_B01", gateway, trace)
    one = asyncio.run(first.fetch(ENTITY_AGENT, "get_order", order_id=ORDER_ID))
    again = asyncio.run(first.fetch(ENTITY_AGENT, "get_order", order_id=ORDER_ID))
    other = asyncio.run(second.fetch(ENTITY_AGENT, "get_order", order_id=ORDER_ID))
    assert one is again
    assert other.ref != one.ref
    assert len(gateway.calls) == 2
    assert {call[1]["case_id"] for call in gateway.calls} == {"L3B_CASE_A01", "L3B_CASE_B01"}


def test_retry_is_bounded_and_deterministic_errors_are_not_retried(trace) -> None:
    flaky = FakeGateway(scenario(), transient_failures=1)
    store = CaseEvidenceStore("L3B_CASE_A01", flaky, trace, max_retries=1)
    assert asyncio.run(store.fetch(ENTITY_AGENT, "get_order", order_id=ORDER_ID)) is not None
    assert len(flaky.calls) == 2

    broken = FakeGateway(scenario(), transient_failures=5)
    store = CaseEvidenceStore("L3B_CASE_A02", broken, trace, max_retries=1)
    with pytest.raises(ConnectionError):
        asyncio.run(store.fetch(ENTITY_AGENT, "get_order", order_id=ORDER_ID))
    assert len(broken.calls) == 2

    missing = FakeGateway(scenario())
    store = CaseEvidenceStore("L3B_CASE_A03", missing, trace)
    assert asyncio.run(store.fetch(ENTITY_AGENT, "get_order", order_id="nope")) is None
    assert asyncio.run(store.fetch(ENTITY_AGENT, "get_order", order_id="nope")) is None
    assert len(missing.calls) == 1


def test_call_budget_is_enforced(trace) -> None:
    store = CaseEvidenceStore("L3B_CASE_A04", FakeGateway(scenario()), trace, call_budget=1)
    asyncio.run(store.fetch(ENTITY_AGENT, "get_order", order_id=ORDER_ID))
    with pytest.raises(CallBudgetExceeded):
        asyncio.run(store.fetch(ENTITY_AGENT, "get_customer_history", customer_unique_id="x"))
