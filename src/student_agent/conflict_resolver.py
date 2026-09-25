from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from .state import (
    DataConflict,
    FinancialResolution,
    PaymentResult,
    RankedCause,
    RefundLine,
    ResponsibleParty,
    ShipmentResult,
)

PRIMARY_ISSUES = frozenset(
    {
        "canceled_order_paid",
        "unavailable_order_paid",
        "late_delivery_seller",
        "late_delivery_logistics",
        "valid_split_payment",
        "payment_mismatch",
        "duplicate_charge",
        "refund_pending",
        "refund_failed",
        "unsupported_claim",
        "insufficient_evidence",
    }
)
_LATE_TOPICS = frozenset({"late_delivery_seller", "late_delivery_logistics"})
_POLICY_TOPICS = frozenset(
    {
        "canceled_order_paid",
        "unavailable_order_paid",
        "valid_split_payment",
        "payment_mismatch",
        "duplicate_charge",
        "refund_pending",
        "refund_failed",
        "requested_full_refund",
    }
)


def dedupe(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if isinstance(value, str) and value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def claim_topics(case: Mapping[str, Any]) -> list[str]:
    claims = case.get("customer_request", {}).get("claims", [])
    if not isinstance(claims, list):
        return []
    return dedupe(
        claim.get("topic")
        for claim in claims
        if isinstance(claim, Mapping) and isinstance(claim.get("topic"), str)
    )


def policy_is_needed(topics: Iterable[str]) -> bool:
    return bool(set(topics) & (_LATE_TOPICS | _POLICY_TOPICS))


def _source_matches(source: str, aliases: Iterable[str]) -> bool:
    normalized = source.lower().replace("-", "_").replace(" ", "_")
    return any(alias in normalized for alias in aliases)


def choose_authoritative_source(field: str, sources: Iterable[str]) -> tuple[str | None, str]:
    clean_sources = dedupe(sources)
    normalized_field = field.lower().replace("-", "_").replace(" ", "_")
    if any(token in normalized_field for token in ("delivery", "shipment", "transit")):
        precedence = (
            (
                ("shipment_event", "shipment", "logistics", "tracking"),
                "AUTHORITATIVE_SHIPMENT_EVENT",
            ),
            (("order",), "ORDER_STATUS_SECONDARY"),
            (("customer", "claim"), "CUSTOMER_CLAIM_SECONDARY"),
        )
    elif any(token in normalized_field for token in ("payment", "capture", "refund", "charge")):
        precedence = (
            (
                ("refund_ledger", "refund", "payment_ledger", "ledger", "payment"),
                "PAYMENT_LEDGER_AUTHORITATIVE",
            ),
            (("order",), "ORDER_STATUS_SECONDARY"),
            (("customer", "claim"), "CUSTOMER_CLAIM_SECONDARY"),
        )
    elif any(token in normalized_field for token in ("policy", "eligib", "action")):
        precedence = (
            (("policy_evidence", "policy", "rule"), "POLICY_EVIDENCE_AUTHORITATIVE"),
            (("order", "shipment", "payment"), "DOMAIN_EVIDENCE_SECONDARY"),
        )
    elif any(token in normalized_field for token in ("customer", "identity", "order_id")):
        precedence = (
            (("entity", "customer"), "ENTITY_EVIDENCE_AUTHORITATIVE"),
            (("order",), "ORDER_EVIDENCE_SECONDARY"),
            (("claim", "free_text"), "FREE_TEXT_HINT_SECONDARY"),
        )
    else:
        precedence = ()
    for aliases, code in precedence:
        for source in clean_sources:
            if _source_matches(source, aliases):
                return source, code
    return None, "UNRESOLVED_INSUFFICIENT_EVIDENCE"


def resolve_conflict_candidate(candidate: Mapping[str, Any]) -> DataConflict | None:
    field = candidate.get("field")
    sources = candidate.get("sources")
    if not isinstance(field, str) or not field or not isinstance(sources, list):
        return None
    clean_sources = dedupe(source for source in sources if isinstance(source, str))
    if len(clean_sources) < 2:
        return None
    selected_source, resolution_code = choose_authoritative_source(field, clean_sources)
    return DataConflict(
        field=field,
        sources=clean_sources[:5],
        selected_source=selected_source,
        resolution_code=resolution_code,
    )


def derive_conflicts(
    case: Mapping[str, Any],
    shipment: ShipmentResult,
    payment: PaymentResult,
) -> list[DataConflict]:
    candidates = case.get("conflict_candidates", [])
    conflicts: list[DataConflict] = []
    if isinstance(candidates, list):
        for candidate in candidates:
            if isinstance(candidate, Mapping):
                conflict = resolve_conflict_candidate(candidate)
                if conflict is not None:
                    conflicts.append(conflict)
    topics = set(claim_topics(case))
    if topics & _LATE_TOPICS and shipment.verdict == "on_time":
        conflicts.append(
            DataConflict(
                field="delivery_status",
                sources=["customer_claim", "shipment_event"],
                selected_source="shipment_event",
                resolution_code="AUTHORITATIVE_SHIPMENT_EVENT",
            )
        )
    if topics & {"payment_mismatch", "duplicate_charge"} and payment.verdict in {
        "reconciled",
        "refunded",
    }:
        conflicts.append(
            DataConflict(
                field="payment_status",
                sources=["customer_claim", "payment_ledger"],
                selected_source="payment_ledger",
                resolution_code="PAYMENT_LEDGER_AUTHORITATIVE",
            )
        )
    unique: list[DataConflict] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    for conflict in conflicts:
        key = (conflict.field, tuple(conflict.sources))
        if key not in seen:
            seen.add(key)
            unique.append(conflict)
    return unique[:5]


def derive_root_cause(
    shipment: ShipmentResult,
    payment: PaymentResult,
    topics: Iterable[str],
    *,
    unsupported_claim: bool = False,
) -> tuple[list[RankedCause], list[ResponsibleParty]]:
    topic_set = set(topics)
    if shipment.verdict == "seller_delay":
        seller_id = shipment.late_seller_ids[0] if shipment.late_seller_ids else None
        return [RankedCause("SELLER_LATE_HANDOFF", 1)], [ResponsibleParty("seller", seller_id)]
    if shipment.verdict == "logistics_delay":
        return [RankedCause("LOGISTICS_TRANSIT_DELAY", 1)], [
            ResponsibleParty("logistics_provider", None)
        ]
    if shipment.verdict == "lost":
        return [RankedCause("SHIPMENT_LOST", 1)], [ResponsibleParty("logistics_provider", None)]
    if shipment.verdict == "returned":
        return [RankedCause("SHIPMENT_RETURNED", 1)], [ResponsibleParty("unknown", None)]
    if payment.verdict == "capture_mismatch":
        return [RankedCause("PAYMENT_CAPTURE_MISMATCH", 1)], [
            ResponsibleParty("payment_provider", None)
        ]
    if payment.verdict == "duplicate_capture":
        return [RankedCause("DUPLICATE_PAYMENT_CAPTURE", 1)], [
            ResponsibleParty("payment_provider", None)
        ]
    if payment.verdict == "refund_pending":
        return [RankedCause("REFUND_PROCESSING_DELAY", 1)], [
            ResponsibleParty("payment_provider", None)
        ]
    if payment.verdict == "refund_failed":
        return [RankedCause("REFUND_PROCESSING_FAILURE", 1)], [
            ResponsibleParty("payment_provider", None)
        ]
    if unsupported_claim:
        return [RankedCause("CLAIM_NOT_SUPPORTED", 1)], [ResponsibleParty("unknown", None)]
    if "valid_split_payment" in topic_set and payment.verdict == "reconciled":
        return [], []
    return [RankedCause("INSUFFICIENT_EVIDENCE", 1)], [ResponsibleParty("unknown", None)]


def primary_issue_for(
    shipment: ShipmentResult,
    payment: PaymentResult,
    topics: Iterable[str],
    *,
    unsupported_claim: bool = False,
) -> str:
    if shipment.verdict == "seller_delay":
        return "late_delivery_seller"
    if shipment.verdict in {"logistics_delay", "lost", "returned"}:
        return "late_delivery_logistics"
    if payment.verdict == "capture_mismatch":
        return "payment_mismatch"
    if payment.verdict == "duplicate_capture":
        return "duplicate_charge"
    if payment.verdict in {"refund_pending", "refund_failed"}:
        return payment.verdict
    if payment.verdict == "reconciled" and "valid_split_payment" in topics:
        return "valid_split_payment"
    if unsupported_claim:
        return "unsupported_claim"
    for topic in topics:
        if topic in PRIMARY_ISSUES and topic != "requested_full_refund":
            return topic
    return "insufficient_evidence"


def policy_value(data: Any, keys: set[str]) -> Any:
    if isinstance(data, Mapping):
        for key, value in data.items():
            normalized = str(key).lower().replace("-", "_")
            if normalized in keys:
                return value
        for value in data.values():
            found = policy_value(value, keys)
            if found is not None:
                return found
    elif isinstance(data, list):
        for value in data:
            found = policy_value(value, keys)
            if found is not None:
                return found
    return None


def refund_allowed(policy_data: Any) -> bool | None:
    allowed = policy_value(
        policy_data,
        {"refund_eligible", "refund_allowed", "full_refund_allowed", "eligible_for_refund"},
    )
    if isinstance(allowed, bool):
        return allowed
    allowed_actions = policy_value(policy_data, {"allowed_actions"})
    if isinstance(allowed_actions, list):
        return any("refund" in str(action).lower() for action in allowed_actions)
    forbidden_actions = policy_value(policy_data, {"forbidden_actions"})
    if isinstance(forbidden_actions, list) and any(
        "refund" in str(action).lower() for action in forbidden_actions
    ):
        return False
    return None


def build_financial_resolution(
    payment: PaymentResult,
    *,
    allow_refund: bool,
) -> FinancialResolution:
    if not allow_refund or payment.verdict == "refunded":
        return FinancialResolution()
    amount = payment.refundable_total_brl
    if amount is None or amount <= 0:
        return FinancialResolution()
    amount = round(amount, 2)
    return FinancialResolution(
        recommended_refund_brl=amount,
        refund_lines=[RefundLine(reason_code="POLICY_REFUND", amount_brl=amount)],
    )