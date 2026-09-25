"""Synthetic A2A and coordinator tests; no real MCP case evidence is used."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from student_agent.a2a import TASK_ACTORS, make_task, wrap_result
from student_agent.agents import (
    EntityAgent,
    OrderShipmentAgent,
    PaymentAgent,
    PolicyAgent,
    VerifierAgent,
)
from student_agent.contracts import Contracts
from student_agent.state import (
    EntityResult,
    FinancialResolution,
    PaymentResult,
    PolicyResult,
    RefundLine,
    ShipmentResult,
)
from student_agent.trace import TraceWriter
from student_agent.workflow import run_case, start_case

REF = "ev_" + "a" * 20
SHIPMENT_REF = "ev_" + "b" * 20
PAYMENT_REF = "ev_" + "c" * 20
POLICY_REF = "ev_" + "d" * 20


def test_actual_main_agents_are_exposed_without_moving_modules() -> None:
    assert [agent.__name__ for agent in (
        EntityAgent, OrderShipmentAgent, PaymentAgent, PolicyAgent, VerifierAgent
    )] == [
        "EntityAgent", "OrderShipmentAgent", "PaymentAgent", "PolicyAgent", "VerifierAgent"
    ]
    assert len(TASK_ACTORS) == 5


def test_a2a_envelope_and_case_state_follow_typed_pipeline() -> None:
    state = start_case({"case_id": "CASE_001", "candidate_order_ids": ["ORDER_1"]})
    entity_task = state.next_task("resolve_entity", {"candidate_count": 1})
    entity = EntityResult("CUSTOMER_1", "ORDER_1", resolution_status="resolved", confidence=0.9)
    state.accept(
        entity_task,
        wrap_result(
            entity_task, entity, status="ok", decision_code="ENTITY_RESOLVED",
            evidence_refs=(REF,), confidence=0.9,
        ),
    )
    assert state.entity is entity
    assert state.evidence_refs == [REF]

    shipment_task = state.next_task(
        "investigate_order_shipment", {"resolved_order_id": entity.resolved_order_id}
    )
    shipment = ShipmentResult(order_ids=["ORDER_1"])
    state.accept(
        shipment_task,
        wrap_result(
            shipment_task, shipment, status="insufficient_evidence",
            decision_code="SHIPMENT_INSUFFICIENT_EVIDENCE", evidence_refs=(REF,),
        ),
    )
    payment_task = state.next_task("investigate_payment_refund")
    payment = PaymentResult()
    state.accept(
        payment_task,
        wrap_result(
            payment_task, payment, status="insufficient_evidence",
            decision_code="PAYMENT_INSUFFICIENT_EVIDENCE",
        ),
    )
    policy_task = state.next_task("decide_policy")
    policy = PolicyResult()
    state.accept(
        policy_task,
        wrap_result(
            policy_task, policy, status="insufficient_evidence",
            decision_code="POLICY_INSUFFICIENT_EVIDENCE",
        ),
    )
    verifier_task = state.next_task("verify_output")
    output = {"case_id": "CASE_001"}
    state.accept(
        verifier_task,
        wrap_result(
            verifier_task, output, status="ok", decision_code="VERIFY_PASS"
        ),
    )

    assert state.shipment is shipment
    assert state.payment is payment
    assert state.policy is policy
    assert state.verification is not None
    assert state.verification.data is output
    assert state.evidence_refs == [REF]
    assert [entity_task.to_actor, shipment_task.to_actor, payment_task.to_actor,
            policy_task.to_actor, verifier_task.to_actor] == list(TASK_ACTORS.values())
    with pytest.raises(ValueError, match="already completed"):
        state.accept(
            entity_task,
            wrap_result(entity_task, entity, status="ok", decision_code="ENTITY_RESOLVED"),
        )


def test_case_state_rejects_cross_case_and_wrong_typed_reply() -> None:
    source = {"case_id": "CASE_001", "candidate_order_ids": ["ORDER_1"]}
    first = start_case(source)
    second = start_case({"case_id": "CASE_002"})
    source["candidate_order_ids"].append("ORDER_2")
    assert first.input_case["candidate_order_ids"] == ["ORDER_1"]
    first_task = first.next_task("resolve_entity")
    second_task = second.next_task("resolve_entity")
    reply = wrap_result(
        first_task, EntityResult(None, None), status="insufficient_evidence",
        decision_code="ENTITY_NOT_FOUND", evidence_refs=(REF,),
    )
    first.accept(first_task, reply)
    with pytest.raises(ValueError, match="case, task, or actor mismatch"):
        second.accept(second_task, reply)
    assert second.entity is None
    assert second.evidence_refs == []

    wrong_type = wrap_result(
        second_task, ShipmentResult(), status="ok", decision_code="ENTITY_RESOLVED"
    )
    with pytest.raises(TypeError, match="must return EntityResult"):
        second.accept(second_task, wrong_type)


def test_invalid_task_and_result_metadata_are_rejected() -> None:
    with pytest.raises(ValueError, match="unknown A2A task type"):
        make_task(case_id="CASE_001", task_type="invented", sequence=1)
    task = make_task(case_id="CASE_001", task_type="resolve_entity", sequence=1)
    with pytest.raises(ValueError, match="invalid evidence_ref syntax"):
        wrap_result(
            task, EntityResult(None, None), status="ok", decision_code="ENTITY_RESOLVED",
            evidence_refs=("ev_fake",),
        )
    with pytest.raises(ValueError, match="confidence"):
        wrap_result(
            task, EntityResult(None, None), status="ok", decision_code="ENTITY_RESOLVED",
            confidence=float("nan"),
        )


class FakeGateway:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.refs = {
            "fake_entity": REF,
            "fake_shipment": SHIPMENT_REF,
            "fake_payment": PAYMENT_REF,
            "fake_policy": POLICY_REF,
        }

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        self.calls.append((tool_name, case_id))
        return {"evidence_ref": self.refs[tool_name], "data": {}}


async def consume(tool: str, gateway: Any, trace: TraceWriter, actor: str) -> str:
    evidence = await gateway.call(tool, case_id=gateway.case_id)
    ref = evidence["evidence_ref"]
    trace.emit(
        case_id=gateway.case_id,
        event_type="tool_result_consumed",
        actor=actor,
        tool_name=tool,
        evidence_refs=[ref],
    )
    return ref


class FakeEntity:
    async def resolve(self, case: dict[str, Any], gateway: Any, trace: TraceWriter):
        ref = await consume("fake_entity", gateway, trace, "entity-agent")
        return EntityResult(
            "CUSTOMER_1", "ORDER_1", resolution_status="resolved",
            confidence=0.9, evidence_refs=[ref],
        )


class FakeShipment:
    async def investigate(self, entity: EntityResult, gateway: Any, trace: TraceWriter):
        ref = await consume("fake_shipment", gateway, trace, "order-shipment-agent")
        return ShipmentResult(
            verdict="seller_delay", order_ids=[entity.resolved_order_id],
            evidence_refs=[ref],
        )


class FakePayment:
    async def check_transactions(
        self, entity: EntityResult, gateway: Any, trace: TraceWriter
    ):
        ref = await consume("fake_payment", gateway, trace, "payment-refund-agent")
        return PaymentResult(
            verdict="refund_pending", captured_total_brl=100,
            refunded_total_brl=0, refundable_total_brl=100, evidence_refs=[ref],
        )


class FakePolicy:
    async def resolve_conflict(
        self,
        case: dict[str, Any],
        entity: EntityResult,
        shipment: ShipmentResult,
        payment: PaymentResult,
        gateway: Any,
        trace: TraceWriter,
    ):
        ref = await consume("fake_policy", gateway, trace, "policy-agent")
        trace.emit(
            case_id=case["case_id"], event_type="policy_decided",
            actor="policy-agent", decision_code="EARLY_DECISION",
        )
        return PolicyResult(
            primary_issue="refund_pending", case_status="action_required",
            confidence=0.8, evidence_refs=[ref],
        )


def writer(tmp_path: Path) -> TraceWriter:
    root = Path(__file__).resolve().parents[1]
    return TraceWriter(tmp_path / "trace.jsonl", Contracts(root / "contracts" / "schemas"))


def events(trace: TraceWriter) -> list[dict[str, Any]]:
    return [json.loads(line) for line in trace.path.read_text(encoding="utf-8").splitlines()]


def test_live_dispatch_shape_uses_actual_handoffs(tmp_path: Path) -> None:
    gateway = FakeGateway()
    trace = writer(tmp_path)
    output = asyncio.run(
        run_case(
            {"case_id": "CASE_010"}, gateway, trace,
            entity_agent=FakeEntity(), order_shipment_agent=FakeShipment(),
            payment_agent=FakePayment(), policy_agent=FakePolicy(),
        )
    )
    trace.contracts.validate_output(output, "test output")
    assert output["entity_resolution"]["resolved_order_ids"] == ["ORDER_1"]
    assert output["evidence_refs"] == [REF, SHIPMENT_REF, PAYMENT_REF, POLICY_REF]
    assert len(gateway.calls) == 4
    entries = events(trace)
    assert [event["target"] for event in entries if event["event_type"] == "task_assigned"] == [
        "entity-agent", "order-shipment-agent", "payment-refund-agent", "policy-agent",
        "verifier",
    ]
    assert len([event for event in entries if event["event_type"] == "handoff"]) == 5
    assert len([event for event in entries if event["event_type"] == "tool_result_consumed"]) == 4
    assert len([event for event in entries if event["event_type"] == "policy_decided"]) == 1
    assert [event["decision_code"] for event in entries
            if event["event_type"] == "verification_completed"] == ["VERIFY_CONSISTENCY_PASS"]
    assert "CUSTOMER_1" not in trace.path.read_text(encoding="utf-8")


def test_ambiguous_entity_skips_order_payment_and_policy(tmp_path: Path) -> None:
    class AmbiguousEntity:
        async def resolve(self, case: dict[str, Any], gateway: Any, trace: TraceWriter):
            return EntityResult(None, None, resolution_status="ambiguous", confidence=0.3)

    class UnexpectedAgent:
        async def investigate(self, *args: Any):
            raise AssertionError("shipment must not be called")

        async def check_transactions(self, *args: Any):
            raise AssertionError("payment must not be called")

        async def resolve_conflict(self, *args: Any):
            raise AssertionError("policy must not be called")

    gateway = FakeGateway()
    trace = writer(tmp_path)
    output = asyncio.run(
        run_case(
            {"case_id": "CASE_011"}, gateway, trace,
            entity_agent=AmbiguousEntity(), order_shipment_agent=UnexpectedAgent(),
            payment_agent=UnexpectedAgent(), policy_agent=UnexpectedAgent(),
        )
    )
    assert gateway.calls == []
    assert output["entity_resolution"]["status"] == "ambiguous"
    assert output["assessment"]["case_status"] == "needs_investigation"
    assert [event["target"] for event in events(trace)
            if event["event_type"] == "task_assigned"] == ["entity-agent", "verifier"]


def test_cross_case_mcp_call_has_no_successful_handoff(tmp_path: Path) -> None:
    class WrongCaseEntity:
        async def resolve(self, case: dict[str, Any], gateway: Any, trace: TraceWriter):
            await gateway.call("fake_entity", case_id="CASE_999")
            raise AssertionError("unreachable")

    trace = writer(tmp_path)
    with pytest.raises(ValueError, match="another case_id"):
        asyncio.run(
            run_case({"case_id": "CASE_012"}, FakeGateway(), trace,
                     entity_agent=WrongCaseEntity())
        )
    assert [event["event_type"] for event in events(trace)] == ["task_assigned"]


def test_unobserved_evidence_ref_cannot_enter_handoff(tmp_path: Path) -> None:
    class InventedRefEntity:
        async def resolve(self, case: dict[str, Any], gateway: Any, trace: TraceWriter):
            return EntityResult(
                "CUSTOMER_1", "ORDER_1", resolution_status="resolved",
                evidence_refs=[REF],
            )

    trace = writer(tmp_path)
    with pytest.raises(ValueError, match="not observed in this case"):
        asyncio.run(
            run_case({"case_id": "CASE_013"}, FakeGateway(), trace,
                     entity_agent=InventedRefEntity())
        )
    assert [event["event_type"] for event in events(trace)] == ["task_assigned"]


def test_invalid_verifier_output_emits_one_failure_event(tmp_path: Path) -> None:
    class InvalidVerifier:
        def verify_and_format(self, *args: Any):
            trace = args[-1]
            trace.emit(
                case_id=args[0]["case_id"], event_type="verification_completed",
                actor="verifier", decision_code="EARLY_SUCCESS",
            )
            return {"case_id": args[0]["case_id"]}

    trace = writer(tmp_path)
    with pytest.raises(ValueError):
        asyncio.run(
            run_case({"case_id": "CASE_014"}, FakeGateway(), trace,
                     verifier_agent=InvalidVerifier())
        )
    assert [event["decision_code"] for event in events(trace)
            if event["event_type"] == "verification_completed"] == ["VERIFY_FAIL"]
    assert [event["target"] for event in events(trace)
            if event["event_type"] == "handoff"] == ["coordinator"]


def test_verifier_cannot_change_accepted_entity_or_policy_finding(tmp_path: Path) -> None:
    class ChangedFindingVerifier(VerifierAgent):
        def verify_and_format(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            output = super().verify_and_format(*args, **kwargs)
            output["entity_resolution"]["resolved_order_ids"] = ["ORDER_2"]
            return output

    trace = writer(tmp_path)
    with pytest.raises(ValueError, match="entity_resolution"):
        asyncio.run(
            run_case(
                {"case_id": "CASE_015"}, FakeGateway(), trace,
                entity_agent=FakeEntity(), order_shipment_agent=FakeShipment(),
                payment_agent=FakePayment(), policy_agent=FakePolicy(),
                verifier_agent=ChangedFindingVerifier(),
            )
        )
    assert [event["decision_code"] for event in events(trace)
            if event["event_type"] == "verification_completed"] == ["VERIFY_FAIL"]


def test_policy_refund_must_equal_lines_and_actions_cannot_be_truncated(tmp_path: Path) -> None:
    class InconsistentPolicy(FakePolicy):
        async def resolve_conflict(self, *args: Any) -> PolicyResult:
            result = await super().resolve_conflict(*args)
            result.financial_resolution = FinancialResolution(
                recommended_refund_brl=20,
                refund_lines=[RefundLine("REFUND", 10)],
            )
            return result

    trace = writer(tmp_path)
    with pytest.raises(ValueError, match="refund line total"):
        asyncio.run(
            run_case(
                {"case_id": "CASE_016"}, FakeGateway(), trace,
                entity_agent=FakeEntity(), order_shipment_agent=FakeShipment(),
                payment_agent=FakePayment(), policy_agent=InconsistentPolicy(),
            )
        )

    class TooManyActions(FakePolicy):
        async def resolve_conflict(self, *args: Any) -> PolicyResult:
            result = await super().resolve_conflict(*args)
            result.resolution_actions = [f"ACTION_{index}" for index in range(9)]
            return result

    second_trace = writer(tmp_path / "actions")
    with pytest.raises(ValueError, match="silent truncation"):
        asyncio.run(
            run_case(
                {"case_id": "CASE_017"}, FakeGateway(), second_trace,
                entity_agent=FakeEntity(), order_shipment_agent=FakeShipment(),
                payment_agent=FakePayment(), policy_agent=TooManyActions(),
            )
        )


def test_claim_reference_must_belong_to_accepted_handoffs(tmp_path: Path) -> None:
    class InventedClaimRefVerifier(VerifierAgent):
        def verify_and_format(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            output = super().verify_and_format(*args, **kwargs)
            output["claim_assessments"] = [{
                "claim_id": "CLAIM_1", "verdict": "supported", "confidence": 0.8,
                "evidence_refs": ["ev_" + "z" * 20],
            }]
            return output

    trace = writer(tmp_path)
    with pytest.raises(ValueError, match="claim cites evidence"):
        asyncio.run(
            run_case(
                {"case_id": "CASE_018"}, FakeGateway(), trace,
                entity_agent=FakeEntity(), order_shipment_agent=FakeShipment(),
                payment_agent=FakePayment(), policy_agent=FakePolicy(),
                verifier_agent=InventedClaimRefVerifier(),
            )
        )


def test_nonfinite_verifier_number_is_rejected_before_serialization(tmp_path: Path) -> None:
    class InvalidNumberVerifier(VerifierAgent):
        def verify_and_format(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            output = super().verify_and_format(*args, **kwargs)
            output["payment_analysis"]["captured_total_brl"] = float("nan")
            return output

    trace = writer(tmp_path)
    with pytest.raises(ValueError, match="strict JSON"):
        asyncio.run(
            run_case(
                {"case_id": "CASE_019"}, FakeGateway(), trace,
                entity_agent=FakeEntity(), order_shipment_agent=FakeShipment(),
                payment_agent=FakePayment(), policy_agent=FakePolicy(),
                verifier_agent=InvalidNumberVerifier(),
            )
        )
