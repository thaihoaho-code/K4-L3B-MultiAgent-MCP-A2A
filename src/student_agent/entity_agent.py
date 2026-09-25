from __future__ import annotations

import unicodedata
from difflib import SequenceMatcher
from typing import Any

from .evidence_utils import (
    collect_identifiers,
    evidence_data,
    evidence_ref,
    first_value,
    is_negative_result,
    unique_strings,
)
from .mcp_gateway import EvidenceGateway
from .state import EntityResult
from .trace import TraceWriter

# ---------------------------------------------------------------------------
# Text normalisation helpers
# ---------------------------------------------------------------------------

def _normalize_text(text: str) -> str:
    """Lowercase, strip whitespace, normalise Unicode for comparison."""
    text = unicodedata.normalize("NFKC", text).strip().lower()
    # Collapse multiple spaces
    return " ".join(text.split())


def _similarity(a: str, b: str) -> float:
    """Compute similarity ratio between two normalised strings."""
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, _normalize_text(a), _normalize_text(b)).ratio()


# ---------------------------------------------------------------------------
# Candidate scoring
# ---------------------------------------------------------------------------

def _score_candidate(
    candidate_id: str,
    order_data: dict[str, Any] | None,
    case: dict[str, Any],
) -> dict[str, Any]:
    """Score a candidate order against the case's hints.

    Returns a dict with ``score``, ``matched_signals`` and ``details``.
    Higher score = better match.
    """
    if order_data is None:
        return {"candidate_id": candidate_id, "score": 0.0, "matched_signals": [], "valid": False}

    score = 0.0
    matched_signals: list[str] = []
    customer_request = case.get("customer_request", {})
    claimed_order_id = customer_request.get("claimed_order_id", "")
    customer_hint = case.get("customer_unique_id_hint", "")

    # 1. Exact order ID match with claimed_order_id (strongest signal)
    if candidate_id == claimed_order_id:
        score += 0.40
        matched_signals.append("exact_order_hint")

    # 2. Order data actually exists on MCP (critical signal)
    score += 0.25
    matched_signals.append("order_exists")

    # 3. Customer ID match
    order_customer_id = order_data.get("customer_unique_id", "")
    if customer_hint and order_customer_id:
        sim = _similarity(customer_hint, order_customer_id)
        if sim >= 0.85:
            score += 0.20
            matched_signals.append("customer_match")
        elif sim >= 0.50:
            score += 0.10
            matched_signals.append("customer_partial_match")

    # 4. Date proximity
    case_opened = case.get("opened_at", "")
    order_date = order_data.get("order_purchase_timestamp", "")
    if case_opened and order_date and order_date[:10] <= case_opened[:10]:
        # Both dates exist — basic validation that order predates the case
        score += 0.10
        matched_signals.append("date_plausible")

    # 5. Claim topic alignment with order status
    order_status = order_data.get("order_status", "")
    claims = customer_request.get("claims", [])
    claim_topics = {c.get("topic", "") for c in claims}

    if order_status == "delivered" and (
        "late_delivery_logistics" in claim_topics or "late_delivery_seller" in claim_topics
    ) or order_status == "canceled" and "canceled_order_paid" in claim_topics:
        score += 0.05
        matched_signals.append("status_claim_aligned")

    return {
        "candidate_id": candidate_id,
        "score": round(min(score, 1.0), 4),
        "matched_signals": matched_signals,
        "valid": True,
    }


# ---------------------------------------------------------------------------
# Resolution policy
# ---------------------------------------------------------------------------

_HIGH_THRESHOLD = 0.60
_GAP_THRESHOLD = 0.15


def _resolve_candidates(
    scored: list[dict[str, Any]],
) -> tuple[str, str | None, list[str]]:
    """Apply resolution policy to scored candidates.

    Returns ``(status, resolved_order_id, rejected_ids)``.
    """
    valid = [s for s in scored if s["valid"]]
    if not valid:
        return "not_found", None, [s["candidate_id"] for s in scored]

    valid.sort(key=lambda s: s["score"], reverse=True)
    top = valid[0]
    rejected = [s["candidate_id"] for s in scored if s["candidate_id"] != top["candidate_id"]]

    if top["score"] >= _HIGH_THRESHOLD:
        if len(valid) < 2 or (top["score"] - valid[1]["score"]) >= _GAP_THRESHOLD:
            return "resolved", top["candidate_id"], rejected
        # Two candidates too close
        return "ambiguous", top["candidate_id"], [
            s["candidate_id"] for s in valid[1:]
        ]

    # Score too low
    if top["score"] > 0.0:
        return "ambiguous", top["candidate_id"], rejected

    return "not_found", None, [s["candidate_id"] for s in scored]


# ---------------------------------------------------------------------------
# EntityAgent
# ---------------------------------------------------------------------------

