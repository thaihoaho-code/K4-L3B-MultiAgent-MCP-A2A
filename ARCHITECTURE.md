# L3B Architecture Record

Tài liệu mô tả các quyết định có thể kiểm chứng của workflow multi-agent L3B. Không chứa prompt bí mật hay chain-of-thought; trace chỉ ghi sự kiện quan sát được, decision code, tool name, evidence ref và metric ngắn.

## 1. System overview

```text
                      case_received (CLI)
                              │
                         Coordinator ──────────────── A2ABus: task_assigned / handoff (validated)
                              │
          ┌───────────────────┼──────────────────────────────────────────────────┐
          ▼                   │                                                  │
   entity-agent  (history + get_order → candidate ranking + temporal scoping)    │
          │ handoff: resolved order, customer, scoped record + window            │
          ▼                                                                      │
   order-product-agent ─► shipment-agent ─► payment-refund-agent                 │
     (items, products)     (timeline)        (captures, refunds)                 │
          └──────────── facts ──────────────────┘                                │
                              ▼                                                  │
                        policy-agent  (classify issue + get_policy → decision) ──┤ policy_decided
                              │ seller blamed? ─► order-product-agent: verify_seller (get_sellers)
                              ▼                                                  │
                      conflict-resolver (field precedence, responsibility check) ┤ policy_decided
                              ▼                                                  │
                    Coordinator assembles draft output                           │
                              ▼                                                  │
                          verifier  (schema + invariants + independent re-derivation)
                              │ VERIFY_FAIL → 1 deterministic repair → re-verify (max 2 runs)
                              ▼
                      case_finalized (CLI)
```

Code layout:

| Module | Vai trò |
| --- | --- |
| `workflow.py` | Coordinator: routing, dependency gating, assembly, verify/repair loop, `solve_case` |
| `a2a.py` | `A2ABus`: task envelope, correlated handoff validation, loop guard |
| `agent_types.py` | Actor names, `TASK_ROUTES`, `TOOL_PERMISSIONS`, `AgentTask`/`AgentResult`, `Status` |
| `evidence_cache.py` | `CaseEvidenceStore`: case-scoped cache, bounded retry, least privilege, provenance |
| `scoping.py` | Chọn order record thuộc phạm vi case và cửa sổ thời gian evidence |
| `money.py` | Decimal arithmetic, tolerance 0.01 BRL |
| `agents/*.py` | Specialist agents (`async run_*(task, store) -> AgentResult`) |

## 2. Agent ownership

| Actor | Input (A2A payload) | Trách nhiệm | Tool permission | Output/handoff |
| --- | --- | --- | --- | --- |
| `coordinator` | case input, mọi `AgentResult` | Duy nhất giao task, gate dependency, assemble output, quyết định finalize/repair | không gọi MCP | output L3B |
| `entity-agent` | claimed order, candidates, customer hint, `opened_at` | Rank/reject candidate, verify order, customer context, chọn record trong phạm vi case | `get_customer_history`, `get_order` | `ENTITY_RESOLVED/AMBIGUOUS/NOT_FOUND` + scoped record/window + conflict candidates |
| `order-product-agent` | order id, window, scoped row | Item/seller/product IDs, order value, freight; follow-up `verify_seller` | `get_order_items`, `get_product_context`, `get_sellers` | `ORDER_ANALYZED`, `SELLER_VERIFIED/UNVERIFIED` |
| `shipment-agent` | order id, window, scoped row | Phân loại timeline: on_time / seller_delay / logistics_delay / lost / returned | `get_shipment_summary` | `SHIPMENT_ANALYZED` + conflict candidates |
| `payment-refund-agent` | order id, window, order value, claim topics | Captured/refunded/refundable, split vs duplicate, mismatch, refund lifecycle | `get_payment_timeline`, `get_order_payments` (fallback), `get_refund_timeline` (conditional) | `PAYMENT_ANALYZED` |
| `policy-agent` | facts từ các specialist, policy version, claims | Phân loại primary issue từ evidence, áp policy rule, refund, responsibility, claim verdicts | `get_policy` | `policy_decided` + `POLICY_<ACTION>` |
| `conflict-resolver` | conflict candidates, responsible parties, seller IDs | Chọn source theo precedence từng field; kiểm tra responsibility | không gọi MCP | `CONFLICT_RESOLVED/UNRESOLVED/NO_CONFLICT` |
| `verifier` | draft output, case, contracts | Schema, invariants, tái dựng độc lập scoped record và captured total từ raw evidence | không gọi MCP | `verification_completed` + `VERIFY_PASS/FAIL` |

Least privilege được enforce trong code (`TOOL_PERMISSIONS`); gọi tool ngoài quyền sẽ ném `ToolPermissionError`.

## 3. Entity resolution và A2A protocol

**Candidate ranking** (deterministic, `agents/entity_customer.py`):

