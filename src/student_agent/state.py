from __future__ import annotations

from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# EntityAgent → OrderShipmentAgent, PaymentAgent, PolicyAgent
# ---------------------------------------------------------------------------

@dataclass
class EntityResult:
    """Kết quả của EntityAgent sau khi resolve customer và order."""

    # None nếu không tìm được
    customer_unique_id: str | None
    # None nếu không resolve được order chính xác
    resolved_order_id: str | None
    # Các candidate bị loại (phải report trong output)
    rejected_order_ids: list[str] = field(default_factory=list)
    # Các order khác của cùng customer (để build customer_context)
    related_order_ids: list[str] = field(default_factory=list)
    # resolved | ambiguous | not_found
    resolution_status: str = "not_found"
    confidence: float = 0.0
    evidence_refs: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# OrderShipmentAgent → PolicyAgent, Verifier
# ---------------------------------------------------------------------------

@dataclass
class ShipmentResult:
    """Kết quả của OrderShipmentAgent sau khi tra cứu đơn hàng và vận chuyển."""

    # on_time | seller_delay | logistics_delay | lost | returned | conflicting |
    # insufficient_evidence
    verdict: str = "insufficient_evidence"
    # Danh sách seller_id bị delay (nếu có)
    late_seller_ids: list[str] = field(default_factory=list)
    # Danh sách order_id, item_id, seller_id, shipment_id đã xác định
    order_ids: list[str] = field(default_factory=list)
    item_ids: list[str] = field(default_factory=list)
    seller_ids: list[str] = field(default_factory=list)
    shipment_ids: list[str] = field(default_factory=list)
    # Timeline có đầy đủ không (ảnh hưởng confidence)
    timeline_complete: bool = False
    evidence_refs: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# PaymentAgent → PolicyAgent, Verifier
# ---------------------------------------------------------------------------

@dataclass
class PaymentResult:
    """Kết quả của PaymentAgent sau khi kiểm tra giao dịch và hoàn tiền."""

    # reconciled | capture_mismatch | duplicate_capture |
    # refund_pending | refund_failed | refunded | insufficient_evidence
    verdict: str = "insufficient_evidence"
    # Tổng tiền đã thu (BRL), None nếu không xác định được
    captured_total_brl: float | None = None
    # Tổng tiền đã hoàn, None nếu chưa có refund
    refunded_total_brl: float | None = None
    # Tổng tiền có thể hoàn theo policy
    refundable_total_brl: float | None = None
    # Danh sách payment reference IDs
    payment_references: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# PolicyAgent → Verifier
# ---------------------------------------------------------------------------

@dataclass
class RankedCause:
    """Một nguyên nhân gốc rễ được xếp hạng."""
    cause_code: str   # Ví dụ: LOGISTICS_DELAY, SELLER_BREACH
    rank: int         # 1 = quan trọng nhất, tối đa 5


@dataclass
class ResponsibleParty:
    """Bên chịu trách nhiệm cho vụ việc."""
    # seller | platform | logistics_provider | payment_provider | customer | unknown
    party_type: str
    party_id: str | None = None


@dataclass
class DataConflict:
    """Một xung đột dữ liệu được phát hiện giữa các nguồn."""
    field: str                    # Tên field bị mâu thuẫn
    sources: list[str]            # Tên các nguồn (ít nhất 2)
    selected_source: str | None   # Nguồn được chọn làm chuẩn
    resolution_code: str          # Lý do chọn nguồn đó


@dataclass
class RefundLine:
    """Một dòng hoàn tiền trong financial resolution."""
    reason_code: str
    amount_brl: float
    entity_id: str | None = None


@dataclass
class FinancialResolution:
    """Kết quả tài chính được đề xuất."""
    currency: str = "BRL"
    recommended_refund_brl: float = 0.0
    refund_lines: list[RefundLine] = field(default_factory=list)


@dataclass
class PolicyResult:
    """Kết quả của PolicyAgent sau khi phân xử dựa trên evidence và policy."""

    # canceled_order_paid | unavailable_order_paid | late_delivery_seller |
    # late_delivery_logistics | valid_split_payment | payment_mismatch |
    # duplicate_charge | refund_pending | refund_failed |
    # unsupported_claim | insufficient_evidence
    primary_issue: str = "insufficient_evidence"
    secondary_issues: list[str] = field(default_factory=list)
    # action_required | no_action | needs_investigation
    case_status: str = "needs_investigation"
    confidence: float = 0.0

    ranked_causes: list[RankedCause] = field(default_factory=list)
    responsible_parties: list[ResponsibleParty] = field(default_factory=list)
    data_conflicts: list[DataConflict] = field(default_factory=list)
    financial_resolution: FinancialResolution = field(default_factory=FinancialResolution)
    resolution_actions: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# ClaimAssessment — dùng trong output cuối (optional nhưng tăng điểm)
# ---------------------------------------------------------------------------

@dataclass
class ClaimAssessment:
    """Đánh giá từng claim riêng lẻ trong case."""
    claim_id: str
    # supported | unsupported | partially_supported | insufficient_evidence
    verdict: str
    confidence: float
    evidence_refs: list[str] = field(default_factory=list)
