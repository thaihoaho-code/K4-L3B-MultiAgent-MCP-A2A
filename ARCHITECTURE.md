# L3B architecture and Phase 2 interface

This records the Phase 1 contract. The agent modules and final output assembly are intentionally unfinished; `day09 run` cannot produce a submission yet. The public JSON Schemas under `contracts/schemas/` remain authoritative.

## 1. System overview

```text
CLI case_received
  -> coordinator -> entity/customer
  -> order/product -> shipment -> payment/refund
  -> policy -> conflict resolver
  -> assemble L3B output -> verifier -> CLI case_finalized
```

`src/student_agent/workflow.py` owns the coordinator and a new `CaseState` for each case. Phase 1 implements assignment through conflict investigation, with an explicit stop before assembly. Phase 2 adds the final output assembly, the verifier call and `verification_completed` before returning to the CLI. An ambiguous or unresolved entity stops broad downstream queries; Sang must still assemble an honest `needs_investigation` output for that path.

## 2. Agent ownership

| Actor | Owner / module | Input | Responsibility and MCP scope | Result `data` contribution |
| --- | --- | --- | --- | --- |
| `coordinator` | Sang, `workflow.py` | case and specialist results | Routing, per-case state, output assembly; no routine MCP calls | final L3B JSON |
| `entity-agent` | Phát, `agents/entity_customer.py` | `payload.case` | Candidate ranking and customer context; relevant entity/customer tools | `resolved_order_ids`, `customer_unique_id`, `rejected_candidates`, `entity_resolution`, `customer_context` |
| `order-product-agent` | Phúc, `agents/order_product.py` | case, resolved IDs, customer ID | Order, item, seller, product evidence | IDs, statuses, product facts, cause hints |
| `shipment-agent` | Phúc, `agents/shipment.py` | case, resolved IDs, order result | Shipment timeline and delay attribution | `shipment_analysis`, shipment IDs, conflict candidates |
| `payment-refund-agent` | Hoàng, `agents/payment_refund.py` | case, resolved IDs, order result | Captures, payment splits, refunds | `payment_analysis`, payment references, financial proposal |
| `policy-agent` | Hồng, `agents/policy.py` | case and specialist findings | Only policy evidence needed for a decision | policy decisions, allowed/forbidden actions, responsibility hints |
| `conflict-resolver` | Hồng, `agents/conflict_resolver.py` | case and findings | Source precedence and unresolved conflicts; usually no new MCP calls | `data_conflicts`, root causes, actions |
| `verifier` | Hòa, `agents/verifier.py` | draft output and evidence ledger | Validate, never silently invent or rewrite facts | `valid`, `errors`, `warnings` |

Each stub has the same async signature `(task: AgentTask, gateway: EvidenceGateway, trace: TraceWriter) -> AgentResult`. Hồng and Hòa may implement their functions without calling `gateway`. Owners should edit their files and tests; Sang owns `workflow.py`, `a2a.py`, `agent_types.py`, and this architecture record. The old root-level agent placeholders were removed because their `"TODO"` return values and incompatible signatures could appear to be valid results.

## 3. Entity resolution and A2A protocol

`AgentTask` in `agent_types.py` contains `case_id`, `task_id`, `from_actor`, `to_actor`, `task_type`, `payload`, and `evidence_refs`. `AgentResult` contains `case_id`, `actor`, `status`, `confidence`, `data`, `evidence_refs`, `warnings`, and optional `decision_code`. Internal status is exactly `ok`, `ambiguous`, `insufficient_evidence`, `needs_followup`, or `error`; output schema statuses are separate. Confidence is a finite number from 0 to 1.

`a2a.py` fixes routing and codes. The coordinator calls `make_task` and `dispatch`; `dispatch` emits `task_assigned`, invokes the actual handler, checks result case and actor, then emits `handoff`. Errors cannot create a false successful handoff. Task IDs are per-case sequence markers, carried only as short trace metadata. Trace payload never includes case text, customer data, prompts, credentials or chain-of-thought.

Phát should rank candidates using only available hints and MCP evidence, preserve rejected candidate IDs, and return `ambiguous` or `insufficient_evidence` when confidence is too low. An `ok` entity result must contain nonempty `data.resolved_order_ids`; the coordinator rejects an empty `ok` result. The initial confidence bands in the task breakdown are guidance to calibrate against public cases, not automatic thresholds. No agent may promote a weak candidate merely to keep the flow moving.

