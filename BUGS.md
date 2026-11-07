# Three Real Bugs Hit During Development (Deliverable #4)

## Bug 1: Same-Tick Approval Timestamp Tie Causing False `APPROVAL_MISMATCH` on Reschedule Retry

### What broke
In `test_6_reschedule_slot_conflict_and_success`, a user approved rescheduling an AC servicing booking (`bk_seed_...`) to `2026-11-14T14:00`. Calling `reschedule()` consumed that approval and returned `slot_conflict`. The user then immediately granted a second approval for the alternative slot `2026-11-14T10:00`. When `reschedule()` ran for `10:00`, `verify_and_consume_approval()` threw:
`PermissionError: APPROVAL_MISMATCH: Material details changed since approval (approved slot=2026-11-14T14:00 vs requested slot=2026-11-14T10:00)`.

### Why it broke
1. `grant_approval()` invalidated prior *unconsumed* approvals (`WHERE consumed = 0`), leaving the already-consumed `14:00` approval row with `invalidated = 0`.
2. Both approvals were created within the same clock tick (`granted_at` had identical float timestamps).
3. `verify_and_consume_approval()` queried `WHERE user_id = ? AND action = ? AND invalidated = 0 ORDER BY granted_at DESC LIMIT 1`. Because `granted_at` tied, SQLite returned the earlier row (`14:00`) first, triggering a fingerprint mismatch against `10:00`.

### The fix
Added secondary ordering by `rowid DESC` (`ORDER BY granted_at DESC, rowid DESC LIMIT 1`) in `ErrandStore.verify_and_consume_approval()` (`errand_agent/store.py`) and `ToolEnvironment.hold_slot()` (`errand_agent/tools.py`) so the most recently inserted approval always wins even on identical timestamps.

### Guarding regression test
- `tests/test_phase1_store_and_tools.py::Phase1StoreAndToolsTest::test_6_reschedule_slot_conflict_and_success`
- `eval/run_eval.py::s17_reschedule_slot_conflict_and_resolution`

---

## Bug 2: Crash Recovery `get_booking_status(idempotency_key)` Returning `not_found` When Process Died Before `book()`

### What broke
In evaluation scenario `S15` (`s15_restart_mid_booking_recovery`), we simulated a process crash right after `store.log_action_start(user_id, "book", idempotency_key, hold_id=...)` was written to `action_journal`, but *before* `env.book()` executed. When the restarted agent ran `recover_pending_actions()`, it queried `get_booking_status(idempotency_key)` and got `status="not_found"` instead of `"not_booked"`, causing recovery to abort (`resolution="not_found"`, `bookings=0`) instead of completing the held booking.

### Why it broke
`hold_slot()` only knows `hold_id` — the deterministic `idempotency_key` is generated later when the agent prepares to call `book()` and logs to `action_journal`. Because the crash occurred before `book()` wrote to `bookings` or `idempotency_ledger`, looking up `idempotency_key` in `bookings`, `idempotency_ledger`, and `holds` (by `hold_id = key`) missed all three tables.

### The fix
Updated `ToolEnvironment.get_booking_status(key)` (`errand_agent/tools.py`) to also resolve `hold_id` through `action_journal` when `key` is a pre-logged `idempotency_key`:
```python
j_row = self.store._conn.execute(
    "SELECT hold_id FROM action_journal WHERE idempotency_key = ? AND hold_id IS NOT NULL",
    (key,),
).fetchone()
```
This links the pre-execution journal entry back to the active hold and returns `status="not_booked"` (or `"hold_expired"`), allowing `recover_pending_actions()` to safely complete the booking once.

### Guarding regression test
- `eval/run_eval.py::s15_restart_mid_booking_recovery`
- `tests/test_phase2_agent_and_context.py::Phase2AgentAndContextTest::test_5_process_restart_mid_booking_recovery`

---

## Bug 3: Unescaped Trace & Booking Payloads in Side-by-Side Web Inspector (`innerHTML` Injection)

### What broke
When rendering the live trace log in the side-by-side Web UI (`errand_agent/web/index.html`), provider reviews containing angle brackets or adversarial HTML/script payloads inside `get_provider` tool results were interpolated directly into `traceLog.innerHTML` via template strings.

### Why it broke
`traceLog.innerHTML` rendered `JSON.stringify(t.payload, null, 2)` without HTML-escaping `&`, `<`, `>`, and `"`. Because Scenario `S06` specifically tests adversarial prompt-injection strings in provider reviews, any review containing raw HTML tags could break the trace DOM or execute script in the inspector pane.

### The fix
Added an explicit `esc(s)` HTML entity encoder in `errand_agent/web/index.html` and wrapped all interpolated fields (`booking_id`, `provider_name`, `slot`, `fingerprint`, and `JSON.stringify(t.payload)`) before setting `innerHTML`.

### Guarding regression test
- Verified via live HTTP server smoke test and `S06` adversarial review trace rendering.
