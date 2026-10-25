"""
Unit tests for core.wear_service.
Validates Scenarios 7, 17, 24, 26, 27, 30, and wear authorizations.
"""
import json
import sqlite3
import unittest
from core.db import Database
from core.models import ConditionState
from core.wear_service import WearService


class TestWearService(unittest.TestCase):

    def setUp(self):
        self.db = Database(":memory:")
        self.db.migrate()
        self.user_id = "u1"
        self.shirt_id = "g_shirt"
        self.jeans_id = "g_jeans"
        self.shoes_id = "g_shoes"
        self.session_id = "ws_monday"

        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO User(id, display_name, timezone_id) VALUES (?, 'Alice', 'Asia/Kolkata');",
                (self.user_id,),
            )
            for gid, name, cat in [
                (self.shirt_id, "Linen Shirt", "top"),
                (self.jeans_id, "Denim Jeans", "bottom"),
                (self.shoes_id, "White Sneakers", "footwear"),
            ]:
                conn.execute(
                    """
                    INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color)
                    VALUES (?, ?, ?, ?, 'standard', 'regular', 'blue');
                    """,
                    (gid, self.user_id, name, cat),
                )
            conn.execute(
                """
                INSERT INTO WearSession(id, user_id, session_date, session_type)
                VALUES (?, ?, '2026-03-01', 'university');
                """,
                (self.session_id, self.user_id),
            )

    def test_log_outfit_wear_and_snapshot_preservation(self):
        """Scenarios 7 & 27: Wear logging increments counter and snapshots metadata."""
        roles = {"top": self.shirt_id, "bottom": self.jeans_id, "footwear": self.shoes_id}
        with self.db.transaction() as conn:
            service = WearService(conn)
            log_id = service.log_outfit_wear(
                user_id=self.user_id,
                session_id=self.session_id,
                garment_roles=roles,
                local_wear_date="2026-03-01",
            )

            # Check wear counts incremented
            g = conn.execute("SELECT * FROM Garment WHERE id = ?;", (self.shirt_id,)).fetchone()
            self.assertEqual(g["wears_since_confirmed_wash"], 1)
            self.assertEqual(g["condition_state"], ConditionState.WORN_AWAITING_ASSESSMENT.value)

            # Scenario 27: Rename garment later
            conn.execute("UPDATE Garment SET name = 'Vintage Oxford Shirt' WHERE id = ?;", (self.shirt_id,))

            # Verify snapshot in OutfitWearLogItem remains original
            item_cur = conn.execute(
                "SELECT garment_snapshot FROM OutfitWearLogItem WHERE outfit_wear_log_id = ? AND garment_id = ?;",
                (log_id, self.shirt_id),
            )
            snapshot = json.loads(item_cur.fetchone()["garment_snapshot"])
            self.assertEqual(snapshot["name"], "Linen Shirt", "Snapshot must preserve historical state")

    def test_duplicate_wear_event_idempotency_guard(self):
        """Scenario 17: Partial unique index blocks duplicate active wear event for same session+garment."""
        roles = {"top": self.shirt_id, "bottom": self.jeans_id, "footwear": self.shoes_id}
        with self.db.transaction() as conn:
            service = WearService(conn)
            service.log_outfit_wear(
                user_id=self.user_id,
                session_id=self.session_id,
                garment_roles=roles,
            )

        # Attempting to log again for the same session raises IntegrityError due to partial unique index
        # First ensure condition allows wear (WORN_REUSABLE) and session is PLANNED
        with self.db.transaction() as conn:
            conn.execute("UPDATE Garment SET condition_state = 'WORN_REUSABLE';")
            conn.execute("UPDATE WearSession SET status = 'PLANNED';")

        with self.assertRaises(sqlite3.IntegrityError):
            with self.db.transaction() as conn:
                service = WearService(conn)
                service.log_outfit_wear(
                    user_id=self.user_id,
                    session_id=self.session_id,
                    garment_roles=roles,
                )

    def test_wear_authorization_consumption(self):
        """Validates that an available authorization is consumed upon wear event when at hard limit."""
        with self.db.transaction() as conn:
            # Set garment at hard limit
            conn.execute(
                "UPDATE Garment SET hard_limit_wears = 1, wears_since_confirmed_wash = 1 WHERE id = ?;",
                (self.shirt_id,),
            )
            service = WearService(conn)
            auth_id = service.create_wear_authorization(
                garment_id=self.shirt_id,
                reason="Important presentation override",
            )

            roles = {"top": self.shirt_id, "bottom": self.jeans_id, "footwear": self.shoes_id}
            service.log_outfit_wear(
                user_id=self.user_id,
                session_id=self.session_id,
                garment_roles=roles,
            )

            # Verify authorization is now CONSUMED
            auth_cur = conn.execute("SELECT * FROM GarmentWearAuthorization WHERE id = ?;", (auth_id,))
            auth = auth_cur.fetchone()
            self.assertEqual(auth["status"], "CONSUMED")
            self.assertIsNotNone(auth["consumed_by_wear_event_id"])

    def test_historical_wear_correction_rebuilds_state(self):
        """Scenario 30: Voiding a wear log rebuilds garment wear counts from remaining events."""
        roles = {"top": self.shirt_id, "bottom": self.jeans_id, "footwear": self.shoes_id}
        with self.db.transaction() as conn:
            service = WearService(conn)
            log_id = service.log_outfit_wear(
                user_id=self.user_id,
                session_id=self.session_id,
                garment_roles=roles,
            )

            # Garment has 1 wear
            g = conn.execute("SELECT * FROM Garment WHERE id = ?;", (self.shirt_id,)).fetchone()
            self.assertEqual(g["wears_since_confirmed_wash"], 1)

            # Void the log
            service.void_outfit_wear(log_id, void_reason="Wrong clothes logged")

            # Check wear count reset to 0 and condition restored to CLEAN
            g_after = conn.execute("SELECT * FROM Garment WHERE id = ?;", (self.shirt_id,)).fetchone()
            self.assertEqual(g_after["wears_since_confirmed_wash"], 0)
            self.assertEqual(g_after["condition_state"], ConditionState.CLEAN.value)

            # Check OutfitWearLog is VOIDED
            owl = conn.execute("SELECT * FROM OutfitWearLog WHERE id = ?;", (log_id,)).fetchone()
            self.assertEqual(owl["record_status"], "VOIDED")

    def test_active_history_query_100_days(self):
        """Scenario 24: Active history query filters to configured window (e.g. 100 days)."""
        with self.db.transaction() as conn:
            service = WearService(conn)

            # Create sessions and logs at 10 days ago, 50 days ago, 120 days ago
            for sid, dt in [("ws_10", "2026-03-20"), ("ws_50", "2026-02-08"), ("ws_120", "2025-11-30")]:
                conn.execute(
                    "INSERT INTO WearSession(id, user_id, session_date, session_type) VALUES (?, ?, ?, 'casual');",
                    (sid, self.user_id, dt),
                )
                service.log_outfit_wear(
                    user_id=self.user_id,
                    session_id=sid,
                    garment_roles={"top": self.shirt_id},
                    local_wear_date=dt,
                )
                conn.execute("UPDATE Garment SET condition_state = 'CLEAN' WHERE id = ?;", (self.shirt_id,))

            # Query with 100-day window relative to 2026-03-30
            records = service.query_active_history(
                user_id=self.user_id,
                reference_date="2026-03-30",
                window_days=100,
            )
            dates = [r["local_wear_date"] for r in records]
            self.assertIn("2026-03-20", dates)
            self.assertIn("2026-02-08", dates)
            self.assertNotIn("2025-11-30", dates, "120 days ago record must be excluded from 100-day window")

            # But all history (-1) includes it!
            all_records = service.query_active_history(
                user_id=self.user_id,
                reference_date="2026-03-30",
                window_days=-1,
            )
            all_dates = [r["local_wear_date"] for r in all_records]
            self.assertIn("2025-11-30", all_dates)


if __name__ == "__main__":
    unittest.main()