Current dispatch order is sequential because a safe concurrency policy has not been established. There is no agent-to-agent recursion: every handoff returns to `coordinator`. Future parallel calls must first verify gateway/client safety and keep the same case scope.

## 4. Evidence and conflict lifecycle

The gateway validates each MCP response against `mcp-evidence-response-v1`. A specialist must read the server-provided `evidence_ref` unchanged, emit `tool_result_consumed` with its real tool name when using that result, and return only refs that support its findings. `CaseState` deduplicates returned refs in first-use order. `AgentResult` validates ref syntax but cannot prove provenance; Hòa must check ref ownership against the case's MCP audit during integration. Never invent a ref or reuse one across cases.

Conflict candidates belong in specialist `data`; Hồng converts them to public `data_conflicts` records. Precedence depends on the field: for example, a payment capture needs payment evidence, while shipment time needs shipment evidence. Hồng must record the sources, chosen source and resolution code. If evidence cannot decide, `selected_source` is `null`; no global source priority is assumed. Claim assessments, when emitted, must cite evidence that supports each verdict. Sang deduplicates the final `evidence_refs` and keeps within the schema maximum of 30.

Trace lifecycle: CLI emits `case_received` and `case_finalized`; coordinator dispatch emits assignment and handoff; specialists emit `tool_result_consumed`; Hồng emits `policy_decided` for an actual policy decision; Hòa emits `verification_completed` with `VERIFY_PASS` or `VERIFY_FAIL` before the coordinator returns. Only the seven public event names in `trace-event-v1` are permitted.

## 5. Failure and efficiency policy

| Condition | Phase 2 policy | Observable result |
| --- | --- | --- |
| MCP timeout | At most one retry only for a safe read request; inspect the current gateway timeout before tuning | Specialist warning and `insufficient_evidence` if exhausted |
| Deterministic MCP error | No repeated identical call | Specialist `error` or `insufficient_evidence` |
| Entity not found or ambiguous | Stop broad order/payment search; ask for targeted evidence if justified | Honest `needs_investigation` output |
| Source conflict | Compare field-specific sources; preserve uncertainty | `data_conflicts`, possibly null `selected_source` |
| Invalid specialist result | Stop assembly and correct the owner module | No false `case_finalized` |
| Invalid draft | Verifier returns errors; Sang fixes or allows one targeted retry | `verification_completed` with failure code |

MCP tools must be discovered via `day09 mcp-tools`; names in old placeholder comments were guesses. Keep a case-local cache keyed by tool and normalized arguments after Hòa adds it. Do not query unrelated domains or call the same tool with the same arguments repeatedly. Current starter gateway has a 300-second overall timeout and no retry/cache layer; the table is a Phase 2 implementation policy, not a claim of existing behavior.

## 6. Verification invariants

Before finalize, Hòa must validate the public L3B output schema with `Contracts.validate_output` and inspect at least:

- output `case_id` equals input; all entity/order/payment IDs are in the resolved scope;
- rejected candidates do not appear as affected entities without further evidence;
- every output and claim evidence ref belongs to this team, run and case, and consumed refs appear in trace;
- shipment verdict agrees with the available timeline and late seller IDs;
- payment and refund totals are nonnegative, reconcile with evidence and cap recommended refund;
- selected conflict sources, responsibility, root cause and actions agree with the evidence;
- confidence is bounded and reflects ambiguity, missing evidence and unresolved conflicts;
- final trace has actual assignments, handoffs and verification before CLI finalization.

Verifier reports findings in `AgentResult.data={"valid": bool, "errors": [...], "warnings": [...]}`. Sang decides whether to correct, retry within budget, or stop. Verification must not silently fabricate missing fields or evidence.

## 7. Reproducibility and team handoff

Python 3.11+ and dependencies are declared in `pyproject.toml`; there is no LLM or random seed in Phase 1. Run `python -m pip install -e ".[dev]"`, then `ruff check .` and `pytest -q`. A clean checkout should have no competition input. With real L3B inputs and private `.env`, use `day09 validate-inputs`, `day09 mcp-tools`, then, after Phase 2 assembly is complete, `day09 run` and `day09 validate`. The current code does not claim a successful real-case run. Never commit `.env`, input payloads, outputs, traces or API keys.

Specialists start from `develop` after Sang's scaffold is merged. Their module path and return type above are the stable handoff. Sang retains final assembly and integration; Hòa owns verifier implementation and full QA. See `LAB09_Task_Breakdown_MultiAgent_MCP_A2A.md` for individual test and PR responsibilities.
