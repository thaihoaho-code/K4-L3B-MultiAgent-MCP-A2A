# L3B architecture record — Sang stages 1–5

This record describes the typed interface, per-case state, coordinator routing and final handoff consistency gate for the flow approved by the team. Specialist business investigation and real-case acceptance still require their owners and Sang stage 6. Public schemas in `contracts/schemas/` remain authoritative.

## 1. System overview

```text
EntityAgent → EntityResult
                  ↓ customer_unique_id, resolved_order_id
          ┌───────┴────────┐
OrderShipmentAgent       PaymentAgent
          ↓                    ↓
   ShipmentResult        PaymentResult
          └────────┬───────────┘
                   ↓
               PolicyAgent
                   ↓
               PolicyResult
                   ↓
         VerifierAgent → dict (L3B output)
```

The current `main` branch has one combined `OrderShipmentAgent` and one `PolicyAgent` responsible for policy and conflict handling. We preserve those modules and their method signatures. `solve_case` now routes through `run_case` in `workflow.py`; the older `coordinator_agent.py` is not the CLI path.

## 2. Agent ownership and typed interface

| Actor name | Existing module/class | Method and return type | Owner |
| --- | --- | --- | --- |
| `coordinator` | `workflow.py` / `coordinator_agent.py` | `solve_case(case, gateway, trace) -> dict` | Sang |
| `entity-agent` | `entity_agent.py` / `EntityAgent` | `resolve(case, gateway, trace) -> EntityResult` | Phát |
| `order-shipment-agent` | `order_shipment_agent.py` / `OrderShipmentAgent` | `investigate(entity, gateway, trace) -> ShipmentResult` | Phúc |
| `payment-refund-agent` | `payment_agent.py` / `PaymentAgent` | `check_transactions(entity, gateway, trace) -> PaymentResult` | Hoàng |
| `policy-agent` | `policy_agent.py` / `PolicyAgent` | `resolve_conflict(case, entity, shipment, payment, gateway, trace) -> PolicyResult` | Hồng |
| `verifier` | `verifier_agent.py` / `VerifierAgent` | `verify_and_format(case, entity, shipment, payment, policy, trace, ...) -> dict` | Hòa |

`agent_types.py` declares protocols matching these signatures. `agents/__init__.py` gives stable imports without moving or editing specialist files. The existing typed dataclasses in `state.py` remain the specialist result contract. `AgentResult[T]` is an A2A **envelope around** a typed result, not a replacement for those dataclasses.

## 3. Entity resolution and A2A protocol

The coordinator is the only task sender. `AgentTask` carries `case_id`, task ID, sender, recipient, task type, a small internal payload and prior evidence refs. `AgentResult[T]` carries the same case/task correlation, actor, internal status (`ok`, `ambiguous`, `insufficient_evidence`, `needs_followup`, `error`), typed data, evidence refs, optional confidence/warnings and a decision code. `a2a.py` fixes task-to-actor names and assignment codes, builds tasks and checks handoff correlation. The task sequence is local to each `CoordinatorState`; no global task counter exists.

`CoordinatorState` stores the input, one result for each actual agent in the diagram, an optional verifier result and evidence refs in first-seen order. It rejects a handoff from another case, wrong actor, wrong result type or duplicate completed task. `CaseGateway` wraps the supplied MCP gateway for each case. It rejects a different `case_id` and records server-issued refs. `dispatch` requires every specialist handoff ref to have been observed through that gateway in this case. The real MCP server's team/run audit remains authoritative.

`run_case` assigns Entity first. Only `resolution_status=resolved` with a selected order permits OrderShipment and Payment tasks. These two independent branches run sequentially because the existing gateway shares one MCP session. Policy runs after both handoffs, then Verifier formats the output. For `ambiguous` or `not_found`, the coordinator skips order, payment and policy calls and sends explicit empty domain results to Verifier. Phát owns candidate ranking, rejected candidates and the threshold for choosing a resolved order; the coordinator never invents a fallback order.

## 4. Evidence and conflict lifecycle

The public MCP response supplies `evidence_ref`; agents must not synthesize it. Specialists emit `tool_result_consumed` only for evidence actually used. The coordinator emits `task_assigned` before each call and `handoff` only after a typed, case-correlated, provenance-checked result. It collects refs in first-seen order. Specialist `policy_decided` and `verification_completed` events are deferred so the coordinator emits a single event only after accepting the corresponding result; a substantive policy decision also requires evidence. A failed verifier emits `VERIFY_FAIL` and the error propagates. Trace metadata contains task ID, status, evidence count and confidence, not customer data, prompts or private reasoning. Hồng owns field-specific source precedence and unresolved conflicts in `PolicyResult.data_conflicts`.

## 5. Failure and efficiency policy

The starter gateway has an overall 300-second timeout and no response cache or retry layer. Coordinator adds no retries or speculative calls. An ambiguous or missing entity does not trigger order/payment/policy MCP searches. A failed specialist call propagates and receives no successful handoff; it must be handled at the CLI/run boundary. Later work can add bounded retries for safe reads and case-local caching after observing real MCP behavior.

## 6. Verification invariants — stage 5 implemented, real-case acceptance pending

Before accepting Verifier's dict, the coordinator requires strict JSON (no NaN or infinity), validates the L3B schema, `case_id`, and equality between output refs and accepted handoff refs. It checks every direct output section against the accepted Entity, Shipment, Payment and Policy results: entity status and IDs, rejected candidates, customer context, affected IDs, shipment verdict and timeline flag, payment verdict and amounts, policy assessment and confidence, root causes, conflicts, financial resolution and actions. It rejects a resolved order also marked rejected, a shipment scope that excludes its selected order, a conflict whose selected source is not among its sources, a refund recommendation that differs from its line total by more than half a cent, and more than eight actions before the existing verifier can silently truncate them. Optional claim refs must belong to accepted handoffs and claim IDs must be unique. Schema validates bounds, required fields and enums; A2A envelopes validate specialist confidence bounds.

Only after these checks does the coordinator emit `VERIFY_CONSISTENCY_PASS` with scope `schema_provenance_consistency`. A failure emits one `VERIFY_FAIL` and no verifier handoff. This verifies internal consistency of the available results. It cannot prove that an MCP result belongs to the correct team/run or that a specialist's conclusion, payment arithmetic, source precedence, timeline or claim verdict is *factually correct* for the case. Hòa and the other specialist owners still need to complete their business logic; stage 6 must run real L3B cases and inspect MCP audit, outputs and traces. Current integration tests use synthetic agents and a fake gateway only.

## 7. Reproducibility

Python 3.11+ and dependency bounds are in `pyproject.toml`. Run `ruff check .` and `pytest -q` on a clean checkout. Real L3B input and MCP access are required before `day09 validate-inputs`, `day09 mcp-tools`, `day09 run` and `day09 validate` can prove a submission. Do not commit `.env`, case inputs, outputs, traces or API keys. The team now creates feature branches from `main` and opens PRs into `main`; the older `develop` scaffold has a different interface and should not be merged wholesale.
