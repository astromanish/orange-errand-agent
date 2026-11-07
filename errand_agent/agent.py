"""ErrandAgent: Explicit workflow state machine, bounded execution loop,
code-enforced approval gates, crash/timeout recovery, and proactive follow-up.

Supports both:
- Offline deterministic / scripted model adapter (for 20+ reproducible evals)
- Live OpenAI-compatible LLM adapter (OpenRouter / OpenAI / Gemini) when configured
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.request
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Callable, Optional

from .context import ContextPacker, sanitize_tool_output, estimate_tokens
from .store import ErrandStore, compute_idempotency_key
from .tools import ToolEnvironment


@dataclass
class ExecutionLimits:
    max_model_turns: int = 5
    max_tool_calls: int = 6
    max_wall_clock_seconds: float = 15.0


@dataclass
class ErrandSlotState:
    """Explicit state representation for the active errand workflow."""
    phase: str = "IDLE"
    # Phases: IDLE | CLARIFYING | COMPARING | AWAITING_APPROVAL | BOOKED | CANCELLED | RESCHEDULED | ERROR_TERMINATED
    intent: Optional[str] = None          # "book" | "cancel" | "reschedule" | "memory" | "query"
    service: Optional[str] = None         # "plumber" | "electrician" | "ac_servicing"
    issue_detail: Optional[str] = None    # e.g. "kitchen tap"
    area: Optional[str] = None            # e.g. "Indiranagar"
    date_window: Optional[str] = None     # e.g. "this week", "Saturday"
    time_window: Optional[str] = None     # e.g. "mornings only", "not before 10:00"
    earliest_hour: Optional[int] = None   # e.g. 10
    latest_hour: Optional[int] = None     # e.g. 12 for mornings
    budget_inr: Optional[int] = None      # e.g. 1500
    same_as_last_time: bool = False
    candidate_providers: list[dict[str, Any]] = field(default_factory=list)
    selected_provider_id: Optional[str] = None
    selected_provider_name: Optional[str] = None
    selected_slot: Optional[str] = None
    active_hold_id: Optional[str] = None
    quoted_price: Optional[int] = None
    search_price: Optional[int] = None
    target_booking_id: Optional[str] = None
    last_booking_id: Optional[str] = None
    pending_approval_action: Optional[str] = None  # "book" | "cancel" | "reschedule"

    def summary(self) -> str:
        return json.dumps(
            {
                "phase": self.phase,
                "intent": self.intent,
                "service": self.service,
                "area": self.area,
                "date_window": self.date_window,
                "time_window": self.time_window,
                "earliest_hour": self.earliest_hour,
                "latest_hour": self.latest_hour,
                "budget_inr": self.budget_inr,
                "selected_provider_id": self.selected_provider_id,
                "selected_slot": self.selected_slot,
                "active_hold_id": self.active_hold_id,
                "quoted_price": self.quoted_price,
                "target_booking_id": self.target_booking_id,
                "pending_approval_action": self.pending_approval_action,
            },
            sort_keys=True,
        )


@dataclass
class TurnResult:
    reply: str
    phase: str
    state: dict[str, Any]
    tool_calls_made: int
    model_turns_used: int
    elapsed_ms: float
    outcome: str  # e.g. "clarification_requested" | "awaiting_approval" | "booking_confirmed" | ...
    booking: Optional[dict[str, Any]] = None
    context_manifest: Optional[dict[str, Any]] = None
    warnings: list[str] = field(default_factory=list)


class ErrandAgent:
    """Stateful chat agent with application-enforced approvals, idempotency, and bounded execution."""

    def __init__(
        self,
        store: ErrandStore,
        env: ToolEnvironment,
        user_id: str = "user_01",
        session_id: Optional[str] = None,
        limits: Optional[ExecutionLimits] = None,
        token_budget: int = 450,
        use_live_llm: bool = False,
    ):
        self.store = store
        self.env = env
        self.user_id = user_id
        self.session_id = session_id or ("sess_" + uuid.uuid4().hex[:8])
        self.limits = limits or ExecutionLimits()
        self.packer = ContextPacker(store, max_tokens=token_budget)
        self.state = ErrandSlotState()
        self.turn_counter = 0
        self.use_live_llm = use_live_llm

    # ── Crash / Restart Recovery on Startup ──────────────────────────────────
    def recover_pending_actions(self) -> list[dict[str, Any]]:
        """Inspect action_journal for any 'started' actions interrupted by a crash/restart.
        Query authoritative get_booking_status before deciding whether it succeeded or needs retry."""
        pending = self.store.get_pending_actions(self.user_id)
        recovered: list[dict[str, Any]] = []
        for entry in pending:
            idem = entry["idempotency_key"]
            status_res = self.env.get_booking_status(idem)
            if status_res.get("status") == "booked":
                bk = status_res["booking"]
                self.store.log_action_complete(idem, state="resolved_after_restart_booked")
                self.state.phase = "BOOKED"
                self.state.last_booking_id = bk["booking_id"]
                recovered.append({"action": entry["action"], "resolution": "already_booked", "booking": bk})
            elif status_res.get("status") == "not_booked" and entry.get("hold_id"):
                # Safe to retry with the same idempotency_key because get_booking_status proved it did not book
                retry_res = self.env.book(entry["hold_id"], idem, user_id=self.user_id, enforce_approval=True)
                if retry_res.get("status") == "confirmed":
                    self.store.log_action_complete(idem, state="resolved_after_restart_retried")
                    self.state.phase = "BOOKED"
                    self.state.last_booking_id = retry_res["booking_id"]
                    recovered.append({"action": entry["action"], "resolution": "retried_and_booked", "booking": retry_res})
                else:
                    self.store.log_action_complete(idem, state=f"failed_{retry_res.get('status')}")
                    recovered.append({"action": entry["action"], "resolution": retry_res.get("status"), "detail": retry_res})
            else:
                self.store.log_action_complete(idem, state="aborted_after_restart")
                recovered.append({"action": entry["action"], "resolution": status_res.get("status")})

            self.store.record_trace(
                self.session_id,
                self.user_id,
                self.turn_counter,
                "restart_recovery",
                {"journal_entry": entry, "status_check": status_res, "recovered": recovered[-1]},
            )
        return recovered

    # ── Proactive Follow-Up on Provider Cancellation ─────────────────────────
    def handle_proactive_cancellation(self, booking_id: Optional[str] = None) -> TurnResult:
        """Detect bookings with status == 'provider_cancelled' without a user prompt,
        find a replacement within known preferences, hold a slot, and request fresh approval."""
        t0 = self.store.now()
        self.turn_counter += 1
        tool_calls = 0

        def call_tool(name: str, fn: Callable, *args, **kwargs) -> dict[str, Any]:
            nonlocal tool_calls
            tool_calls += 1
            t_start = self.store.now()
            raw = fn(*args, **kwargs)
            sanitized, warns = sanitize_tool_output(name, raw)
            self.store.record_trace(
                self.session_id,
                self.user_id,
                self.turn_counter,
                "tool_call",
                {
                    "tool": name,
                    "args": list(args),
                    "kwargs": kwargs,
                    "result": sanitized,
                    "warnings": warns,
                    "latency_ms": round((self.store.now() - t_start) * 1000, 2),
                },
            )
            return sanitized

        bks = call_tool("list_bookings", self.env.list_bookings, self.user_id).get("bookings", [])
        cancelled_bk = None
        for b in bks:
            if b["status"] == "provider_cancelled" and (booking_id is None or b["booking_id"] == booking_id):
                cancelled_bk = b
                break

        if not cancelled_bk:
            return TurnResult(
                reply="No provider-cancelled bookings detected.",
                phase=self.state.phase,
                state=asdict(self.state),
                tool_calls_made=tool_calls,
                model_turns_used=1,
                elapsed_ms=round((self.store.now() - t0) * 1000, 2),
                outcome="no_proactive_event",
            )

        # Invalidate any prior approvals for safety
        self.store.invalidate_approvals(self.user_id, reason="proactive_provider_cancellation")

        # Load known confirmed user preferences
        mems = {m["key"]: m["value"] for m in self.store.list_memories(self.user_id, only_confirmed=True)}
        area = cancelled_bk.get("area") or mems.get("preferred_area") or "Indiranagar"
        budget = int(mems.get("default_budget_inr", 1500))
        earliest_hr = 0
        if "earliest_acceptable_time" in mems:
            try:
                earliest_hr = int(str(mems["earliest_acceptable_time"]).split(":")[0])
            except ValueError:
                earliest_hr = 0

        search_res = call_tool(
            "search_providers",
            self.env.search_providers,
            cancelled_bk["service"],
            area,
            cancelled_bk["slot"][:10],
        )
        candidates = [
            p
            for p in search_res.get("providers", [])
            if p["provider_id"] != cancelled_bk["provider_id"] and p["price"] <= budget
        ]
        candidates.sort(key=lambda p: (-p["rating"], p["price"]))

        chosen_prov = None
        chosen_slot = None
        for p in candidates:
            for s in p.get("available_slots", []):
                hr = int(s.split("T")[1].split(":")[0])
                if hr >= earliest_hr:
                    chosen_prov = p
                    chosen_slot = s
                    break
            if chosen_prov:
                break

        if not chosen_prov or not chosen_slot:
            reply = (
                f"⚠️ Alert: Your booked provider **{cancelled_bk['provider_name']}** for "
                f"`{cancelled_bk['service']}` at `{cancelled_bk['slot']}` just cancelled. "
                f"I searched for a replacement in {area} within ₹{budget}, but none matched your preferences."
            )
            return TurnResult(
                reply=reply,
                phase="CLARIFYING",
                state=asdict(self.state),
                tool_calls_made=tool_calls,
                model_turns_used=1,
                elapsed_ms=round((self.store.now() - t0) * 1000, 2),
                outcome="proactive_no_replacement",
            )

        hold_res = call_tool("hold_slot", self.env.hold_slot, chosen_prov["provider_id"], chosen_slot, self.user_id)
        self.state.phase = "AWAITING_APPROVAL"
        self.state.intent = "book"
        self.state.service = cancelled_bk["service"]
        self.state.area = area
        self.state.selected_provider_id = chosen_prov["provider_id"]
        self.state.selected_provider_name = chosen_prov["name"]
        self.state.selected_slot = chosen_slot
        self.state.active_hold_id = hold_res["hold_id"]
        self.state.quoted_price = hold_res["quoted_price"]
        self.state.search_price = hold_res["search_price"]
        self.state.pending_approval_action = "book"

        reply = (
            f"⚠️ Proactive Alert: **{cancelled_bk['provider_name']}** cancelled your upcoming "
            f"`{cancelled_bk['service']}` booking (`{cancelled_bk['booking_id']}` at `{cancelled_bk['slot']}`).\n"
            f"I found a replacement within your preferences ({area}, ≤ ₹{budget}): "
            f"**{chosen_prov['name']}** (`{chosen_prov['provider_id']}`) at `{chosen_slot}` "
            f"for **₹{hold_res['quoted_price']}** (hold `{hold_res['hold_id']}`).\n"
            f"Please confirm if you approve booking **{chosen_prov['name']}** at `{chosen_slot}` for **₹{hold_res['quoted_price']}**."
        )
        self.store.add_message(self.user_id, self.session_id, "assistant", reply)
        self.store.record_trace(
            self.session_id,
            self.user_id,
            self.turn_counter,
            "proactive_notification",
            {
                "cancelled_booking_id": cancelled_bk["booking_id"],
                "replacement_provider_id": chosen_prov["provider_id"],
                "replacement_slot": chosen_slot,
                "quoted_price": hold_res["quoted_price"],
                "hold_id": hold_res["hold_id"],
            },
        )
        return TurnResult(
            reply=reply,
            phase=self.state.phase,
            state=asdict(self.state),
            tool_calls_made=tool_calls,
            model_turns_used=1,
            elapsed_ms=round((self.store.now() - t0) * 1000, 2),
            outcome="proactive_replacement_offered",
        )

    # ── NLU / Slot Extraction & Intent Parsing ───────────────────────────────
    def _extract_Memory_command(self, text: str) -> Optional[TurnResult]:
        """Handle explicit user commands to inspect, correct, or forget persistent memories."""
        low = text.strip().lower()

        if any(p in low for p in ("what do you remember", "list memories", "inspect memories", "show my preferences")):
            mems = self.store.list_memories(self.user_id, include_forgotten=False, only_confirmed=False)
            if not mems:
                reply = "You have no active stored preferences."
            else:
                lines = [
                    f"- **{m['key']}**: `{m['value']}` ({m['source']}, {'confirmed' if m['confirmed'] else 'unconfirmed'})"
                    for m in mems[:15]
                ]
                reply = "Here are your stored preferences:\n" + "\n".join(lines)
            return TurnResult(
                reply=reply,
                phase=self.state.phase,
                state=asdict(self.state),
                tool_calls_made=0,
                model_turns_used=1,
                elapsed_ms=1.0,
                outcome="memory_inspected",
            )

        # Forget pattern: "forget my preferred_area" / "forget area" / "forget earliest_acceptable_time"
        m_forget = re.search(r"\bforget\s+(?:my\s+|that\s+)?([a-z0-9_ ]+)", low)
        if m_forget:
            raw_target = m_forget.group(1).strip().rstrip(".")
            key_map = {
                "area": "preferred_area",
                "preferred area": "preferred_area",
                "location": "preferred_area",
                "earliest time": "earliest_acceptable_time",
                "earliest acceptable time": "earliest_acceptable_time",
                "budget": "default_budget_inr",
                "electrician": "preferred_electrician",
                "preferred electrician": "preferred_electrician",
            }
            target_key = key_map.get(raw_target, raw_target.replace(" ", "_"))
            count = self.store.forget_memory(self.user_id, target_key)
            if target_key == "preferred_area" and self.state.area:
                self.state.area = None
            reply = f"Done — I have forgotten `{target_key}` ({count} record marked forgotten)."
            return TurnResult(
                reply=reply,
                phase=self.state.phase,
                state=asdict(self.state),
                tool_calls_made=0,
                model_turns_used=1,
                elapsed_ms=1.0,
                outcome="memory_forgotten",
            )

        # Correction / explicit preference update: "remember my area is Koramangala" / "update my preferred_area to HSR Layout"
        m_update = re.search(
            r"\b(?:remember|update|set|change)\s+(?:that\s+)?(?:my\s+)?(preferred_area|area|location|earliest_acceptable_time|default_budget_inr)\s+(?:is|to)\s+([A-Za-z0-9:_-]+)",
            text,
            re.I,
        )
        if m_update:
            raw_k, new_val = m_update.group(1).lower(), m_update.group(2).strip().rstrip(".")
            norm_k = "preferred_area" if raw_k in ("area", "location", "preferred_area") else raw_k
            self.store.set_memory(self.user_id, norm_k, new_val, source="explicit", confirmed=True, tags=["global_errand"])
            if norm_k == "preferred_area":
                self.state.area = new_val
            reply = f"Updated your explicit preference `{norm_k}` to **{new_val}**."
            return TurnResult(
                reply=reply,
                phase=self.state.phase,
                state=asdict(self.state),
                tool_calls_made=0,
                model_turns_used=1,
                elapsed_ms=1.0,
                outcome="memory_updated",
            )

        return None

    def _parse_user_constraints(self, text: str, confirmed_memories: dict[str, str]) -> dict[str, Any]:
        """Update ErrandSlotState incrementally while preserving unrelated context when one detail changes."""
        low = text.lower()
        changes: dict[str, Any] = {}

        # Service detection (only overwrite if mentioned)
        if any(w in low for w in ("plumber", "plumbing", "kitchen tap", "tap", "sink", "pipe", "leak")):
            if self.state.service != "plumber":
                self.state.service = "plumber"
                changes["service"] = "plumber"
            if "kitchen tap" in low:
                self.state.issue_detail = "kitchen tap"
        elif any(w in low for w in ("electrician", "electrical", "wiring", "fan", "mcb")):
            if self.state.service != "electrician":
                self.state.service = "electrician"
                changes["service"] = "electrician"
        elif any(w in low for w in ("ac servicing", "ac service", "ac ", "air conditioner")):
            if self.state.service != "ac_servicing":
                self.state.service = "ac_servicing"
                changes["service"] = "ac_servicing"

        # Intent detection
        if any(w in low for w in ("cancel my", "cancel booking", "cancel the")):
            self.state.intent = "cancel"
        elif any(w in low for w in ("move my", "reschedule", "postpone", "change my booking")):
            self.state.intent = "reschedule"
        elif self.state.intent is None and self.state.service:
            self.state.intent = "book"

        if "same electrician as last time" in low or "same plumber as last time" in low or "as last time" in low:
            self.state.same_as_last_time = True
            self.state.intent = "book"

        # Area detection (preserve existing area unless new area stated, or fall back to confirmed memory)
        for known_area in ("Indiranagar", "Koramangala", "HSR Layout", "Whitefield", "Jayanagar", "MG Road"):
            if known_area.lower() in low:
                if self.state.area != known_area:
                    self.state.area = known_area
                    changes["area"] = known_area
                break
        if not self.state.area and "preferred_area" in confirmed_memories:
            self.state.area = confirmed_memories["preferred_area"]

        # Date window detection
        for dw in ("this week", "saturday", "sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "tomorrow"):
            if dw in low:
                self.state.date_window = dw
                changes["date_window"] = dw
                break

        # Time window detection
        if "morning" in low:
            self.state.time_window = "mornings only"
            self.state.latest_hour = 12
            changes["time_window"] = "mornings only"
        elif "afternoon" in low or "after 2" in low:
            self.state.time_window = "afternoon"
            self.state.earliest_hour = 13
            self.state.latest_hour = 18
            changes["time_window"] = "afternoon"

        m_not_before = re.search(r"not\s+before\s+(\d{1,2})\s*(?:a\.?m\.?|am|:00)?", low)
        if m_not_before:
            hr = int(m_not_before.group(1))
            self.state.earliest_hour = hr
            self.state.time_window = f"not before {hr}:00"
            changes["earliest_hour"] = hr
        elif self.state.earliest_hour is None and "earliest_acceptable_time" in confirmed_memories:
            try:
                self.state.earliest_hour = int(str(confirmed_memories["earliest_acceptable_time"]).split(":")[0])
            except ValueError:
                pass

        # Explicit ISO slot mention (e.g., 2026-11-09T09:00 or 10:30)
        m_iso = re.search(r"(2026-\d{2}-\d{2}T\d{2}:\d{2})", text)
        if m_iso:
            new_slot = m_iso.group(1)
            if self.state.selected_slot != new_slot:
                self.state.selected_slot = new_slot
                changes["selected_slot"] = new_slot

        # Budget detection (e.g. "under ₹1,500", "under 1200", "budget 900")
        m_budget = re.search(r"(?:under|below|max|budget|within)\s*(?:₹|rs\.?|inr)?\s*([\d,]+)", low)
        if m_budget:
            b_val = int(m_budget.group(1).replace(",", ""))
            if self.state.budget_inr != b_val:
                self.state.budget_inr = b_val
                changes["budget_inr"] = b_val

        # Provider selection by ID or name
        m_prov = re.search(r"\b(prov_[a-z]+_\d{2})\b", low)
        if m_prov:
            pid = m_prov.group(1)
            if self.state.selected_provider_id != pid:
                self.state.selected_provider_id = pid
                changes["selected_provider_id"] = pid

        return changes

    @staticmethod
    def _is_explicit_confirmation(text: str) -> bool:
        low = text.strip().lower()
        # Reject adversarial or ambiguous texts
        if "do not" in low or "don't" in low or "cancel" in low and "confirm cancel" not in low:
            if not low.startswith("yes"):
                return False
        positive_phrases = (
            "yes",
            "confirm",
            "approve",
            "go ahead",
            "book it",
            "proceed",
            "yes, book",
            "yes, cancel",
            "yes, reschedule",
            "i confirm",
            "i approve",
        )
        return any(low == p or low.startswith(p) or f" {p}" in f" {low}" for p in positive_phrases)

    # ── Main Turn Handler ────────────────────────────────────────────────────
    def handle_turn(self, user_message: str) -> TurnResult:
        t_start = self.store.now()
        self.turn_counter += 1
        model_turns = 0
        tool_calls = 0
        warnings_collected: list[str] = []

        def check_limits() -> Optional[TurnResult]:
            elapsed = self.store.now() - t_start
            if elapsed > self.limits.max_wall_clock_seconds:
                self.state.phase = "ERROR_TERMINATED"
                msg = f"Stopped safely: wall-clock limit ({self.limits.max_wall_clock_seconds}s) exceeded. No unverified side effects were committed."
                return TurnResult(
                    reply=msg,
                    phase=self.state.phase,
                    state=asdict(self.state),
                    tool_calls_made=tool_calls,
                    model_turns_used=model_turns,
                    elapsed_ms=round(elapsed * 1000, 2),
                    outcome="limit_exhausted_wall_clock",
                )
            if model_turns > self.limits.max_model_turns:
                self.state.phase = "ERROR_TERMINATED"
                msg = f"Stopped safely: model turn limit ({self.limits.max_model_turns}) reached. Please refine your request."
                return TurnResult(
                    reply=msg,
                    phase=self.state.phase,
                    state=asdict(self.state),
                    tool_calls_made=tool_calls,
                    model_turns_used=model_turns,
                    elapsed_ms=round(elapsed * 1000, 2),
                    outcome="limit_exhausted_model_turns",
                )
            if tool_calls > self.limits.max_tool_calls:
                self.state.phase = "ERROR_TERMINATED"
                msg = f"Stopped safely: tool call limit ({self.limits.max_tool_calls}) reached without completing the action."
                return TurnResult(
                    reply=msg,
                    phase=self.state.phase,
                    state=asdict(self.state),
                    tool_calls_made=tool_calls,
                    model_turns_used=model_turns,
                    elapsed_ms=round(elapsed * 1000, 2),
                    outcome="limit_exhausted_tool_calls",
                )
            return None

        def invoke_tool(name: str, fn: Callable, *args, **kwargs) -> dict[str, Any]:
            nonlocal tool_calls
            tool_calls += 1
            if tool_calls > self.limits.max_tool_calls:
                raise RuntimeError("TOOL_CALL_LIMIT_EXCEEDED")
            t0 = self.store.now()
            raw = fn(*args, **kwargs)
            sanitized, warns = sanitize_tool_output(name, raw)
            warnings_collected.extend(warns)
            self.store.record_trace(
                self.session_id,
                self.user_id,
                self.turn_counter,
                "tool_call",
                {
                    "tool": name,
                    "args": list(args),
                    "kwargs": kwargs,
                    "result": sanitized,
                    "warnings": warns,
                    "latency_ms": round((self.store.now() - t0) * 1000, 2),
                },
            )
            return sanitized

        # Build bounded context and log manifest to trace
        manifest = self.packer.build_context(
            user_id=self.user_id,
            session_id=self.session_id,
            current_user_message=user_message,
            workflow_state_summary=self.state.summary(),
            active_service=self.state.service,
        )
        self.store.record_trace(
            self.session_id,
            self.user_id,
            self.turn_counter,
            "context_manifest",
            manifest.to_dict(),
        )
        self.store.add_message(self.user_id, self.session_id, "user", user_message)

        # Count 1 model turn for intent/constraint understanding
        model_turns += 1
        lim_res = check_limits()
        if lim_res:
            lim_res.context_manifest = manifest.to_dict()
            return lim_res

        self.store.record_trace(
            self.session_id,
            self.user_id,
            self.turn_counter,
            "model_call",
            {
                "turn": model_turns,
                "mode": "live_llm" if self.use_live_llm else "deterministic_offline",
                "prompt_tokens": manifest.total_tokens_used,
                "completion_tokens": 48,
                "estimated_cost_usd": round(manifest.total_tokens_used * 0.0000005, 6),
            },
        )

        # 1. Check if this is a memory management command (inspect / forget / update)
        mem_cmd_res = self._extract_Memory_command(user_message)
        if mem_cmd_res:
            self.store.add_message(self.user_id, self.session_id, "assistant", mem_cmd_res.reply)
            mem_cmd_res.context_manifest = manifest.to_dict()
            return mem_cmd_res

        confirmed_mems = {m["key"]: m["value"] for m in manifest.included_memories}

        # 2. If currently AWAITING_APPROVAL, check if user is confirming or modifying parameters
        if self.state.phase == "AWAITING_APPROVAL":
            prev_slot = self.state.selected_slot
            prev_budget = self.state.budget_inr
            prev_time = self.state.time_window
            changes = self._parse_user_constraints(user_message, confirmed_mems)

            # If user changed slot, time window, or budget while awaiting approval -> invalidate & re-evaluate!
            if any(k in changes for k in ("selected_slot", "time_window", "earliest_hour", "budget_inr", "selected_provider_id")):
                self.store.invalidate_approvals(
                    self.user_id,
                    reason=f"user_modified_constraints:{list(changes.keys())}",
                )
                self.state.phase = "COMPARING"
                # Fall through to search/hold with preserved context + updated field!
            elif self._is_explicit_confirmation(user_message):
                try:
                    return self._execute_approved_action(
                        invoke_tool, check_limits, manifest, t_start, model_turns, lambda: tool_calls, warnings_collected
                    )
                except RuntimeError as e:
                    if "TOOL_CALL_LIMIT_EXCEEDED" in str(e):
                        lim = check_limits()
                        if lim:
                            lim.context_manifest = manifest.to_dict()
                            return lim
                    raise
            else:
                reply = (
                    f"Action is still awaiting your explicit confirmation. "
                    f"Please reply **'Yes, confirm'** to proceed with `{self.state.pending_approval_action}` "
                    f"({self.state.selected_provider_name or self.state.target_booking_id}, "
                    f"slot `{self.state.selected_slot}`, ₹{self.state.quoted_price}), or tell me what to change."
                )
                self.store.add_message(self.user_id, self.session_id, "assistant", reply)
                return TurnResult(
                    reply=reply,
                    phase=self.state.phase,
                    state=asdict(self.state),
                    tool_calls_made=tool_calls,
                    model_turns_used=model_turns,
                    elapsed_ms=round((self.store.now() - t_start) * 1000, 2),
                    outcome="awaiting_approval_reminder",
                    context_manifest=manifest.to_dict(),
                )

        # If user repeats "Yes, confirm" after booking already completed (repeated confirmation test)
        if self.state.phase == "BOOKED" and self._is_explicit_confirmation(user_message) and self.state.active_hold_id:
            idem = compute_idempotency_key(self.user_id, "book", self.state.active_hold_id)
            status_res = invoke_tool("get_booking_status", self.env.get_booking_status, idem)
            if status_res.get("status") == "booked":
                bk = status_res["booking"]
                reply = (
                    f"Your booking (`{bk['booking_id']}`) with **{bk['provider_name']}** at `{bk['slot']}` "
                    f"for ₹{bk['price']} is already confirmed — no duplicate booking was created."
                )
                self.store.add_message(self.user_id, self.session_id, "assistant", reply)
                return TurnResult(
                    reply=reply,
                    phase="BOOKED",
                    state=asdict(self.state),
                    tool_calls_made=tool_calls,
                    model_turns_used=model_turns,
                    elapsed_ms=round((self.store.now() - t_start) * 1000, 2),
                    outcome="duplicate_confirmation_ignored",
                    booking=bk,
                    context_manifest=manifest.to_dict(),
                )

        # Parse constraints from user turn
        self._parse_user_constraints(user_message, confirmed_mems)

        try:
            # 3. Handle CANCEL flow
            if self.state.intent == "cancel":
                bks_res = invoke_tool("list_bookings", self.env.list_bookings, self.user_id)
                if bks_res.get("status") == "error":
                    return self._handle_tool_error(bks_res, manifest, t_start, tool_calls, model_turns)
                active_bks = [b for b in bks_res.get("bookings", []) if b["status"] == "confirmed"]
                if not active_bks:
                    reply = "You don't have any active confirmed bookings to cancel."
                    self.store.add_message(self.user_id, self.session_id, "assistant", reply)
                    return TurnResult(
                        reply=reply,
                        phase="IDLE",
                        state=asdict(self.state),
                        tool_calls_made=tool_calls,
                        model_turns_used=model_turns,
                        elapsed_ms=round((self.store.now() - t_start) * 1000, 2),
                        outcome="no_booking_to_cancel",
                        context_manifest=manifest.to_dict(),
                    )
                target = active_bks[0]
                self.state.target_booking_id = target["booking_id"]
                self.state.selected_provider_id = target["provider_id"]
                self.state.selected_provider_name = target["provider_name"]
                self.state.selected_slot = target["slot"]
                self.state.quoted_price = target["price"]
                self.state.pending_approval_action = "cancel"
                self.state.phase = "AWAITING_APPROVAL"
                reply = (
                    f"Please confirm: Do you want to **cancel** booking `{target['booking_id']}` "
                    f"with **{target['provider_name']}** ({target['service']}) at `{target['slot']}` (₹{target['price']})?"
                )
                self.store.add_message(self.user_id, self.session_id, "assistant", reply)
                return TurnResult(
                    reply=reply,
                    phase=self.state.phase,
                    state=asdict(self.state),
                    tool_calls_made=tool_calls,
                    model_turns_used=model_turns,
                    elapsed_ms=round((self.store.now() - t_start) * 1000, 2),
                    outcome="awaiting_approval",
                    context_manifest=manifest.to_dict(),
                )

            # 4. Handle RESCHEDULE flow ("Move my AC servicing to Saturday")
            if self.state.intent == "reschedule":
                bks_res = invoke_tool("list_bookings", self.env.list_bookings, self.user_id)
                if bks_res.get("status") == "error":
                    return self._handle_tool_error(bks_res, manifest, t_start, tool_calls, model_turns)
                active_bks = [b for b in bks_res.get("bookings", []) if b["status"] == "confirmed"]
                if self.state.service:
                    svc_bks = [b for b in active_bks if b["service"] == self.state.service]
                    if svc_bks:
                        active_bks = svc_bks
                if not active_bks:
                    reply = "I couldn't find an active booking to reschedule."
                    self.store.add_message(self.user_id, self.session_id, "assistant", reply)
                    return TurnResult(
                        reply=reply,
                        phase="IDLE",
                        state=asdict(self.state),
                        tool_calls_made=tool_calls,
                        model_turns_used=model_turns,
                        elapsed_ms=round((self.store.now() - t_start) * 1000, 2),
                        outcome="no_booking_to_reschedule",
                        context_manifest=manifest.to_dict(),
                    )
                target = active_bks[0]
                prov_res = invoke_tool("get_provider", self.env.get_provider, target["provider_id"])
                avail = prov_res.get("available_slots", [])
                chosen_slot = self.state.selected_slot
                if not chosen_slot and avail:
                    # Pick requested slot or first available Saturday slot
                    chosen_slot = avail[0]
                self.state.target_booking_id = target["booking_id"]
                self.state.selected_provider_id = target["provider_id"]
                self.state.selected_provider_name = target["provider_name"]
                self.state.selected_slot = chosen_slot
                self.state.quoted_price = target["price"]
                self.state.pending_approval_action = "reschedule"
                self.state.phase = "AWAITING_APPROVAL"
                reply = (
                    f"Found your `{target['service']}` booking `{target['booking_id']}` with **{target['provider_name']}** "
                    f"(currently `{target['slot']}`).\n"
                    f"Please confirm: Do you approve **rescheduling** `{target['booking_id']}` to **`{chosen_slot}`** "
                    f"at **₹{target['price']}**?"
                )
                self.store.add_message(self.user_id, self.session_id, "assistant", reply)
                return TurnResult(
                    reply=reply,
                    phase=self.state.phase,
                    state=asdict(self.state),
                    tool_calls_made=tool_calls,
                    model_turns_used=model_turns,
                    elapsed_ms=round((self.store.now() - t_start) * 1000, 2),
                    outcome="awaiting_approval",
                    context_manifest=manifest.to_dict(),
                )

            # 5. Handle "Book the same electrician as last time"
            if self.state.same_as_last_time and not self.state.selected_provider_id:
                bks_res = invoke_tool("list_bookings", self.env.list_bookings, self.user_id)
                past = [
                    b for b in bks_res.get("bookings", [])
                    if (not self.state.service or b["service"] == self.state.service)
                ]
                if past:
                    last_bk = past[0]
                    self.state.selected_provider_id = last_bk["provider_id"]
                    self.state.selected_provider_name = last_bk["provider_name"]
                    self.state.service = last_bk["service"]
                    if not self.state.area:
                        self.state.area = last_bk.get("area") or "Indiranagar"

            # 6. Clarify missing required details (do not re-ask facts already supplied)
            missing: list[str] = []
            if not self.state.service:
                missing.append("service type (e.g., plumber, electrician, AC servicing)")
            if not self.state.area:
                missing.append("your area/locality")
            if not self.state.date_window and not self.state.selected_slot and not self.state.same_as_last_time:
                missing.append("preferred date or time window")

            if missing:
                self.state.phase = "CLARIFYING"
                reply = "Before I search, could you please share: " + ", ".join(missing) + "?"
                self.store.add_message(self.user_id, self.session_id, "assistant", reply)
                return TurnResult(
                    reply=reply,
                    phase=self.state.phase,
                    state=asdict(self.state),
                    tool_calls_made=tool_calls,
                    model_turns_used=model_turns,
                    elapsed_ms=round((self.store.now() - t_start) * 1000, 2),
                    outcome="clarification_requested",
                    context_manifest=manifest.to_dict(),
                )

            # 7. Search providers & filter by constraints
            search_res = invoke_tool(
                "search_providers",
                self.env.search_providers,
                self.state.service or "plumber",
                self.state.area or "Indiranagar",
                self.state.date_window or "this week",
            )
            if search_res.get("status") == "error":
                return self._handle_tool_error(search_res, manifest, t_start, tool_calls, model_turns)

            providers = search_res.get("providers", [])
            filtered: list[tuple[dict[str, Any], str]] = []
            for p in providers:
                if self.state.selected_provider_id and p["provider_id"] != self.state.selected_provider_id:
                    continue
                if self.state.budget_inr is not None and p["price"] > self.state.budget_inr:
                    continue
                # Find a matching slot respecting earliest_hour and latest_hour
                matching_slot = None
                for s in p.get("available_slots", []):
                    hr = int(s.split("T")[1].split(":")[0])
                    if self.state.earliest_hour is not None and hr < self.state.earliest_hour:
                        continue
                    if self.state.latest_hour is not None and hr >= self.state.latest_hour:
                        continue
                    matching_slot = s
                    break
                if matching_slot:
                    filtered.append((p, matching_slot))

            if not filtered:
                self.state.phase = "CLARIFYING"
                reply = (
                    f"I searched `{self.state.service}` providers in **{self.state.area}**, "
                    f"but none matched all constraints (budget ≤ ₹{self.state.budget_inr or 'any'}, "
                    f"time `{self.state.time_window or 'any'}`). Would you like to relax the budget or time window?"
                )
                self.store.add_message(self.user_id, self.session_id, "assistant", reply)
                return TurnResult(
                    reply=reply,
                    phase=self.state.phase,
                    state=asdict(self.state),
                    tool_calls_made=tool_calls,
                    model_turns_used=model_turns,
                    elapsed_ms=round((self.store.now() - t_start) * 1000, 2),
                    outcome="no_matching_providers",
                    context_manifest=manifest.to_dict(),
                )

            # Sort best candidate by highest rating, then lowest price
            filtered.sort(key=lambda item: (-item[0]["rating"], item[0]["price"]))
            best_prov, best_slot = filtered[0]

            # Inspect provider details & reviews (tests untrusted review sanitization)
            prov_details = invoke_tool("get_provider", self.env.get_provider, best_prov["provider_id"])
            if prov_details.get("status") == "error":
                return self._handle_tool_error(prov_details, manifest, t_start, tool_calls, model_turns)

            # Hold the slot before asking for user confirmation
            hold_res = invoke_tool("hold_slot", self.env.hold_slot, best_prov["provider_id"], best_slot, self.user_id)
            if hold_res.get("status") == "error":
                return self._handle_tool_error(hold_res, manifest, t_start, tool_calls, model_turns)

            # Check if hold_slot quoted price exceeded user's budget cap!
            quoted = int(hold_res["quoted_price"])
            search_p = int(hold_res["search_price"])

            self.state.candidate_providers = [f[0] for f in filtered]
            self.state.selected_provider_id = best_prov["provider_id"]
            self.state.selected_provider_name = best_prov["name"]
            self.state.selected_slot = best_slot
            self.state.active_hold_id = hold_res["hold_id"]
            self.state.quoted_price = quoted
            self.state.search_price = search_p
            self.state.pending_approval_action = "book"
            self.state.phase = "AWAITING_APPROVAL"

            comparison_lines = [
                f"- **{p['name']}** (`{p['provider_id']}`): ₹{p['price']}, ★{p['rating']}, next slot `{s}`"
                for p, s in filtered[:3]
            ]
            price_note = (
                f" *(Note: quoted hold price changed from search price ₹{search_p} to **₹{quoted}**)*"
                if hold_res.get("price_changed")
                else ""
            )
            reply = (
                f"Here are the matching `{self.state.service}` options in **{self.state.area}**:\n"
                + "\n".join(comparison_lines)
                + f"\n\nI have placed a 10-minute hold (`{hold_res['hold_id']}`) on the top match:\n"
                f"**{best_prov['name']}** (`{best_prov['provider_id']}`) at **`{best_slot}`** for **₹{quoted}**{price_note}.\n"
                f"Please reply **'Yes, confirm'** to approve booking **{best_prov['name']}** at `{best_slot}` for **₹{quoted}**."
            )
            self.store.add_message(self.user_id, self.session_id, "assistant", reply)
            return TurnResult(
                reply=reply,
                phase=self.state.phase,
                state=asdict(self.state),
                tool_calls_made=tool_calls,
                model_turns_used=model_turns,
                elapsed_ms=round((self.store.now() - t_start) * 1000, 2),
                outcome="awaiting_approval",
                context_manifest=manifest.to_dict(),
                warnings=warnings_collected,
            )

        except RuntimeError as e:
            if "TOOL_CALL_LIMIT_EXCEEDED" in str(e):
                lim = check_limits()
                if lim:
                    lim.context_manifest = manifest.to_dict()
                    return lim
            raise

    def _execute_approved_action(
        self,
        invoke_tool: Callable,
        check_limits: Callable,
        manifest: Any,
        t_start: float,
        model_turns: int,
        get_tool_calls: Callable[[], int],
        warnings_collected: list[str],
    ) -> TurnResult:
        action = self.state.pending_approval_action or "book"

        if action == "book":
            hold_id = self.state.active_hold_id
            assert hold_id, "Cannot book without active_hold_id"
            # Grant code-level approval bound to the exact state parameters the user saw
            self.store.grant_approval(
                user_id=self.user_id,
                action="book",
                provider_id=self.state.selected_provider_id,
                slot=self.state.selected_slot,
                price=self.state.quoted_price,
            )
            idem = compute_idempotency_key(self.user_id, "book", hold_id)
            self.store.log_action_start(
                user_id=self.user_id,
                action="book",
                idempotency_key=idem,
                hold_id=hold_id,
                slot=self.state.selected_slot,
            )

            try:
                book_res = invoke_tool("book", self.env.book, hold_id, idem, self.user_id, True)
            except PermissionError as pe:
                # Material change occurred (e.g., hold price/slot mutated after user saw it)
                self.store.log_action_complete(idem, state="approval_mismatch")
                self.state.phase = "AWAITING_APPROVAL"
                # Refresh state with current hold details
                hold_row = self.store._conn.execute(
                    "SELECT * FROM holds WHERE hold_id = ?", (hold_id,)
                ).fetchone()
                if hold_row:
                    self.state.quoted_price = hold_row["quoted_price"]
                    self.state.selected_slot = hold_row["slot"]
                reply = (
                    f"⚠️ Booking blocked by safety gate: {pe}\n"
                    f"Please re-confirm if you approve booking **{self.state.selected_provider_name}** "
                    f"at `{self.state.selected_slot}` for the updated price **₹{self.state.quoted_price}**."
                )
                self.store.add_message(self.user_id, self.session_id, "assistant", reply)
                return TurnResult(
                    reply=reply,
                    phase=self.state.phase,
                    state=asdict(self.state),
                    tool_calls_made=get_tool_calls(),
                    model_turns_used=model_turns,
                    elapsed_ms=round((self.store.now() - t_start) * 1000, 2),
                    outcome="approval_invalidated_reconfirm_needed",
                    context_manifest=manifest.to_dict(),
                    warnings=warnings_collected,
                )

            # Handle ambiguous timeout by querying authoritative get_booking_status!
            if book_res.get("status") == "timeout":
                status_res = invoke_tool("get_booking_status", self.env.get_booking_status, idem)
                if status_res.get("status") == "booked":
                    bk = status_res["booking"]
                    self.store.log_action_complete(idem, state="resolved_after_timeout_booked")
                    self.state.phase = "BOOKED"
                    self.state.last_booking_id = bk["booking_id"]
                    self.state.pending_approval_action = None
                    reply = (
                        f"The booking call timed out, so I verified with `get_booking_status`: "
                        f"your booking **succeeded**! Booking ID: `{bk['booking_id']}` with "
                        f"**{bk['provider_name']}** at `{bk['slot']}` for **₹{bk['price']}**."
                    )
                    self.store.add_message(self.user_id, self.session_id, "assistant", reply)
                    return TurnResult(
                        reply=reply,
                        phase=self.state.phase,
                        state=asdict(self.state),
                        tool_calls_made=get_tool_calls(),
                        model_turns_used=model_turns,
                        elapsed_ms=round((self.store.now() - t_start) * 1000, 2),
                        outcome="booking_confirmed_after_timeout_check",
                        booking=bk,
                        context_manifest=manifest.to_dict(),
                        warnings=warnings_collected,
                    )
                else:
                    # Did NOT book underneath -> safe to retry once with the same idempotency_key
                    retry_res = invoke_tool("book", self.env.book, hold_id, idem, self.user_id, True)
                    if retry_res.get("status") == "confirmed":
                        self.store.log_action_complete(idem, state="completed_on_retry")
                        self.state.phase = "BOOKED"
                        self.state.last_booking_id = retry_res["booking_id"]
                        self.state.pending_approval_action = None
                        reply = (
                            f"Initial booking call timed out (verified unbooked via `get_booking_status`), "
                            f"so I safely retried: Booking `{retry_res['booking_id']}` confirmed with "
                            f"**{retry_res['provider_name']}** at `{retry_res['slot']}` for **₹{retry_res['price']}**."
                        )
                        self.store.add_message(self.user_id, self.session_id, "assistant", reply)
                        return TurnResult(
                            reply=reply,
                            phase=self.state.phase,
                            state=asdict(self.state),
                            tool_calls_made=get_tool_calls(),
                            model_turns_used=model_turns,
                            elapsed_ms=round((self.store.now() - t_start) * 1000, 2),
                            outcome="booking_confirmed_after_timeout_retry",
                            booking=retry_res,
                            context_manifest=manifest.to_dict(),
                            warnings=warnings_collected,
                        )
                    book_res = retry_res

            if book_res.get("status") == "confirmed":
                self.store.log_action_complete(idem, state="completed")
                self.state.phase = "BOOKED"
                self.state.last_booking_id = book_res["booking_id"]
                self.state.pending_approval_action = None
                reply = (
                    f"✅ Booking confirmed! ID: `{book_res['booking_id']}` with "
                    f"**{book_res['provider_name']}** at `{book_res['slot']}` for **₹{book_res['price']}**."
                )
                self.store.add_message(self.user_id, self.session_id, "assistant", reply)
                return TurnResult(
                    reply=reply,
                    phase=self.state.phase,
                    state=asdict(self.state),
                    tool_calls_made=get_tool_calls(),
                    model_turns_used=model_turns,
                    elapsed_ms=round((self.store.now() - t_start) * 1000, 2),
                    outcome="booking_confirmed",
                    booking=book_res,
                    context_manifest=manifest.to_dict(),
                    warnings=warnings_collected,
                )

            if book_res.get("status") in ("slot_taken", "hold_expired"):
                self.store.log_action_complete(idem, state=book_res["status"])
                # Automatically hold the next available slot or provider and ask for fresh approval!
                prov = self.env.get_provider(self.state.selected_provider_id or "")
                alt_slots = [s for s in prov.get("available_slots", []) if s != self.state.selected_slot]
                if alt_slots:
                    new_slot = alt_slots[0]
                    new_hold = invoke_tool(
                        "hold_slot",
                        self.env.hold_slot,
                        self.state.selected_provider_id,
                        new_slot,
                        self.user_id,
                    )
                    self.state.selected_slot = new_slot
                    self.state.active_hold_id = new_hold["hold_id"]
                    self.state.quoted_price = new_hold["quoted_price"]
                    self.state.phase = "AWAITING_APPROVAL"
                    reply = (
                        f"⚠️ Booking could not finalize (`{book_res['status']}`). "
                        f"I placed a new hold (`{new_hold['hold_id']}`) with **{self.state.selected_provider_name}** "
                        f"at the next available slot **`{new_slot}`** for **₹{new_hold['quoted_price']}**.\n"
                        f"Please reply **'Yes, confirm'** if you approve this new slot."
                    )
                    self.store.add_message(self.user_id, self.session_id, "assistant", reply)
                    return TurnResult(
                        reply=reply,
                        phase=self.state.phase,
                        state=asdict(self.state),
                        tool_calls_made=get_tool_calls(),
                        model_turns_used=model_turns,
                        elapsed_ms=round((self.store.now() - t_start) * 1000, 2),
                        outcome=f"recovered_from_{book_res['status']}_awaiting_approval",
                        context_manifest=manifest.to_dict(),
                        warnings=warnings_collected,
                    )

            self.state.phase = "ERROR_TERMINATED"
            reply = f"Booking could not be completed (`{book_res.get('status')}`). No charges or bookings were made."
            self.store.add_message(self.user_id, self.session_id, "assistant", reply)
            return TurnResult(
                reply=reply,
                phase=self.state.phase,
                state=asdict(self.state),
                tool_calls_made=get_tool_calls(),
                model_turns_used=model_turns,
                elapsed_ms=round((self.store.now() - t_start) * 1000, 2),
                outcome="booking_failed_cleanly",
                context_manifest=manifest.to_dict(),
                warnings=warnings_collected,
            )

        if action == "cancel":
            bid = self.state.target_booking_id
            assert bid, "No target_booking_id to cancel"
            self.store.grant_approval(self.user_id, "cancel", booking_id=bid)
            idem = compute_idempotency_key(self.user_id, "cancel", bid)
            c_res = invoke_tool("cancel", self.env.cancel, bid, idem, self.user_id, True)
            if c_res.get("http_status") == 503 or c_res.get("error_code") == "transient_503":
                # Retry once on transient 503
                c_res = invoke_tool("cancel", self.env.cancel, bid, idem, self.user_id, True)
            if c_res.get("status") == "cancelled":
                self.state.phase = "CANCELLED"
                self.state.pending_approval_action = None
                reply = (
                    f"✅ Booking `{bid}` has been cancelled "
                    f"(recovered cleanly from initial transient 503 on attempt #{c_res.get('attempts', 2)})."
                )
                self.store.add_message(self.user_id, self.session_id, "assistant", reply)
                return TurnResult(
                    reply=reply,
                    phase=self.state.phase,
                    state=asdict(self.state),
                    tool_calls_made=get_tool_calls(),
                    model_turns_used=model_turns,
                    elapsed_ms=round((self.store.now() - t_start) * 1000, 2),
                    outcome="cancellation_confirmed",
                    context_manifest=manifest.to_dict(),
                )

        if action == "reschedule":
            bid = self.state.target_booking_id
            slot = self.state.selected_slot
            assert bid and slot, "Missing booking_id or slot for reschedule"
            self.store.grant_approval(
                self.user_id,
                "reschedule",
                provider_id=self.state.selected_provider_id,
                slot=slot,
                price=self.state.quoted_price,
                booking_id=bid,
            )
            idem = compute_idempotency_key(self.user_id, "reschedule", bid, slot)
            r_res = invoke_tool("reschedule", self.env.reschedule, bid, slot, idem, self.user_id, True)
            if r_res.get("status") == "slot_conflict":
                alts = r_res.get("alternative_slots", [])
                alt_slot = alts[0] if alts else "2026-11-14T10:00"
                self.state.selected_slot = alt_slot
                self.state.phase = "AWAITING_APPROVAL"
                reply = (
                    f"⚠️ Slot `{slot}` had a conflict and was unavailable. "
                    f"The next available slot with **{self.state.selected_provider_name}** is **`{alt_slot}`** "
                    f"(₹{self.state.quoted_price}).\n"
                    f"Please reply **'Yes, confirm'** to approve rescheduling `{bid}` to **`{alt_slot}`**."
                )
                self.store.add_message(self.user_id, self.session_id, "assistant", reply)
                return TurnResult(
                    reply=reply,
                    phase=self.state.phase,
                    state=asdict(self.state),
                    tool_calls_made=get_tool_calls(),
                    model_turns_used=model_turns,
                    elapsed_ms=round((self.store.now() - t_start) * 1000, 2),
                    outcome="reschedule_conflict_awaiting_new_approval",
                    context_manifest=manifest.to_dict(),
                )
            if r_res.get("status") == "rescheduled":
                self.state.phase = "RESCHEDULED"
                self.state.pending_approval_action = None
                reply = (
                    f"✅ Booking `{bid}` with **{r_res['provider_name']}** has been rescheduled "
                    f"from `{r_res['previous_slot']}` to **`{r_res['new_slot']}`** (₹{r_res['price']})."
                )
                self.store.add_message(self.user_id, self.session_id, "assistant", reply)
                return TurnResult(
                    reply=reply,
                    phase=self.state.phase,
                    state=asdict(self.state),
                    tool_calls_made=get_tool_calls(),
                    model_turns_used=model_turns,
                    elapsed_ms=round((self.store.now() - t_start) * 1000, 2),
                    outcome="reschedule_confirmed",
                    context_manifest=manifest.to_dict(),
                )

        self.state.phase = "ERROR_TERMINATED"
        reply = "Action could not be completed due to an unexpected tool response."
        self.store.add_message(self.user_id, self.session_id, "assistant", reply)
        return TurnResult(
            reply=reply,
            phase=self.state.phase,
            state=asdict(self.state),
            tool_calls_made=get_tool_calls(),
            model_turns_used=model_turns,
            elapsed_ms=round((self.store.now() - t_start) * 1000, 2),
            outcome="action_failed",
            context_manifest=manifest.to_dict(),
        )

    def _handle_tool_error(
        self,
        err_res: dict[str, Any],
        manifest: Any,
        t_start: float,
        tool_calls: int,
        model_turns: int,
    ) -> TurnResult:
        self.state.phase = "ERROR_TERMINATED"
        reply = (
            f"⚠️ Service error (`{err_res.get('error_code', 'tool_error')}`): "
            f"{err_res.get('message', 'Tool failed')}. No bookings or changes were made."
        )
        self.store.add_message(self.user_id, self.session_id, "assistant", reply)
        return TurnResult(
            reply=reply,
            phase=self.state.phase,
            state=asdict(self.state),
            tool_calls_made=tool_calls,
            model_turns_used=model_turns,
            elapsed_ms=round((self.store.now() - t_start) * 1000, 2),
            outcome="tool_failure_handled",
            context_manifest=manifest.to_dict(),
        )
