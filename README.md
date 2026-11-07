# Errand Agent 🍊 — Founding AI Engineer Assignment

A local, stateful chat agent that arranges home-service errands (plumbers, electricians, AC servicing) with **code-enforced approval gates**, **idempotent timeout & crash recovery**, **prompt-injection defense**, **50+ memory token-budget packing**, **proactive provider-cancellation follow-up**, and a **22-scenario automated evaluation suite**.

---

## Quick Start

Requires only **Python 3.10+** (zero external package dependencies — uses Python standard library `sqlite3`, `http.server`, `unittest`).

### 1. Run Unit Tests & 22-Scenario Automated Evaluation Suite
```bash
# Run all unit + evaluation tests
python3 -m unittest discover -s tests -v

# Run the 22-scenario evaluation suite and regenerate EVAL_RESULTS.md + traces/timeout_after_success_trace.json
python3 eval/run_eval.py
```

### 2. Launch the Side-by-Side Web UI (Chat + Live Observability Trace)
```bash
python3 -m errand_agent.server --port 8765
# Open http://127.0.0.1:8765 in your browser
```
The split-screen UI displays:
- **Left Pane:** Chat interface, 1-click scenario prompts, live fault-injection toggles (*Normal Success*, *Timeout-After-Success*, *Slot Taken*, *Mutate Hold Price +₹300*, *Simulate Provider Cancel*), and live Bookings & Memory counters.
- **Right Pane:** Real-time state machine strip, context token budget meter (`<= 420 tokens` out of 55 seeded memories + 30 historical messages), approval fingerprint status, and step-by-step sanitized tool traces.

---

## Repository Map & Deliverables Checklist

| Deliverable | File / Location | Description |
|---|---|---|
| **1. Implementation & Mocks** | `errand_agent/store.py`<br>`errand_agent/tools.py`<br>`errand_agent/context.py`<br>`errand_agent/agent.py`<br>`errand_agent/server.py` | SQLite store, 8 deterministic mock tools with configurable faults, token-budget context packer, prompt-injection sanitizer, state-machine agent, and side-by-side web server. |
| **2. Design Note (≤ 1 page)** | [`DESIGN_NOTE.md`](./DESIGN_NOTE.md) | State model, code-level approval enforcement, idempotency & crash recovery, persistent memory budgeting, and annotated trace excerpt. |
| **3. Auto-Generated Eval Table** | [`EVAL_RESULTS.md`](./EVAL_RESULTS.md)<br>`eval/run_eval.py` | **22/22 scenarios passing (100%)**, generated automatically by `python3 eval/run_eval.py`. |
| **4. Three Real Bugs** | [`BUGS.md`](./BUGS.md) | Root-cause analysis, fix, and regression tests for 3 real bugs encountered during build. |
| **5. End-to-End Raw Trace** | [`traces/timeout_after_success_trace.json`](./traces/timeout_after_success_trace.json) | Complete JSON trace of a booking run where `book()` times out after succeeding underneath and is resolved via `get_booking_status`. |
| **6. Side-by-Side Demo UI** | `errand_agent/web/index.html` | Split-screen Chat + Live Trace UI for the 3–5 min video walkthrough. |
| **7. AI Coding Transcript** | [`AI_CODING_TRANSCRIPT.md`](./AI_CODING_TRANSCRIPT.md) | Transcript of the AI coding assistant session used to design, build, test, and debug the system. |
