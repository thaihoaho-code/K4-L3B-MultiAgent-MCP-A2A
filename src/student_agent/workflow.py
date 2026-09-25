"""Coordinator: the only actor that assigns tasks, merges handoffs and finalizes output.

Flow per case (sequential, one shared MCP session):

    entity-agent -> [order-product-agent -> shipment-agent -> payment-refund-agent]
                 -> policy-agent -> (order-product-agent: verify_seller, only on seller blame)
                 -> conflict-resolver -> assemble draft -> verifier -> (one repair + re-verify)

Specialists run only when the entity is resolved, so an ambiguous or unknown entity never
triggers broad MCP scans. All state is per case; nothing is shared between cases.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from .a2a import A2ABus
from .agent_types import AgentResult, AgentTask, Status
from .agents import (
    run_conflict_resolver,
    run_entity_customer_agent,
    run_order_product_agent,
    run_payment_refund_agent,
    run_policy_agent,
    run_seller_verification,
    run_shipment_agent,
    run_verifier,
)
from .evidence_cache import CaseEvidenceStore, Gateway, TraceSink
from .money import TOLERANCE, to_brl, to_decimal

SCHEMA_VERSION = "day09-l3b-output-v2"
MAX_EVIDENCE_REFS = 30
PAYMENT_ISSUES = {
    "valid_split_payment",
    "payment_mismatch",
    "duplicate_charge",
    "refund_pending",
    "refund_failed",
}
SHIPMENT_ISSUES = {"late_delivery_seller", "late_delivery_logistics"}
ISSUE_TOOLS = {
    "shipment": {"get_order", "get_customer_history", "get_shipment_summary", "get_sellers"},
    "payment": {"get_order", "get_customer_history", "get_payment_timeline", "get_refund_timeline"},
    "order": {"get_order", "get_customer_history", "get_order_items", "get_payment_timeline"},
}
REFUND_TOOLS = {"get_payment_timeline", "get_refund_timeline", "get_order_items", "get_policy"}


@dataclass
class CaseState:
    case_id: str
    case: dict[str, Any]
    entity: AgentResult | None = None
    order: AgentResult | None = None
    shipment: AgentResult | None = None
    payment: AgentResult | None = None
    policy: AgentResult | None = None
    seller_check: AgentResult | None = None
    conflicts: AgentResult | None = None
    verification: AgentResult | None = None
    notes: list[str] = field(default_factory=list)


class Coordinator:
    def __init__(self, case: dict[str, Any], gateway: Gateway, trace: TraceSink) -> None:
        self.case = deepcopy(case)
        self.case_id = str(self.case["case_id"])
        self.trace = trace
        self.contracts = getattr(trace, "contracts", None)
        self.store = CaseEvidenceStore(self.case_id, gateway, trace)
        self.bus = A2ABus(self.case_id, trace, self.store)
        self.state = CaseState(self.case_id, self.case)

    async def _run(self, task_type: str, agent: Any, payload: dict[str, Any]) -> AgentResult:
        async def handler(task: AgentTask) -> AgentResult:
            try:
                return await agent(task, self.store)
            except Exception as exc:  # noqa: BLE001 - agent failures degrade, never crash a run
                return AgentResult.reply(
                    task,
                    status=Status.ERROR,
                    confidence=0.0,
                    decision_code="AGENT_ERROR",
                    evidence_refs=[],
                    warnings=[type(exc).__name__],
                )

        return await self.bus.dispatch(
            task_type, handler, payload, evidence_refs=self.store.consumed_refs()
        )

    # ----------------------------------------------------------------- orchestration
    async def run(self) -> dict[str, Any]:
        request = self.case.get("customer_request") or {}
        claims = [claim for claim in request.get("claims") or [] if isinstance(claim, dict)]
        topics = [str(claim.get("topic")) for claim in claims if claim.get("topic")]
        state = self.state

        state.entity = await self._run(
            "resolve_entity",
            run_entity_customer_agent,
            {
                "claimed_order_id": request.get("claimed_order_id"),
                "candidate_order_ids": list(self.case.get("candidate_order_ids") or []),
                "customer_unique_id_hint": self.case.get("customer_unique_id_hint"),
                "opened_at": self.case.get("opened_at"),
            },
        )
        entity = state.entity.data
        resolved = entity.get("resolved_order_ids") or []
        scoped = entity.get("scoped")
        if state.entity.status == Status.OK and resolved and scoped is not None:
            await self._investigate(resolved[0], scoped, topics, claims)
        draft = self._assemble(claims)
        return await self._verify(draft)

    async def _investigate(
        self, order_id: str, scoped: Any, topics: list[str], claims: list[dict[str, Any]]
    ) -> None:
        state = self.state
        scope = self.case.get("investigation_scope") or {}
        state.order = await self._run(
            "investigate_order",
            run_order_product_agent,
            {
                "order_id": order_id,
                "window": scoped.window,
                "scoped_row": scoped.row,
                "include_product_context": scope.get("include_product_context", True),
            },
        )
        state.shipment = await self._run(
            "investigate_shipment",
            run_shipment_agent,
            {"order_id": order_id, "window": scoped.window, "scoped_row": scoped.row},
        )
        state.payment = await self._run(
            "investigate_payment",
            run_payment_refund_agent,
            {
                "order_id": order_id,
                "window": scoped.window,
                "order_value": state.order.data.get("order_value"),
                "claim_topics": topics,
            },
        )
        facts = self._facts(order_id, scoped)
        state.policy = await self._run(
            "decide_policy",
            run_policy_agent,
            {
                "facts": facts,
                "claim_topics": topics,
                "claims": claims,
                "policy_version": self.case.get("policy_version") or "",
                "evidence_confidence": state.entity.confidence,
            },
        )
        parties = state.policy.data.get("responsible_parties", [])
        blamed = [p["party_id"] for p in parties if p["party_type"] == "seller" and p["party_id"]]
        if blamed:
            state.seller_check = await self._run(
                "verify_seller",
                run_seller_verification,
                {"order_id": order_id, "seller_ids": blamed},
            )
            unknown = set(state.seller_check.data.get("unknown_seller_ids", []))
            if unknown:
                parties = [
                    {**p, "party_id": None} if p["party_id"] in unknown else p for p in parties
                ]
        candidates = [
            *state.entity.data.get("conflict_candidates", []),
            *(state.shipment.data.get("conflict_candidates", []) if state.shipment else []),
            *(state.payment.data.get("conflict_candidates", []) if state.payment else []),
        ]
        state.conflicts = await self._run(
            "resolve_conflicts",
            run_conflict_resolver,
            {
                "conflict_candidates": candidates,
                "responsible_parties": parties,
                "seller_ids": state.order.data.get("seller_ids", []),
                "shipment_verdict": self._shipment()["verdict"],
                "primary_issue": state.policy.data.get("primary_issue"),
                "supporting_refs": [
                    ref
                    for ref in (
                        state.entity.data.get("history_ref"),
                        state.entity.data.get("order_ref"),
                        *(state.shipment.evidence_refs if state.shipment else []),
                    )
                    if ref
                ],
            },
        )

    def _facts(self, order_id: str, scoped: Any) -> dict[str, Any]:
        state = self.state
        order = state.order.data if state.order else {}
        payment = state.payment.data if state.payment else {}
        payment_facts = payment.get("facts") or {}
        shipment = self._shipment()
        return {
            "entity_status": state.entity.data.get("status"),
            "order_id": order_id,
            "order_status": scoped.row.get("order_status"),
            "seller_ids": order.get("seller_ids", []),
            "late_seller_ids": shipment["late_seller_ids"],
            "shipment_verdict": shipment["verdict"],
            "payment_verdict": self._payment()["verdict"],
            "captured": payment_facts.get("captured"),
            "refundable": payment_facts.get("refundable"),
            "split": payment_facts.get("split", False),
            "duplicate_amount": payment_facts.get("duplicate_amount"),
            "mismatch_amount": payment_facts.get("mismatch_amount"),
            "failed_refund_amount": payment_facts.get("failed_refund_amount"),
            "freight_total": order.get("freight_total"),
        }

    # ----------------------------------------------------------------- assembly
    def _shipment(self) -> dict[str, Any]:
        result = self.state.shipment
        if result and result.status != Status.ERROR and "shipment_analysis" in result.data:
            return dict(result.data["shipment_analysis"])
        return {
            "verdict": "insufficient_evidence",
            "late_seller_ids": [],
            "timeline_complete": False,
        }

    def _payment(self) -> dict[str, Any]:
        result = self.state.payment
        if result and result.status != Status.ERROR and "payment_analysis" in result.data:
            return dict(result.data["payment_analysis"])
        return {
            "verdict": "insufficient_evidence",
            "captured_total_brl": None,
            "refunded_total_brl": None,
            "refundable_total_brl": None,
        }

    def _issue_confidence(self, issue: str) -> float:
        state = self.state
        confidence = state.policy.confidence if state.policy else 0.3
        domain = None
        if issue in SHIPMENT_ISSUES:
            domain = state.shipment
        elif issue in PAYMENT_ISSUES:
            domain = state.payment
        elif issue in {"canceled_order_paid", "unavailable_order_paid"}:
            domain = state.order
        if domain is not None:
            confidence = min(confidence, max(domain.confidence, 0.3))
        if state.conflicts and state.conflicts.data.get("unresolved"):
            confidence = min(confidence, 0.7)
        return round(confidence, 4)

    def _claim_refs(self, kind: str, issue: str) -> list[str]:
        if kind == "refund":
            tools = REFUND_TOOLS
        elif issue in SHIPMENT_ISSUES or issue == "unsupported_claim":
            tools = ISSUE_TOOLS["shipment"] | {"get_payment_timeline", "get_policy"}
        elif issue in PAYMENT_ISSUES:
            tools = ISSUE_TOOLS["payment"] | {"get_order_items", "get_policy"}
        else:
            tools = ISSUE_TOOLS["order"] | {"get_policy"}
        refs = []
        for ref in self.store.consumed_refs():
            evidence = self.store.get(ref)
            if evidence is not None and evidence.tool in tools:
                refs.append(ref)
        return refs[:MAX_EVIDENCE_REFS]

    def _assemble(self, claims: list[dict[str, Any]]) -> dict[str, Any]:
        state = self.state
        entity = state.entity.data if state.entity else {}
        order = state.order.data if state.order and state.order.status != Status.ERROR else {}
        payment = (
            state.payment.data if state.payment and state.payment.status != Status.ERROR else {}
        )
        policy = state.policy.data if state.policy and state.policy.status != Status.ERROR else {}
        conflicts = state.conflicts.data if state.conflicts else {}

        issue = policy.get("primary_issue", "insufficient_evidence")
        case_status = policy.get("case_status", "needs_investigation")
        action = policy.get("recommended_action", "escalate_investigation")
        refund: Decimal = policy.get("recommended_refund", Decimal(0))
        parties = (
            conflicts.get("responsible_parties")
            or policy.get("responsible_parties")
            or [{"party_type": "unknown", "party_id": None}]
        )
        confidence = self._issue_confidence(issue) if policy else 0.3

        claim_assessments = []
        for verdict in policy.get("claim_verdicts", []):
            if not verdict.get("claim_id"):
                continue
            refs = self._claim_refs(verdict["kind"], issue)
            claim_confidence = confidence if verdict["verdict"] != "insufficient_evidence" else 0.5
            claim_assessments.append(
                {
                    "claim_id": str(verdict["claim_id"])[:64],
                    "verdict": verdict["verdict"],
                    "confidence": round(claim_confidence, 4),
                    "evidence_refs": refs,
                }
            )
        if not claim_assessments and claims and issue == "insufficient_evidence":
            claim_assessments = [
                {
                    "claim_id": str(claim.get("claim_id"))[:64],
                    "verdict": "insufficient_evidence",
                    "confidence": 0.3,
                    "evidence_refs": [],
                }
                for claim in claims[:5]
                if claim.get("claim_id")
            ]

        output = {
            "schema_version": SCHEMA_VERSION,
            "case_id": self.case_id,
            "assessment": {
                "primary_issue": issue,
                "secondary_issues": [],
                "case_status": case_status,
                "confidence": confidence,
            },
            "affected_entities": {
                "order_ids": list(entity.get("resolved_order_ids") or []),
                "item_ids": order.get("item_ids", [])[:20],
                "seller_ids": order.get("seller_ids", [])[:20],
                "payment_references": payment.get("payment_references", [])[:20],
                "shipment_ids": [],
            },
            "claim_assessments": claim_assessments,
            "entity_resolution": {
                "status": entity.get("status", "not_found"),
                "resolved_order_ids": list(entity.get("resolved_order_ids") or []),
                "rejected_candidates": list(entity.get("rejected_candidates") or [])[:20],
                "confidence": state.entity.confidence if state.entity else 0.0,
            },
            "customer_context": {
                "customer_unique_id": entity.get("customer_unique_id"),
                "related_order_ids": list(entity.get("related_order_ids") or [])[:20],
            },
            "shipment_analysis": self._shipment(),
            "payment_analysis": self._payment(),
            "root_cause_analysis": {
                "ranked_causes": policy.get(
                    "ranked_causes", [{"cause_code": "INSUFFICIENT_EVIDENCE", "rank": 1}]
                ),
                "responsible_parties": parties,
            },
            "evidence_refs": self.store.consumed_refs()[:MAX_EVIDENCE_REFS],
            "data_conflicts": conflicts.get("data_conflicts", []),
            "financial_resolution": {
                "currency": "BRL",
                "recommended_refund_brl": to_brl(refund) or 0.0,
                "refund_lines": policy.get("refund_lines", []),
            },
            "resolution_actions": [action],
        }
        if not output["claim_assessments"]:
            del output["claim_assessments"]
        return output

    # ----------------------------------------------------------------- verification
    async def _verify(self, draft: dict[str, Any]) -> dict[str, Any]:
        scoped = self.state.entity.data.get("scoped") if self.state.entity else None
        payload = {
            "case": self.case,
            "contracts": self.contracts,
            "scoped_purchase": scoped.row.get("order_purchase_timestamp") if scoped else None,
        }
        result = await self._run("verify_output", run_verifier, {**payload, "output": draft})
        self.state.verification = result
        if result.data.get("valid"):
            return draft
        repaired = repair(draft, result.data.get("errors", []), set(self.store.consumed_refs()))
        result = await self._run("verify_output", run_verifier, {**payload, "output": repaired})
        self.state.verification = result
        if not result.data.get("valid"):
            repaired["assessment"]["case_status"] = "needs_investigation"
            repaired["assessment"]["confidence"] = min(repaired["assessment"]["confidence"], 0.4)
        return repaired


def repair(output: dict[str, Any], errors: list[str], consumed: set[str]) -> dict[str, Any]:
    """Deterministic, bounded fixes for verifier findings; never invents new facts."""
    fixed = deepcopy(output)
    fixed["evidence_refs"] = [
        ref for ref in dict.fromkeys(fixed["evidence_refs"]) if ref in consumed
    ]
    for claim in fixed.get("claim_assessments", []):
        claim["evidence_refs"] = [ref for ref in claim["evidence_refs"] if ref in consumed]
    fixed["resolution_actions"] = list(dict.fromkeys(fixed["resolution_actions"]))
    payment = fixed["payment_analysis"]
    financial = fixed["financial_resolution"]
    refundable = to_decimal(payment.get("refundable_total_brl"))
    recommended = to_decimal(financial["recommended_refund_brl"]) or Decimal(0)
    if refundable is not None and recommended > refundable + TOLERANCE:
        recommended = refundable
    if fixed["assessment"]["case_status"] == "no_action" or payment["verdict"] == "refunded":
        recommended = Decimal(0)
    financial["recommended_refund_brl"] = to_brl(recommended) or 0.0
    lines = financial["refund_lines"][:1]
    if recommended > 0 and lines:
        lines[0]["amount_brl"] = to_brl(recommended)
    financial["refund_lines"] = lines if recommended > 0 else []
    sellers = set(fixed["affected_entities"]["seller_ids"])
    shipment = fixed["shipment_analysis"]
    shipment["late_seller_ids"] = [s for s in shipment["late_seller_ids"] if s in sellers]
    if shipment["verdict"] != "seller_delay":
        shipment["late_seller_ids"] = []
    for party in fixed["root_cause_analysis"]["responsible_parties"]:
        if party["party_type"] == "seller" and party["party_id"] not in sellers:
            party["party_id"] = None
    if any(error.endswith("not_reproducible") for error in errors):
        fixed["assessment"]["case_status"] = "needs_investigation"
        fixed["assessment"]["confidence"] = min(fixed["assessment"]["confidence"], 0.5)
    return fixed


async def solve_case(case: dict[str, Any], gateway: Gateway, trace: TraceSink) -> dict[str, Any]:
    """Entry point used by ``day09 run``: coordinate all agents for one case."""
    return await Coordinator(case, gateway, trace).run()