| Signal | Weight |
| --- | ---: |
| `history_exact` (ID có trong customer history của hint) | 0.35 |
| `history_fuzzy` (SequenceMatcher ≥ 0.90 sau NFKC/casefold/strip punctuation) | 0.25 |
| `claimed_order` | 0.20 |
| `well_formed_id` (32 hex) | 0.10 |
| `order_verified` (`get_order` trả record) | 0.30 |

- `resolved` khi top score ≥ 0.60 **và** gap với candidate thứ hai ≥ 0.25; `ambiguous` khi top ≥ 0.40; còn lại `not_found`.
- Chỉ gọi `get_order` cho candidate đúng shape; leader và mọi contender trong khoảng gap đều được verify (tối đa 2 lookup) để tie vẫn là tie. Decoy sai shape bị reject mà không tốn MCP call.
- Mọi candidate không được chọn nằm trong `rejected_candidates`. Không bao giờ invent order/customer ID; `customer_unique_id` chỉ được điền khi order resolved xuất hiện trong history của customer đó.

**Temporal scoping** (`scoping.py`): MCP có thể trả nhiều record cho cùng một order ID (row "authoritative" của một giai đoạn khác và row trong customer history khớp khiếu nại). Record thuộc case là record có `order_purchase_timestamp` muộn nhất nhưng ≤ `opened_at` **và đã tới hạn** (`order_estimated_delivery_date` ≤ `opened_at`, hoặc trạng thái kết thúc canceled/unavailable) — một đơn còn đang vận chuyển lúc mở case không thể là đối tượng khiếu nại; evidence window chạy từ purchase đó tới purchase của record kế tiếp (mở nếu là record cuối). Item (`shipping_limit_date`), payment/refund/shipment event (`event_at`) chỉ được dùng khi nằm trong window. Lệch giữa `get_order` và record trong phạm vi case được đẩy sang conflict resolver. Item row trùng khít được coi là một bản ghi. Khi nhiều giao dịch thanh toán cùng nằm trong window (cùng timestamp), payment agent tách giao dịch theo `payment_sequential` (mỗi lần reset về 1 là một giao dịch), chọn giao dịch mà evidence giải thích được claim (hoặc khớp giá trị đơn), ghi conflict `payment_transaction` ở trạng thái `UNRESOLVED_INSUFFICIENT_EVIDENCE` và hạ confidence xuống ≤ 0.70.

**A2A envelope**: `AgentTask(case_id, task_id, from_actor="coordinator", to_actor, task_type, payload, evidence_refs)` và `AgentResult(case_id, task_id, actor, status, confidence, data, evidence_refs, warnings, decision_code)`. `A2ABus.accept` từ chối handoff khi: sai case, sai task id, actor khác actor được giao, status ngoài enum, confidence ngoài [0,1], thiếu decision code, evidence ref không do MCP cấp cho case này hoặc chưa từng được consume.

**Tránh vòng lặp**: tối đa 14 task/case và mỗi task type chạy tối đa 2 lần (chỉ `verify_output` dùng lần thứ hai). Specialists không gọi nhau; coordinator là nơi duy nhất quyết định bước tiếp theo. Entity không resolved → không chạy specialist nào (không quét rộng).

## 4. Evidence và conflict lifecycle

1. `EvidenceGateway.call` validate envelope theo `mcp-evidence-response-v1`.
2. `CaseEvidenceStore._register` lưu evidence theo `evidence_ref` do server cấp và ghi owner case; ref xuất hiện ở case khác → `CrossCaseEvidenceError`.
3. Agent chỉ gọi `store.consume(actor, evidence)` khi thực sự dùng dữ liệu → emit `tool_result_consumed` (một lần mỗi actor/ref).
4. Output `evidence_refs` = đúng tập ref đã consume trong case, theo thứ tự, dedupe, ≤ 30. `claim_assessments[].evidence_refs` là tập con liên quan tới claim.

**Source precedence theo field** (`agents/conflict_resolver.py`):

| Field | Ưu tiên | Resolution code |
| --- | --- | --- |
| `order_purchase_timestamp`, `order_status`, `order_delivered_customer_date` | `get_customer_history` (record trong case window) > `get_order` | `CASE_WINDOW_RECORD_SELECTED` |
| `delivered_customer_at` | `get_customer_history` > `get_shipment_summary` header | `CASE_WINDOW_RECORD_SELECTED` |
| `delivery_delay_attribution` | timeline (carrier date vs shipping limit) > shipment event | `AUTHORITATIVE_TIMELINE_SELECTED` |
| Field không có rule | không chọn | `selected_source = null`, `UNRESOLVED_INSUFFICIENT_EVIDENCE` |

Payment ledger/refund timeline luôn được ưu tiên hơn claim text; claim topic chỉ là giả thuyết: khớp evidence thì tăng confidence, lệch thì hạ confidence, không bao giờ override evidence.

**Policy**: primary issue được phân loại từ facts theo thứ tự canceled/unavailable + capture → refund failed/pending → capture mismatch → duplicate → valid split → seller/logistics delay → unsupported. Rule trong `get_policy` cung cấp `case_status`, action, trần refund và loại bên chịu trách nhiệm; refund = min(trần policy, số tiền evidence của issue, refundable). `party_id` của seller luôn lấy từ evidence (late seller hoặc seller của item), không copy từ policy document, và được xác minh lại bằng `get_sellers`.

