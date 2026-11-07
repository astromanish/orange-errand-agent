"""Phase 1 tests: SQLite store, approval gates, idempotency, and all 8 tool contracts."""
from __future__ import annotations

import unittest

from errand_agent.store import ErrandStore, compute_idempotency_key
from errand_agent.tools import ScenarioConfig, ToolEnvironment, PROMPT_INJECTION_REVIEW


class FakeClock:
    def __init__(self, initial: float = 1_700_000_000.0):
        self.t = initial

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class Phase1StoreAndToolsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.store = ErrandStore(":memory:", clock=self.clock)
        self.config = ScenarioConfig(seed=42)
        self.env = ToolEnvironment(self.store, self.config, default_user_id="user_test")

    def tearDown(self) -> None:
        self.store.close()

    def test_1_search_and_get_provider_with_prompt_injection(self) -> None:
        res = self.env.search_providers("plumber", "Indiranagar", "2026-11-09")
        self.assertEqual(res["status"], "ok")
        self.assertGreaterEqual(len(res["providers"]), 3)
        self.assertLessEqual(len(res["providers"]), 8)

        prov = self.env.get_provider("prov_plumb_01")
        self.assertEqual(prov["status"], "ok")
        self.assertIn(PROMPT_INJECTION_REVIEW, prov["reviews"])

    def test_2_approval_enforcement_and_price_drift_invalidation(self) -> None:
        # Hold slot at normal price (850)
        hold = self.env.hold_slot("prov_plumb_01", "2026-11-09T09:00")
        self.assertEqual(hold["quoted_price"], 850)
        idem = compute_idempotency_key("user_test", "book", hold["hold_id"])

        # Attempting to book without approval MUST raise PermissionError
        with self.assertRaises(PermissionError):
            self.env.book(hold["hold_id"], idem)

        # Grant approval for 850
        self.store.grant_approval(
            "user_test",
            action="book",
            provider_id="prov_plumb_01",
            slot="2026-11-09T09:00",
            price=850,
        )

        # Now simulate price drift on a new hold_slot (quoted price 950 vs approved 850)
        self.config.hold_price_overrides["prov_plumb_01"] = 950
        hold2 = self.env.hold_slot("prov_plumb_01", "2026-11-09T09:00")
        self.assertTrue(hold2["price_changed"])
        self.assertEqual(hold2["quoted_price"], 950)

        # Prior approval for 850 must have been automatically invalidated!
        idem2 = compute_idempotency_key("user_test", "book", hold2["hold_id"])
        with self.assertRaises(PermissionError):
            self.env.book(hold2["hold_id"], idem2)

    def test_3_hold_expiry_returns_hold_expired(self) -> None:
        hold = self.env.hold_slot("prov_plumb_01", "2026-11-09T09:00")
        self.store.grant_approval(
            "user_test", "book", provider_id="prov_plumb_01", slot="2026-11-09T09:00", price=850
        )
        # Advance clock past 10 minutes (601s)
        self.clock.advance(601.0)
        idem = compute_idempotency_key("user_test", "book", hold["hold_id"])
        res = self.env.book(hold["hold_id"], idem)
        self.assertEqual(res["status"], "hold_expired")

    def test_4_timeout_after_success_and_idempotent_resolution(self) -> None:
        self.config.force_book_outcome = "timeout_booked"
        hold = self.env.hold_slot("prov_plumb_01", "2026-11-09T09:00")
        self.store.grant_approval(
            "user_test", "book", provider_id="prov_plumb_01", slot="2026-11-09T09:00", price=850
        )
        idem = compute_idempotency_key("user_test", "book", hold["hold_id"])

        # 1st call returns ambiguous timeout, even though it booked underneath
        res1 = self.env.book(hold["hold_id"], idem)
        self.assertEqual(res1["status"], "timeout")

        # Authoritative check via get_booking_status resolves the truth
        status = self.env.get_booking_status(idem)
        self.assertEqual(status["status"], "booked")
        booking_id = status["booking"]["booking_id"]

        # Even if book() is called again with the same idempotency_key, no duplicate is created
        res2 = self.env.book(hold["hold_id"], idem)
        self.assertEqual(res2["status"], "confirmed")
        self.assertTrue(res2.get("idempotent_replay"))
        self.assertEqual(res2["booking_id"], booking_id)

        all_bks = self.env.list_bookings("user_test")["bookings"]
        self.assertEqual(len(all_bks), 1)

    def test_5_cancel_first_fails_503_then_succeeds(self) -> None:
        self.config.force_book_outcome = "success"
        hold = self.env.hold_slot("prov_elec_01", "2026-11-09T10:30")
        self.store.grant_approval(
            "user_test", "book", provider_id="prov_elec_01", slot="2026-11-09T10:30", price=700
        )
        bk = self.env.book(
            hold["hold_id"], compute_idempotency_key("user_test", "book", hold["hold_id"])
        )
        booking_id = bk["booking_id"]

        # Grant cancel approval
        self.store.grant_approval("user_test", "cancel", booking_id=booking_id)
        cancel_idem = compute_idempotency_key("user_test", "cancel", booking_id)

        # 1st cancel call must fail with 503
        c1 = self.env.cancel(booking_id, cancel_idem)
        self.assertEqual(c1["status"], "error")
        self.assertEqual(c1["http_status"], 503)

        # Retry with same idempotency key succeeds
        c2 = self.env.cancel(booking_id, cancel_idem)
        self.assertEqual(c2["status"], "cancelled")
        self.assertEqual(c2["attempts"], 2)

    def test_6_reschedule_slot_conflict_and_success(self) -> None:
        bk = self.env.seed_booking(
            "user_test", "prov_ac_01", "2026-11-12T10:00", status="confirmed"
        )
        booking_id = bk["booking_id"]

        # Approve reschedule to conflict slot 2026-11-14T14:00
        self.store.grant_approval(
            "user_test",
            "reschedule",
            provider_id="prov_ac_01",
            slot="2026-11-14T14:00",
            price=1100,
            booking_id=booking_id,
        )
        r1 = self.env.reschedule(
            booking_id,
            "2026-11-14T14:00",
            compute_idempotency_key("user_test", "reschedule", booking_id, "2026-11-14T14:00"),
        )
        self.assertEqual(r1["status"], "slot_conflict")
        self.assertIn("2026-11-14T10:00", r1["alternative_slots"])

        # Approve reschedule to valid slot 2026-11-14T10:00
        self.store.grant_approval(
            "user_test",
            "reschedule",
            provider_id="prov_ac_01",
            slot="2026-11-14T10:00",
            price=1100,
            booking_id=booking_id,
        )
        r2 = self.env.reschedule(
            booking_id,
            "2026-11-14T10:00",
            compute_idempotency_key("user_test", "reschedule", booking_id, "2026-11-14T10:00"),
        )
        self.assertEqual(r2["status"], "rescheduled")
        self.assertEqual(r2["new_slot"], "2026-11-14T10:00")

    def test_7_memories_explicit_vs_inferred_and_forget(self) -> None:
        m1 = self.store.set_memory("user_test", "area", "Indiranagar", source="explicit")
        self.assertTrue(m1["confirmed"])

        m2 = self.store.set_memory(
            "user_test", "preferred_plumber", "prov_plumb_01", source="inferred"
        )
        self.assertFalse(m2["confirmed"])

        # Only confirmed memories should return m1
        confirmed = self.store.list_memories("user_test", only_confirmed=True)
        self.assertEqual([m["key"] for m in confirmed], ["area"])

        # Forget m1
        self.store.forget_memory("user_test", "area")
        active = self.store.list_memories("user_test", only_confirmed=True)
        self.assertEqual(active, [])


if __name__ == "__main__":
    unittest.main()
