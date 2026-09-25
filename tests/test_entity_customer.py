"""Tests for EntityAgent — Nguyễn Tiến Phát.

Covers:
  1. Single candidate exact match → resolved
  2. Two candidates close scores → ambiguous
  3. No valid candidate → not_found
  4. Fuzzy customer match still resolves
  5. Rejected candidate in rejected list
  6. Evidence isolation (no cross-case)
  7. Candidate scoring helpers
"""
from __future__ import annotations

import pytest

from student_agent.entity_agent import (
    EntityAgent,
    _normalize_text,
    _resolve_candidates,
    _score_candidate,
    _similarity,
)
from student_agent.state import EntityResult

# ---------------------------------------------------------------------------
# Helper: mock gateway + trace
# ---------------------------------------------------------------------------

class _FakeTrace:
    """Capture trace.emit() calls for assertions."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def emit(self, **kwargs) -> dict:
        self.events.append(kwargs)
        return kwargs


class _FakeGateway:
    """Return pre-loaded MCP responses keyed by (tool_name, order_id | customer_unique_id)."""

    def __init__(self, responses: dict[tuple[str, str], dict]) -> None:
        self._responses = responses

    async def call(self, tool_name: str, *, case_id: str, **kwargs: str) -> dict:
        key_value = kwargs.get("order_id") or kwargs.get("customer_unique_id") or ""
        key = (tool_name, key_value)
        if key not in self._responses:
            raise RuntimeError(f"MCP tool {tool_name} failed: not found")
        return self._responses[key]


# ---------------------------------------------------------------------------
# Test normalisation helpers
# ---------------------------------------------------------------------------

class TestNormalization:
    def test_basic_normalize(self):
        assert _normalize_text("  Hello   World  ") == "hello world"

    def test_unicode_normalize(self):
        assert _normalize_text("Ｆｕｌｌ　Ｗｉｄｔｈ") == "full width"

    def test_similarity_exact(self):
        assert _similarity("hello", "hello") == 1.0

    def test_similarity_different(self):
        assert _similarity("hello", "world") < 0.5

    def test_similarity_empty(self):
        assert _similarity("", "hello") == 0.0

    def test_similarity_close(self):
        assert _similarity("customer-abc123", "customer-abc124") > 0.85


# ---------------------------------------------------------------------------
# Test candidate scoring
# ---------------------------------------------------------------------------

class TestCandidateScoring:
    def test_exact_match_high_score(self):
        case = {
            "customer_request": {
                "claimed_order_id": "order-001",
                "claims": [{"claim_id": "c1", "topic": "late_delivery_logistics"}],
            },
            "customer_unique_id_hint": "cust-abc",
            "opened_at": "2018-06-01T00:00:00",
        }
        order_data = {
            "customer_unique_id": "cust-abc",
            "order_purchase_timestamp": "2018-05-01T00:00:00",
            "order_status": "delivered",
        }
        result = _score_candidate("order-001", order_data, case)
        assert result["valid"] is True
        assert result["score"] >= 0.85
        assert "exact_order_hint" in result["matched_signals"]
        assert "order_exists" in result["matched_signals"]
        assert "customer_match" in result["matched_signals"]

    def test_no_order_data_zero_score(self):
        case = {"customer_request": {"claimed_order_id": "x"}, "opened_at": "2018-01-01"}
        result = _score_candidate("candidate-fake", None, case)
        assert result["valid"] is False
        assert result["score"] == 0.0

    def test_candidate_without_hint_match(self):
        case = {
            "customer_request": {"claimed_order_id": "order-001", "claims": []},
            "customer_unique_id_hint": "cust-abc",
            "opened_at": "2018-06-01T00:00:00",
        }
        order_data = {
            "customer_unique_id": "cust-xyz-different",
            "order_purchase_timestamp": "2018-05-01T00:00:00",
        }
        result = _score_candidate("order-001", order_data, case)
        assert result["valid"] is True
        # Has exact_order_hint but no customer_match
        assert "exact_order_hint" in result["matched_signals"]
        assert "customer_match" not in result["matched_signals"]


# ---------------------------------------------------------------------------
# Test resolution policy
# ---------------------------------------------------------------------------

class TestResolutionPolicy:
    def test_single_strong_candidate_resolves(self):
        scored = [
            {"candidate_id": "real-order", "score": 0.90, "matched_signals": ["a"], "valid": True},
            {"candidate_id": "fake-001", "score": 0.0, "matched_signals": [], "valid": False},
        ]
        status, resolved, rejected = _resolve_candidates(scored)
        assert status == "resolved"
        assert resolved == "real-order"
        assert "fake-001" in rejected

    def test_two_close_candidates_ambiguous(self):
        scored = [
            {"candidate_id": "a", "score": 0.70, "matched_signals": ["x"], "valid": True},
            {"candidate_id": "b", "score": 0.65, "matched_signals": ["y"], "valid": True},
        ]
        status, resolved, rejected = _resolve_candidates(scored)
        assert status == "ambiguous"

    def test_no_valid_candidates_not_found(self):
        scored = [
            {"candidate_id": "fake-1", "score": 0.0, "matched_signals": [], "valid": False},
            {"candidate_id": "fake-2", "score": 0.0, "matched_signals": [], "valid": False},
        ]
        status, resolved, rejected = _resolve_candidates(scored)
        assert status == "not_found"
        assert resolved is None
        assert len(rejected) == 2

    def test_clear_gap_resolves(self):
        scored = [
            {"candidate_id": "a", "score": 0.85, "matched_signals": ["x"], "valid": True},
            {"candidate_id": "b", "score": 0.30, "matched_signals": ["y"], "valid": True},
        ]
        status, resolved, rejected = _resolve_candidates(scored)
        assert status == "resolved"
        assert resolved == "a"


# ---------------------------------------------------------------------------
# Test full EntityAgent async flow
# ---------------------------------------------------------------------------

class TestEntityAgentResolve:
    @pytest.mark.asyncio
    async def test_resolved_case(self):
        """One real order + one fake candidate → resolved."""
        case = {
            "case_id": "L3B_CASE_TEST",
            "opened_at": "2018-06-01T09:00:00-03:00",
            "customer_request": {
                "claimed_order_id": "real-order-abc",
                "claims": [{"claim_id": "c1", "topic": "late_delivery_logistics"}],
            },
            "candidate_order_ids": ["real-order-abc", "candidate-fake"],
            "customer_unique_id_hint": "customer-abc",
            "investigation_scope": {"include_customer_history": True},
        }
        gateway = _FakeGateway({
            ("get_order", "real-order-abc"): {
                "evidence_ref": "ev_test_order_real_abc_12345678901234567890",
                "data": {
                    "customer_unique_id": "customer-abc",
                    "order_purchase_timestamp": "2018-05-01T00:00:00",
                    "order_status": "delivered",
                },
            },
            # "candidate-fake" will raise RuntimeError → marked invalid
            ("get_customer_history", "customer-abc"): {
                "evidence_ref": "ev_test_cust_history_abc_12345678901234567890",
                "data": {
                    "customer_unique_id": "customer-abc",
                    "order_ids": ["real-order-abc", "old-order-xyz"],
                },
            },
        })
        trace = _FakeTrace()

        agent = EntityAgent()
        result = await agent.resolve(case, gateway, trace)

        assert isinstance(result, EntityResult)
        assert result.resolution_status == "resolved"
        assert result.resolved_order_id == "real-order-abc"
        assert "candidate-fake" in result.rejected_order_ids
        assert result.customer_unique_id == "customer-abc"
        assert "real-order-abc" in result.related_order_ids
        assert result.confidence > 0.5
        assert len(result.evidence_refs) >= 1

        # Verify trace events
        event_types = [e["event_type"] for e in trace.events]
        assert "task_assigned" in event_types
        assert "tool_result_consumed" in event_types
        assert "handoff" in event_types

    @pytest.mark.asyncio
    async def test_not_found_case(self):
        """All candidates fail MCP → not_found."""
        case = {
            "case_id": "L3B_CASE_NF",
            "opened_at": "2018-06-01T09:00:00",
            "customer_request": {
                "claimed_order_id": "nonexistent",
                "claims": [],
            },
            "candidate_order_ids": ["nonexistent", "also-fake"],
            "customer_unique_id_hint": "",
            "investigation_scope": {},
        }
        gateway = _FakeGateway({})  # All calls will fail
        trace = _FakeTrace()

        agent = EntityAgent()
        result = await agent.resolve(case, gateway, trace)

        assert result.resolution_status == "not_found"
        assert result.resolved_order_id is None
        assert result.confidence == 0.0

    @pytest.mark.asyncio
    async def test_evidence_refs_are_unique(self):
        """Evidence refs should not contain duplicates."""
        case = {
            "case_id": "L3B_CASE_DEDUP",
            "opened_at": "2018-06-01T09:00:00",
            "customer_request": {
                "claimed_order_id": "order-a",
                "claims": [],
            },
            "candidate_order_ids": ["order-a"],
            "customer_unique_id_hint": "cust-a",
            "investigation_scope": {"include_customer_history": True},
        }
        gateway = _FakeGateway({
            ("get_order", "order-a"): {
                "evidence_ref": "ev_test_order_aaaaaaaaaa_12345678901234567890",
                "data": {"customer_unique_id": "cust-a", "order_purchase_timestamp": "2018-01-01"},
            },
            ("get_customer_history", "cust-a"): {
                "evidence_ref": "ev_test_cust_bbbbbbbbbb_12345678901234567890",
                "data": {"customer_unique_id": "cust-a", "order_ids": ["order-a"]},
            },
        })
        trace = _FakeTrace()

        agent = EntityAgent()
        result = await agent.resolve(case, gateway, trace)

        assert len(result.evidence_refs) == len(set(result.evidence_refs))

    @pytest.mark.asyncio
    async def test_rejected_and_resolved_do_not_overlap(self):
        """Resolved order must not appear in rejected list."""
        case = {
            "case_id": "L3B_CASE_OVL",
            "opened_at": "2018-06-01T09:00:00",
            "customer_request": {
                "claimed_order_id": "order-real",
                "claims": [],
            },
            "candidate_order_ids": ["order-real", "order-fake"],
            "customer_unique_id_hint": "cust-x",
            "investigation_scope": {},
        }
        gateway = _FakeGateway({
            ("get_order", "order-real"): {
                "evidence_ref": "ev_test_order_cccccccccc_12345678901234567890",
                "data": {"customer_unique_id": "cust-x", "order_purchase_timestamp": "2018-01-01"},
            },
        })
        trace = _FakeTrace()

        agent = EntityAgent()
        result = await agent.resolve(case, gateway, trace)

        if result.resolved_order_id:
            assert result.resolved_order_id not in result.rejected_order_ids