## 5. Failure and efficiency policy

| Failure | Retry budget | Fallback | Trace event/code |
| --- | ---: | --- | --- |
| MCP timeout / transport error | 1 retry, timeout 90 s/call | ném lỗi → agent trả `AGENT_ERROR`, domain = `insufficient_evidence` | `handoff` `AGENT_ERROR` |
| MCP deterministic error ("no rows") | 0 (cache miss) | `None`; agent báo warning, không suy đoán | `handoff` với status `insufficient_evidence` |
| Entity not found / ambiguous | 0 | không chạy specialist; output `insufficient_evidence`, `needs_investigation` | `ENTITY_NOT_FOUND` / `ENTITY_AMBIGUOUS` |
| Source conflict | 0 | precedence theo field hoặc `selected_source=null` | `policy_decided` `CONFLICT_RESOLVED/UNRESOLVED` |
| Invalid specialist result | 0 | `HandoffError` chặn handoff | không emit `handoff` |
| Verifier fail | 1 repair + re-verify | repair xác định (cap refund, dedupe, clear seller không hợp lệ); vẫn fail → `needs_investigation`, confidence ≤ 0.4 | `VERIFY_FAIL` rồi `VERIFY_PASS/FAIL` |

**Query budget**: cache key `(case_id, tool, normalized_arguments)`, một store mỗi case, hard budget 12 call/case. Kế hoạch call mỗi case resolved: `get_customer_history`, `get_order`, `get_order_items`, `get_product_context` (theo `investigation_scope.include_product_context`), `get_shipment_summary`, `get_payment_timeline`, `get_policy` = 7; thêm `get_refund_timeline` chỉ khi claim/payment event trỏ tới refund; thêm `get_sellers` chỉ khi policy quy trách nhiệm cho seller. Không bao giờ gọi `get_order_payments` khi payment timeline có dữ liệu (timeline là superset), không gọi tool với decoy ID sai shape, không gọi lặp cùng arguments.

## 6. Verification invariants

`agents/verifier.py` chạy trước finalize, không gọi MCP, không sửa output:

- **Schema**: `contracts.validate_output` (L3B v2).
- **Identity**: `case_id` khớp input.
- **Entity**: resolved ∩ rejected = ∅; resolved ⊆ `affected_entities.order_ids`; status `resolved` phải có order; unresolved mà confidence > 0.6 → warning.
- **Evidence**: không trùng ref; mọi ref (kể cả claim refs) thuộc case và đã được consume.
- **Timeline**: `late_seller_ids` ⊆ seller bị ảnh hưởng; `seller_delay` ⇔ có late seller; primary issue delay khớp shipment verdict.
- **Payment/refund**: các tổng ≥ 0; refundable = captured − refunded (±0.01); tổng refund lines = recommended; recommended ≤ refundable; đã `refunded` thì không refund tiếp.
- **Action consistency**: `no_action` ⇒ refund = 0 và không có action refund; `action_required` phải có action; không trùng action.
- **Responsibility**: seller chịu trách nhiệm phải thuộc affected sellers; logistics delay không đổ lỗi seller (conflict resolver).
- **Independent re-derivation**: verifier tự chọn lại scoped record từ raw `get_customer_history`/`get_order` evidence và tự cộng captured trong window từ raw `get_payment_timeline`; lệch với output → `*_not_reproducible`.
- **Confidence**: trong [0,1]; `insufficient_evidence` mà confidence > 0.5 → warning.

**Confidence policy**: entity = điểm ranking (0.95 khi đủ history + claimed + shape + verified); assessment = min(policy confidence, confidence của domain quyết định issue); claim khớp evidence ≤ 0.93, claim lệch evidence ≤ 0.55, conflict chưa resolve ≤ 0.70, thiếu evidence 0.30.

## 7. Reproducibility

- Deterministic: không dùng LLM, không random, không phụ thuộc thời gian thực (trừ timestamp trace do starter tạo). Cùng MCP evidence → cùng output.
- Python ≥ 3.11, dependency bounds trong `pyproject.toml`; môi trường ảo riêng của project (`.venv`).
- Concurrency: tuần tự trong mỗi case và giữa các case trên một MCP session (ưu tiên correctness); timeout 90 s/call, 1 retry cho lỗi transient.
- Lệnh chạy:

```bash
python3.11 -m venv .venv && source .venv/bin/activate
python -m pip install -e ".[dev]"
ruff check . && pytest -q
day09 validate-inputs && day09 mcp-tools
day09 run && day09 validate
day09 package --output dist/submission.zip
```

- Không commit `.env`, inputs, outputs, traces, zip hay API key; `validate_artifacts` chặn chuỗi `sk-team-` trong output/trace. Trace không chứa raw customer data hay reasoning.
