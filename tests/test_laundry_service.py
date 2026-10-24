"""
Unit tests for core.laundry_service.
Validates Scenarios 4, 12, 15, 18, 41, and 42.
"""
import unittest
from core.db import Database
from core.models import ConditionState, AvailabilityState, LaundryItemStatus
from core.laundry_service import LaundryService


class TestLaundryService(unittest.TestCase):

    def setUp(self):
        self.db = Database(":memory:")
        self.db.migrate()
        self.user_id = "u1"
        self.garment_ids = ["g1", "g2", "g3"]

        with self.db.transaction() as conn:
            conn.execute("INSERT INTO User(id, display_name) VALUES (?, 'Test User');", (self.user_id,))
            for gid in self.garment_ids:
                conn.execute(
                    """
                    INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color,
                                       condition_state, availability_state, wears_since_confirmed_wash)
                    VALUES (?, ?, 'Item', 'top', 'shirt', 'regular', 'white', 'NEEDS_WASH', 'AVAILABLE', 2);
                    """,
                    (gid, self.user_id),
                )

    def test_partial_return_and_terminal_outcomes(self):
        """Scenarios 12 & 41: 2 items returned -> PARTIAL_RETURN; 3rd item returned -> RETURNED_CONFIRMED."""
        with self.db.transaction() as conn:
            service = LaundryService(conn)
            batch_id = service.send_laundry_batch(
                user_id=self.user_id,
                garment_ids=self.garment_ids,
                sent_date="2026-02-01",
                expected_return_date="2026-02-04",
            )

            # All 3 items should now be IN_LAUNDRY
            for gid in self.garment_ids:
                g = conn.execute("SELECT * FROM Garment WHERE id = ?;", (gid,)).fetchone()
                self.assertEqual(g["availability_state"], AvailabilityState.IN_LAUNDRY.value)

            # Scenario 12: Return 2 items clean, 1 stays in laundry
            service.record_item_return(
                batch_id=batch_id,
                garment_id="g1",
                item_status=LaundryItemStatus.RETURNED_CLEAN,
                wash_confirmed=True,
            )
            service.record_item_return(
                batch_id=batch_id,
                garment_id="g2",
                item_status=LaundryItemStatus.RETURNED_CLEAN,
                wash_confirmed=True,
            )

            # Check g1 and g2: condition CLEAN, availability AVAILABLE, wears = 0
            for gid in ["g1", "g2"]:
                g = conn.execute("SELECT * FROM Garment WHERE id = ?;", (gid,)).fetchone()
                self.assertEqual(g["availability_state"], AvailabilityState.AVAILABLE.value)
                self.assertEqual(g["condition_state"], ConditionState.CLEAN.value)
                self.assertEqual(g["wears_since_confirmed_wash"], 0)

            # Check batch status: PARTIAL_RETURN
            b = conn.execute("SELECT * FROM LaundryBatch WHERE id = ?;", (batch_id,)).fetchone()
            self.assertEqual(b["status"], "PARTIAL_RETURN")

            # Scenario 41: Return 3rd item as MISSING_RESOLVED
            service.record_item_return(
                batch_id=batch_id,
                garment_id="g3",
                item_status=LaundryItemStatus.MISSING_RESOLVED,
                wash_confirmed=False,
            )

            # g3 availability becomes UNAVAILABLE
            g3 = conn.execute("SELECT * FROM Garment WHERE id = ?;", ("g3",)).fetchone()
            self.assertEqual(g3["availability_state"], AvailabilityState.UNAVAILABLE.value)

            # Batch status now closes to RETURNED_CONFIRMED
            b_closed = conn.execute("SELECT * FROM LaundryBatch WHERE id = ?;", (batch_id,)).fetchone()
            self.assertEqual(b_closed["status"], "RETURNED_CONFIRMED")

    def test_damaged_item_routes_to_repair(self):
        """Scenario 42: Damaged laundry return with wash_confirmed=1 sets CLEAN and routes to REPAIR."""
        with self.db.transaction() as conn:
            service = LaundryService(conn)
            batch_id = service.send_laundry_batch(
                user_id=self.user_id,
                garment_ids=["g1"],
                sent_date="2026-02-01",
                expected_return_date="2026-02-04",
            )

            service.record_item_return(
                batch_id=batch_id,
                garment_id="g1",
                item_status=LaundryItemStatus.RETURNED_DAMAGED,
                wash_confirmed=True,
            )

            g1 = conn.execute("SELECT * FROM Garment WHERE id = ?;", ("g1",)).fetchone()
            # Must be CLEAN because wash was confirmed, but REPAIR because damaged (bypassing AVAILABLE)
            self.assertEqual(g1["condition_state"], ConditionState.CLEAN.value)
            self.assertEqual(g1["availability_state"], AvailabilityState.REPAIR.value)
            self.assertEqual(g1["wears_since_confirmed_wash"], 0)

    def test_atomic_laundry_return_rollback(self):
        """Scenario 18: Injected failure during laundry return transaction rolls back completely."""
        with self.db.transaction() as conn:
            service = LaundryService(conn)
            batch_id = service.send_laundry_batch(
                user_id=self.user_id,
                garment_ids=["g1", "g2"],
                sent_date="2026-02-01",
                expected_return_date="2026-02-04",
            )

        # Attempt atomic return with failure halfway
        with self.assertRaises(RuntimeError):
            with self.db.transaction() as conn:
                service = LaundryService(conn)
                service.record_item_return(
                    batch_id=batch_id,
                    garment_id="g1",
                    item_status=LaundryItemStatus.RETURNED_CLEAN,
                    wash_confirmed=True,
                )
                raise RuntimeError("Simulated crash midway through batch return")

        # Verify: g1 remains in IN_LAUNDRY and NEEDS_WASH; no WashEvent created
        with self.db.connection() as conn:
            g1 = conn.execute("SELECT * FROM Garment WHERE id = ?;", ("g1",)).fetchone()
            self.assertEqual(g1["availability_state"], AvailabilityState.IN_LAUNDRY.value)
            self.assertEqual(g1["condition_state"], ConditionState.NEEDS_WASH.value)
            wash_count = conn.execute("SELECT count(*) FROM WashEvent WHERE garment_id = ?;", ("g1",)).fetchone()[0]
            self.assertEqual(wash_count, 0, "No WashEvent should exist after rollback")

    def test_laundry_policy_versioning(self):
        """Scenario 15: Updating policy sets effective_until on old policy and creates new policy."""
        with self.db.transaction() as conn:
            service = LaundryService(conn)
            p1 = service.create_policy(
                user_id=self.user_id,
                policy_type="weekly_monday",
                effective_from="2026-01-01",
                handover_weekday=0,
            )

            p2 = service.update_policy(
                user_id=self.user_id,
                old_policy_id=p1,
                policy_type="biweekly_wednesday",
                effective_from="2026-02-01",
                handover_weekday=2,
            )

            # Check p1 has effective_until = '2026-02-01'
            old_p = conn.execute("SELECT * FROM LaundryPolicy WHERE id = ?;", (p1,)).fetchone()
            self.assertEqual(old_p["effective_until"], "2026-02-01")

            # Check p2 exists and is effective from 2026-02-01
            new_p = conn.execute("SELECT * FROM LaundryPolicy WHERE id = ?;", (p2,)).fetchone()
            self.assertEqual(new_p["effective_from"], "2026-02-01")
            self.assertIsNone(new_p["effective_until"])


if __name__ == "__main__":
    unittest.main()
