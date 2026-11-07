# AI Coding Assistant Transcript — Building Errand Agent

**Project:** Orange Invest Founding AI Engineer Assignment (`orange-errand-agent`)  
**Date:** November 7, 2026  
**Assistant:** Hermes Agent  

---

## Session Log Summary & Prompts

### Turn 1 — Assignment Analysis & Architecture Plan
- **User Prompt:** *"Check orange invest assignment task in details and prepare plan for building"*
- **Assistant Actions:**
  - Pulled the assignment email (`Founding AI Engineer Assignment | Orange Invest 🍊 | Binary`) and extracted `Orange_Engineering_Assignment.pdf`.
  - Identified the 8 fixed tool contracts (`search_providers`, `get_provider`, `hold_slot`, `book`, `get_booking_status`, `cancel`, `reschedule`, `list_bookings`), required fault distributions (70% success / 15% slot_taken / 15% timeout with half succeeding underneath, 1st-call 503 on cancel, slot conflict on reschedule, price drift on hold, and prompt-injection in reviews), and the 7 deliverables.
  - Proposed a 5-phase bottom-up build plan:
    1. Phase 1: SQLite Store (`ErrandStore`) + 8 Mock Tools (`ToolEnvironment` & `ScenarioConfig`).
    2. Phase 2: Context Budget Packer (50+ memories), Untrusted Review Sanitizer, Stateful `ErrandAgent`, and Proactive Cancellation Handler.
    3. Phase 3: 22-Scenario Automated Evaluation Harness (`eval/run_eval.py`) + Auto-Generated `EVAL_RESULTS.md` + Raw Trace (`traces/timeout_after_success_trace.json`).
    4. Phase 4: Side-by-Side Web UI (`errand_agent/server.py` + `errand_agent/web/index.html`).
    5. Phase 5: `DESIGN_NOTE.md`, `BUGS.md`, `README.md`, and Transcript export.

---

### Turn 2 — Phase 1: SQLite Store, Approval Fingerprints & 8 Mock Tools
- **User Prompt:** *"let's start building phase 1"*
- **Assistant Actions:**
  - Initialized Git repository at `/home/astro/Documents/projects/orange-errand-agent`.
  - Implemented `errand_agent/store.py` with tables for `holds`, `bookings`, `idempotency_ledger`, `cancel_attempts`, `action_journal`, `approvals`, `memories`, `conversation_messages`, and `traces`.
  - Implemented `compute_approval_fingerprint()` and `verify_and_consume_approval()` to enforce user confirmation in application code and invalidate approvals on material changes (provider, slot, price).
  - Implemented `errand_agent/tools.py` with all 8 mock tools and `ScenarioConfig`.
  - Wrote `tests/test_phase1_store_and_tools.py`.
  - **Bug Hit & Fixed (#1):** `test_6_reschedule_slot_conflict_and_success` failed with `APPROVAL_MISMATCH` because two approvals granted in the same clock tick tied on `granted_at DESC`. Fixed by ordering `ORDER BY granted_at DESC, rowid DESC LIMIT 1`.
  - Verified 7/7 Phase 1 tests passing; committed as `37c50c9`.

---

### Turn 3 — Phase 2: Context Budget Packer, Prompt Injection Defense & Agent Loop
- **User Prompt:** *"Yes proced"*
- **Assistant Actions:**
  - Implemented `errand_agent/context.py`:
    - `seed_fifty_plus_memories_and_long_history()` seeding 55 memories (explicit, unconfirmed inferred, forgotten, and 46 historical noise entries) + 30 prior chat messages.
    - `ContextPacker.build_context()` enforcing a hard token budget, excluding `forgotten` and unconfirmed `inferred` (`confirmed=0`) memories, and logging `context_manifest` per turn.
    - `sanitize_tool_output()` wrapping free-text provider reviews in `[UNTRUSTED_REVIEW_DATA_ONLY]` and redacting prompt-injection instructions.
  - Implemented `errand_agent/agent.py` (`ErrandAgent`):
    - Explicit state machine (`IDLE`, `CLARIFYING`, `COMPARING`, `AWAITING_APPROVAL`, `BOOKED`, `CANCELLED`, `RESCHEDULED`, `ERROR_TERMINATED`).
    - Incremental constraint updates preserving unrelated context.
    - Hard execution limits (`max_model_turns`, `max_tool_calls`, `max_wall_clock_seconds`).
    - `recover_pending_actions()` for mid-booking restart recovery and `handle_proactive_cancellation()` for unprompted provider cancellation follow-up.
  - Wrote `tests/test_phase2_agent_and_context.py` (12/12 unit tests passing); committed as `cddf4fb`.

---

### Turn 4 — Phase 3: 22-Scenario Automated Evaluation Suite & Canonical Trace
- **User Prompt:** *"Proced"*
- **Assistant Actions:**
  - Implemented `eval/run_eval.py` with 22 automated scenarios (`S01`–`S22`) and `tests/test_phase3_eval_suite.py`.
  - **Bug Hit & Fixed (#2):** `S15` (`s15_restart_mid_booking_recovery`) failed with `resolution=not_found` when a crash occurred after `action_journal` logged `idempotency_key` but before `book()` wrote to `bookings` or `idempotency_ledger`. Fixed `get_booking_status()` in `errand_agent/tools.py` to join against `action_journal.hold_id` when queried by a pre-logged `idempotency_key`.
  - Re-ran `python3 eval/run_eval.py`: **22/22 scenarios passed**, generating `EVAL_RESULTS.md` and `traces/timeout_after_success_trace.json`; committed as `554065a`.

---

### Turn 5 — Phase 4 & 5: Side-by-Side Web UI, Bug Log & Documentation
- **User Prompt:** *"Yes"*
- **Assistant Actions:**
  - Built `errand_agent/server.py` and `errand_agent/web/index.html` (split-screen Chat + Live Fault Injection on the left, real-time State + Context Budget + Approval Fingerprint + Tool Trace on the right).
  - **Bug Hit & Fixed (#3):** Escaped all trace and booking fields via `esc()` before rendering into `innerHTML` in `index.html` to prevent adversarial review strings from injecting raw HTML into the trace viewer.
  - Smoke-tested the HTTP server end-to-end (`/api/state`, `/api/chat`, `/api/faults` with `timeout_booked` recovery).
  - Authored `DESIGN_NOTE.md`, `BUGS.md`, `README.md`, and `AI_CODING_TRANSCRIPT.md`.
