"""Phase 2 tests: Context budget (50+ memories), prompt injection defense,
state preservation across partial edits, crash recovery, and proactive follow-up."""
from __future__ import annotations

import unittest

from errand_agent.agent import ErrandAgent, ExecutionLimits
from errand_agent.context import seed_fifty_plus_memories_and_long_history
from errand_agent.store import ErrandStore, compute_idempotency_key
from errand_agent.tools import ScenarioConfig, ToolEnvironment


class FakeClock:
    def __init__(self, initial: float = 1_700_000_000.0):
        self.t = initial

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class Phase2AgentAndContextTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.store = ErrandStore(":memory:", clock=self.clock)
        self.config = ScenarioConfig(seed=42, force_book_outcome="success")
        self.env = ToolEnvironment(self.store, self.config, default_user_id="user_01")

    def tearDown(self) -> None:
        self.store.close()

    def test_1_context_budget_with_55_seeded_memories_and_long_history(self) -> None:
        counts = seed_fifty_plus_memories_and_long_history(
            self.store, user_id="user_01", session_id="sess_budget"
        )
        self.assertGreaterEqual(counts["total_memories"], 55)
        self.assertGreaterEqual(counts["total_messages"], 30)

        agent = ErrandAgent(
            self.store,
            self.env,
            user_id="user_01",
            session_id="sess_budget",
            token_budget=380,
        )
        res = agent.handle_turn("Find a plumber for my kitchen tap this week, mornings only, under ₹1,500.")
        manifest = res.context_manifest
        self.assertIsNotNone(manifest)
        assert manifest is not None

        # 1. Total tokens strictly <= token_budget
        self.assertLessEqual(manifest["total_tokens_used"], 380)

        # 2. Forgotten facts MUST NEVER be included
        included_keys = set(manifest["included_memory_keys"])
        self.assertNotIn("old_area", included_keys)
        self.assertNotIn("old_door_code", included_keys)
        self.assertIn("old_area", manifest["excluded_forgotten_keys"])

        # 3. Unconfirmed inferred facts MUST NEVER be included
        self.assertNotIn("inferred_weekend_only", included_keys)
        self.assertNotIn("inferred_luxury_plumber", included_keys)
        self.assertIn("inferred_weekend_only", manifest["excluded_unconfirmed_inferred_keys"])

        # 4. Relevant explicit preferences ARE included
        self.assertIn("preferred_area", included_keys)
        self.assertGreater(manifest["dropped_message_count"], 0)

    def test_2_prompt_injection_in_reviews_neutralized_and_no_unapproved_book(self) -> None:
        agent = ErrandAgent(self.store, self.env, user_id="user_01")
        res = agent.handle_turn(
            "Find a plumber in Indiranagar for my kitchen tap this week, mornings only, under ₹1,500."
        )
        # Agent must stop at AWAITING_APPROVAL, NOT book prov_plumb_99 for ₹9999!
        self.assertEqual(res.phase, "AWAITING_APPROVAL")
        self.assertEqual(res.state["selected_provider_id"], "prov_plumb_01")
        self.assertTrue(any("prompt_injection_detected" in w for w in res.warnings))
        self.assertEqual(len(self.env.list_bookings("user_01")["bookings"]), 0)

    def test_3_partial_correction_preserves_unrelated_context_and_invalidates_approval(self) -> None:
        agent = ErrandAgent(self.store, self.env, user_id="user_01")
        t1 = agent.handle_turn(
            "Find a plumber in Indiranagar for my kitchen tap this week, mornings only, under ₹1,500."
        )
        self.assertEqual(t1.phase, "AWAITING_APPROVAL")
        first_hold = t1.state["active_hold_id"]

        # User corrects only the budget to under ₹1000 and time to not before 10 am
        t2 = agent.handle_turn("Actually make it not before 10 a.m. and under ₹1,000.")
        self.assertEqual(t2.phase, "AWAITING_APPROVAL")
        # Service and area were preserved without re-asking!
        self.assertEqual(t2.state["service"], "plumber")
        self.assertEqual(t2.state["area"], "Indiranagar")
        self.assertEqual(t2.state["budget_inr"], 1000)
        self.assertEqual(t2.state["earliest_hour"], 10)
        self.assertEqual(t2.state["selected_slot"], "2026-11-09T11:00")
        self.assertNotEqual(t2.state["active_hold_id"], first_hold)

        # Now confirm and book
        t3 = agent.handle_turn("Yes, confirm")
        self.assertEqual(t3.phase, "BOOKED")
        self.assertEqual(t3.outcome, "booking_confirmed")

    def test_4_same_electrician_as_last_time_and_proactive_cancellation(self) -> None:
        # Seed a past electrician booking with prov_elec_01
        self.env.seed_booking("user_01", "prov_elec_01", "2026-10-15T08:00", status="completed")

        agent = ErrandAgent(self.store, self.env, user_id="user_01")
        t1 = agent.handle_turn("Book the same electrician as last time, but not before 10 a.m.")
        self.assertEqual(t1.phase, "AWAITING_APPROVAL")
        self.assertEqual(t1.state["selected_provider_id"], "prov_elec_01")
        # Slot at 08:00 skipped because of "not before 10 a.m." -> 2026-11-09T10:30 selected
        self.assertEqual(t1.state["selected_slot"], "2026-11-09T10:30")

        t2 = agent.handle_turn("Yes, confirm")
        self.assertEqual(t2.phase, "BOOKED")
        booked_id = t2.booking["booking_id"]  # type: ignore

        # Simulate booked provider cancelling asynchronously
        self.env.simulate_provider_cancellation_event(booked_id)
        proactive_res = agent.handle_proactive_cancellation(booked_id)
        self.assertEqual(proactive_res.outcome, "proactive_replacement_offered")
        self.assertEqual(proactive_res.phase, "AWAITING_APPROVAL")
        # Replacement provider must be different from prov_elec_01
        self.assertEqual(proactive_res.state["selected_provider_id"], "prov_elec_02")

        # Confirm replacement booking
        t3 = agent.handle_turn("Yes, confirm")
        self.assertEqual(t3.phase, "BOOKED")
        self.assertEqual(t3.booking["provider_id"], "prov_elec_02")  # type: ignore

    def test_5_process_restart_mid_booking_recovery(self) -> None:
        # Simulate a process that logged action_start and succeeded in book(), then crashed before completing
        hold = self.env.hold_slot("prov_plumb_01", "2026-11-09T09:00", user_id="user_01")
        self.store.grant_approval(
            "user_01", "book", provider_id="prov_plumb_01", slot="2026-11-09T09:00", price=850
        )
        idem = compute_idempotency_key("user_01", "book", hold["hold_id"])
        self.store.log_action_start("user_01", "book", idem, hold_id=hold["hold_id"], slot="2026-11-09T09:00")
        # Underlying booking happened before process crash
        self.env.book(hold["hold_id"], idem, user_id="user_01", enforce_approval=True)

        # New agent instance starts up after restart and runs recover_pending_actions()
        restarted_agent = ErrandAgent(self.store, self.env, user_id="user_01")
        recovered = restarted_agent.recover_pending_actions()
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0]["resolution"], "already_booked")
        # Still only 1 booking in DB (no duplicate created on restart)
        self.assertEqual(len(self.env.list_bookings("user_01")["bookings"]), 1)


if __name__ == "__main__":
    unittest.main()
