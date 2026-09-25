"""Policy agent: classify the primary issue from specialist facts and apply the policy rule.

Classification only uses verified facts (order status, payment/refund verdicts, shipment
verdict). Claim topics are hypotheses: agreement raises confidence, disagreement lowers it,
but a claim never overrides evidence. The machine-readable policy supplies case status, action,
refund ceiling and responsible party type; party IDs are always bound to case evidence, never
copied from the policy document.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from ..agent_types import POLICY_AGENT, AgentResult, AgentTask, Status
from ..evidence_cache import CaseEvidenceStore
from ..money import to_brl, to_decimal

FULL_REFUND_ACTIONS = {"issue_refund", "retry_refund"}
PARTIAL_REFUND_ACTIONS = {"refund_freight", "refund_duplicate_charge", "reconcile_payment"}


def classify_issue(facts: dict[str, Any]) -> tuple[str, list[str]]:
    """Return the primary issue and the matched evidence signals."""
    if facts.get("entity_status") != "resolved":
        return "insufficient_evidence", ["entity_unresolved"]
    order_status = facts.get("order_status")
    payment = facts.get("payment_verdict")
    shipment = facts.get("shipment_verdict")
    captured = to_decimal(facts.get("captured")) or Decimal(0)
    if order_status == "canceled" and captured > 0:
        return "canceled_order_paid", ["order_status_canceled", "capture_confirmed"]
    if order_status == "unavailable" and captured > 0:
        return "unavailable_order_paid", ["order_status_unavailable", "capture_confirmed"]
    if payment == "refund_failed":
        return "refund_failed", ["refund_event_failed"]
    if payment == "refund_pending":
        return "refund_pending", ["refund_event_pending"]
    if payment == "capture_mismatch":
        return "payment_mismatch", ["reconciliation_mismatch_open"]
    if payment == "duplicate_capture":
        return "duplicate_charge", ["repeated_capture_exceeds_order_value"]
    if payment == "reconciled" and facts.get("split"):
        return "valid_split_payment", ["captures_sum_to_order_value"]
    if shipment == "seller_delay":
        return "late_delivery_seller", ["seller_handoff_after_limit", "delivered_after_estimate"]
    if shipment == "logistics_delay":
        return "late_delivery_logistics", ["seller_on_time", "delivered_after_estimate"]
    if payment in {"reconciled", "refunded"} and shipment in {"on_time", "lost", "returned"}:
        return "unsupported_claim", ["no_issue_in_scoped_evidence"]
    if (
        payment == "reconciled"
        and shipment == "insufficient_evidence"
        and order_status == "delivered"
    ):
        return "unsupported_claim", ["no_issue_in_scoped_evidence"]
    return "insufficient_evidence", ["evidence_incomplete"]


def evidence_refund(issue: str, facts: dict[str, Any]) -> Decimal | None:
    refundable = to_decimal(facts.get("refundable"))
    freight = to_decimal(facts.get("freight_total"))
    by_issue = {
        "canceled_order_paid": refundable,
        "unavailable_order_paid": refundable,
        "refund_failed": to_decimal(facts.get("failed_refund_amount")),
        "payment_mismatch": to_decimal(facts.get("mismatch_amount")),
        "duplicate_charge": to_decimal(facts.get("duplicate_amount")),
        "late_delivery_seller": freight,
        "late_delivery_logistics": freight,
    }
    return by_issue.get(issue)


def decide_refund(issue: str, rule: dict[str, Any], facts: dict[str, Any]) -> Decimal:
    ceiling = to_decimal(rule.get("refund_brl")) or Decimal(0)
    if ceiling <= 0:
        return Decimal(0)
    evidence_amount = evidence_refund(issue, facts)
    amount = ceiling if not evidence_amount else min(ceiling, evidence_amount)
    refundable = to_decimal(facts.get("refundable"))
    if refundable is not None:
        amount = min(amount, refundable)
    return max(amount, Decimal(0))


def responsible_parties(rule: dict[str, Any], issue: str, facts: dict[str, Any]) -> list[dict]:
    parties: list[dict[str, Any]] = []
    for party in rule.get("responsible_parties") or [{"party_type": "unknown"}]:
        party_type = party.get("party_type") or "unknown"
        if party_type == "seller":
            sellers = facts.get("late_seller_ids") if issue == "late_delivery_seller" else None
            sellers = sellers or facts.get("seller_ids") or []
            parties.extend({"party_type": "seller", "party_id": seller} for seller in sellers)
            if not sellers:
                parties.append({"party_type": "seller", "party_id": None})
        else:
            parties.append({"party_type": party_type, "party_id": None})
    unique = {(item["party_type"], item["party_id"]): item for item in parties}
    return list(unique.values())[:5]


def claim_verdicts(
    claims: list[dict[str, Any]], issue: str, action: str, refund: Decimal
) -> list[dict[str, Any]]:
    verdicts = []
    for claim in claims[:5]:
        topic = claim.get("topic")
        if topic == "requested_full_refund":
            if action in FULL_REFUND_ACTIONS and refund > 0:
                verdict = "supported"
            elif action in PARTIAL_REFUND_ACTIONS and refund > 0:
                verdict = "partially_supported"
            elif issue == "refund_pending":
                verdict = "insufficient_evidence"
            else:
                verdict = "unsupported"
            kind = "refund"
        elif issue == "insufficient_evidence":
            verdict, kind = "insufficient_evidence", "issue"
        elif topic == issue:
            verdict = "unsupported" if issue == "unsupported_claim" else "supported"
            kind = "issue"
        else:
            verdict, kind = "unsupported", "issue"
        verdicts.append({"claim_id": claim.get("claim_id"), "verdict": verdict, "kind": kind})
    return verdicts


async def run_policy_agent(task: AgentTask, store: CaseEvidenceStore) -> AgentResult:
    facts: dict[str, Any] = task.payload["facts"]
    topics = [
        topic for topic in task.payload.get("claim_topics", []) if topic != "requested_full_refund"
    ]
    issue, signals = classify_issue(facts)
    warnings: list[str] = []
    refs: list[str] = []

    rule: dict[str, Any] = {}
    if issue != "insufficient_evidence":
        policy = await store.fetch(
            POLICY_AGENT, "get_policy", policy_version=task.payload["policy_version"]
        )
        rules = policy.data.get("rules", {}) if policy and isinstance(policy.data, dict) else {}
        rule = rules.get(issue) or {}
        if policy and rules:
            refs.append(store.consume(POLICY_AGENT, policy))
        if not rule:
            warnings.append("policy_rule_missing")

    claim_agrees = issue in topics
    if rule:
        case_status = rule.get("case_status", "needs_investigation")
        action = rule.get("recommended_action") or "escalate_investigation"
        refund = decide_refund(issue, rule, facts)
    else:
        case_status, action, refund = "needs_investigation", "escalate_investigation", Decimal(0)
    if case_status == "no_action":
        refund = Decimal(0)

    base = float(task.payload.get("evidence_confidence", 0.9))
    if issue == "insufficient_evidence":
        confidence = 0.3
    elif claim_agrees:
        confidence = min(base, 0.93)
    elif topics:
        confidence = min(base, 0.55)
        warnings.append("claim_topic_not_supported_by_evidence")
    else:
        confidence = min(base, 0.75)

    parties = (
        responsible_parties(rule, issue, facts)
        if rule
        else [{"party_type": "unknown", "party_id": None}]
    )
    refund_lines = []
    if refund > 0:
        refund_lines.append(
            {
                "reason_code": action.upper(),
                "amount_brl": to_brl(refund),
                "entity_id": facts.get("order_id"),
            }
        )
    decision_code = f"POLICY_{action.upper()}"[:80]
    store.trace.emit(
        case_id=task.case_id,
        event_type="policy_decided",
        actor=POLICY_AGENT,
        target="coordinator",
        decision_code=decision_code,
        evidence_refs=[ref for ref in refs if ref] or None,
        attributes={
            "primary_issue": issue,
            "case_status": case_status,
            "recommended_refund_brl": to_brl(refund),
        },
    )
    return AgentResult.reply(
        task,
        status=Status.OK if rule else Status.INSUFFICIENT_EVIDENCE,
        confidence=confidence,
        decision_code=decision_code,
        evidence_refs=[ref for ref in refs if ref],
        warnings=warnings,
        data={
            "primary_issue": issue,
            "signals": signals,
            "case_status": case_status,
            "recommended_action": action,
            "recommended_refund": refund,
            "refund_lines": refund_lines,
            "responsible_parties": parties,
            "ranked_causes": [{"cause_code": issue.upper(), "rank": 1}],
            "claim_agrees": claim_agrees,
            "claim_verdicts": claim_verdicts(task.payload.get("claims", []), issue, action, refund),
        },
    )