class EntityAgent:
    """Người 2 – Nguyễn Tiến Phát: Entity Resolution và Customer Context."""

    async def resolve(
        self, case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
    ) -> EntityResult:
        """Resolve the correct order and customer from candidate_order_ids.

        Steps:
        1. Emit task_assigned trace.
        2. Verify each candidate via ``get_order`` MCP tool.
        3. Score and rank candidates.
        4. Resolve or mark ambiguous/not_found.
        5. Fetch customer history if resolved.
        6. Return EntityResult with evidence.
        """
        case_id = case["case_id"]
        candidate_ids: list[str] = case.get("candidate_order_ids", [])
        customer_hint: str = case.get("customer_unique_id_hint", "")
        evidence_refs: list[str] = []

        # ------------------------------------------------------------------
        # Step 1  – Emit task_assigned
        # ------------------------------------------------------------------
        trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor="entity-agent",
            target="coordinator",
            decision_code="RESOLVE_ENTITY",
        )

        # ------------------------------------------------------------------
        # Step 2  – Verify each candidate via MCP get_order
        # ------------------------------------------------------------------
        candidate_data: dict[str, dict[str, Any] | None] = {}
        for cid in candidate_ids:
            try:
                evidence = await gateway.call(
                    "get_order",
                    case_id=case_id,
                    order_id=cid,
                )
                candidate_data[cid] = evidence.get("data", {})
                ev_ref = evidence.get("evidence_ref")
                if ev_ref:
                    evidence_refs.append(ev_ref)
                    trace.emit(
                        case_id=case_id,
                        event_type="tool_result_consumed",
                        actor="entity-agent",
                        tool_name="get_order",
                        evidence_refs=[ev_ref],
                    )
            except Exception:
                # Candidate does not exist or MCP error – mark as invalid
                candidate_data[cid] = None

        # ------------------------------------------------------------------
        # Step 3  – Score candidates
        # ------------------------------------------------------------------
        scored = [
            _score_candidate(cid, candidate_data[cid], case)
            for cid in candidate_ids
        ]

        # ------------------------------------------------------------------
        # Step 4  – Apply resolution policy
        # ------------------------------------------------------------------
        resolution_status, resolved_order_id, rejected_ids = _resolve_candidates(scored)

        # Compute confidence from top score
        valid_scores = [s["score"] for s in scored if s["valid"]]
        if resolution_status == "resolved" and valid_scores:
            confidence = round(min(max(valid_scores[0] if valid_scores else 0.0, 0.0), 1.0), 4)
            # Boost confidence slightly if gap is large
            if len(valid_scores) >= 2:
                gap = valid_scores[0] - sorted(valid_scores, reverse=True)[1]
                confidence = round(min(confidence + gap * 0.1, 1.0), 4)
        elif resolution_status == "ambiguous":
            confidence = round(min(max(valid_scores[0] if valid_scores else 0.0, 0.0), 0.65), 4)
        else:
            confidence = 0.0

        # ------------------------------------------------------------------
        # Step 5  – Fetch customer history (if resolved or hint available)
        # ------------------------------------------------------------------
        customer_unique_id: str | None = None
        related_order_ids: list[str] = []

        lookup_customer_id = customer_hint
        if resolved_order_id and candidate_data.get(resolved_order_id):
            order_cust = candidate_data[resolved_order_id].get("customer_unique_id", "")
            if order_cust:
                lookup_customer_id = order_cust

        if lookup_customer_id and case.get("investigation_scope", {}).get(
            "include_customer_history", False
        ):
            try:
                cust_evidence = await gateway.call(
                    "get_customer_history",
                    case_id=case_id,
                    customer_unique_id=lookup_customer_id,
                )
                cust_data = cust_evidence.get("data", {})
                customer_unique_id = cust_data.get("customer_unique_id", lookup_customer_id)
                related_order_ids = cust_data.get("order_ids", [])
                ev_ref = cust_evidence.get("evidence_ref")
                if ev_ref:
                    evidence_refs.append(ev_ref)
                    trace.emit(
                        case_id=case_id,
                        event_type="tool_result_consumed",
                        actor="entity-agent",
                        tool_name="get_customer_history",
                        evidence_refs=[ev_ref],
                    )
            except Exception:
                # If customer lookup fails, use hint as fallback
                customer_unique_id = lookup_customer_id
        elif lookup_customer_id:
            customer_unique_id = lookup_customer_id

        # ------------------------------------------------------------------
        # Step 6  – Emit handoff back to coordinator
        # ------------------------------------------------------------------
        trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor="entity-agent",
            target="coordinator",
            decision_code=(
                "ENTITY_RESOLVED" if resolution_status == "resolved"
                else "ENTITY_AMBIGUOUS" if resolution_status == "ambiguous"
                else "ENTITY_NOT_FOUND"
            ),
            evidence_refs=evidence_refs or None,
            attributes={
                "candidate_count": len(candidate_ids),
                "resolved_count": 1 if resolved_order_id else 0,
                "confidence": confidence,
            },
        )

        # ------------------------------------------------------------------
        # Build result
        # ------------------------------------------------------------------
        # Dedupe evidence refs
        seen: set[str] = set()
        unique_refs: list[str] = []
        for ref in evidence_refs:
            if ref not in seen:
                seen.add(ref)
                unique_refs.append(ref)

        return EntityResult(
            customer_unique_id=customer_unique_id,
            resolved_order_id=resolved_order_id,
            rejected_order_ids=rejected_ids,
            related_order_ids=related_order_ids,
            resolution_status=resolution_status,
            confidence=confidence,
            evidence_refs=unique_refs,
        )
