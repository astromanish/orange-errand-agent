# Errand Agent — Design Note (Max 1 Page)

## 1. Explicit State Model & Bounded Orchestration
Rather than relying on an unconstrained free-form LLM loop, `ErrandAgent` models the workflow as an explicit state machine (`IDLE` → `CLARIFYING` → `COMPARING` → `AWAITING_APPROVAL` → `BOOKED` / `CANCELLED` / `RESCHEDULED` / `ERROR_TERMINATED`) backed by a structured slot state (`ErrandSlotState`: `service`, `area`, `date_window`, `time_window`, `earliest_hour`, `latest_hour`, `budget_inr`, `selected_provider_id`, `selected_slot`, `active_hold_id`, `quoted_price`).
- **Incremental Slot Updates:** When a user modifies a single constraint (e.g., *"Actually make it not before 10 a.m. and under ₹1,000"*), `_parse_user_constraints()` updates only the changed fields while preserving previously supplied context (`service="plumber"`, `area="Indiranagar"`).
- **Hard Execution Bounds:** Every turn is guarded by `ExecutionLimits` (`max_model_turns=5`, `max_tool_calls=6`, `max_wall_clock_seconds=15.0`). Exceeding any bound aborts cleanly to `ERROR_TERMINATED` with a deterministic user-facing message and zero unverified side effects.

## 2. Application-Enforced Approval Gate & Prompt-Injection Defense
Consequential tools (`book`, `cancel`, `reschedule`) cannot be executed by model output alone:
- **Cryptographic Parameter Binding:** When the user confirms an action, `ErrandStore.grant_approval()` writes a row to `approvals` with a canonical SHA-256 `fingerprint` over `(action, provider_id, slot, quoted_price, booking_id)`.
- **Code-Level Interceptor:** Inside `ToolEnvironment.book()`, `cancel()`, and `reschedule()`, `store.verify_and_consume_approval()` recomputes the SHA-256 fingerprint from the actual execution parameters. If no approval exists, or if `hold_slot` quoted a different price (`price_changed=True`) or the slot changed, prior approvals are automatically invalidated (`invalidated=1`) and `PermissionError` blocks the side effect before any DB write.
- **Untrusted Tool Output Wrapping:** `sanitize_tool_output()` inspects free-text fields (`get_provider` reviews), flags prompt-injection patterns (`SYSTEM OVERRIDE`, `ignore all previous`, `without asking`), strips instruction payloads, and wraps reviews in `[UNTRUSTED_REVIEW_DATA_ONLY]`. Even if a model were tricked, the code-level approval gate rejects any unapproved `book()` call.

## 3. Idempotency, Ambiguous Timeouts & Crash Recovery
- **Deterministic Idempotency Keys:** Computed via `sha256(user_id | action | target_id | slot)`. Retries and repeated user confirmations (`"Yes, confirm"` sent 3x) map to the exact same key and hit `idempotency_ledger`, returning the settled outcome without duplicating bookings.
- **Pre-Write Action Journal & Timeout Resolution:** Before calling `book()`, the agent writes `state='started'` to `action_journal`. If `book()` returns `network_timeout` (15% fault rate, half of which actually booked underneath) or the process restarts mid-booking (`recover_pending_actions()`), the agent queries `get_booking_status(idempotency_key)` first. If `"booked"`, it reports the existing `booking_id`; if `"not_booked"`, it safely retries with the same `idempotency_key`.

## 4. Persistent Memory & Strict Token-Budget Context Packing
` ErrandStore` tracks `source` (`explicit` vs `inferred`), `confirmed` (`1` vs `0`), and `status` (`active` vs `forgotten`). With 55 seeded memories and a 30-message history, `ContextPacker.build_context()` enforces a strict token ceiling (`max_tokens=360–450`):
1. Hard-excludes all `forgotten` memories and all unconfirmed `inferred` memories (`confirmed=0`).
2. Ranks remaining memories by domain relevance (global errand prefs + active service tags) and packs recent history until the token ceiling is reached, logging a `context_manifest` event every turn.

## 5. Observability & Annotated Trace Excerpt (`timeout-after-success`)
Every turn logs `context_manifest`, `model_call` (tokens, latency, cost), `tool_call` (sanitized outputs, injection warnings), and recovery events to `traces` (full raw file: `traces/timeout_after_success_trace.json`):
```json
// Turn 1: Context packed under budget (55 seeded -> 3 relevant explicit included, 2 forgotten & 2 unconfirmed excluded)
{"turn_index": 1, "event_type": "context_manifest", "payload": {"token_budget": 450, "total_tokens_used": 354,
  "included_memory_keys": ["earliest_acceptable_time", "preferred_area", "default_budget_inr"],
  "excluded_forgotten_keys": ["old_door_code", "old_area"],
  "excluded_unconfirmed_inferred_keys": ["inferred_luxury_plumber", "inferred_weekend_only"]}}
// Turn 1: get_provider review injection caught & neutralized; slot held at ₹850
{"turn_index": 1, "event_type": "tool_call", "payload": {"tool": "get_provider", "args": ["prov_plumb_01"],
  "warnings": ["prompt_injection_detected_in_review[2]: matched 'ignore\\s+(all\\s+)?previous...'"]}}
{"turn_index": 1, "event_type": "tool_call", "payload": {"tool": "hold_slot", "result": {"hold_id": "hold_...", "quoted_price": 850}}}
// Turn 2: User confirms -> book() times out after succeeding underneath -> resolved via get_booking_status
{"turn_index": 2, "event_type": "tool_call", "payload": {"tool": "book", "result": {"status": "timeout", "error_code": "network_timeout"}}}
{"turn_index": 2, "event_type": "tool_call", "payload": {"tool": "get_booking_status",
  "result": {"status": "booked", "booking": {"booking_id": "bk_...", "provider_id": "prov_plumb_01", "price": 850}}}}
```
