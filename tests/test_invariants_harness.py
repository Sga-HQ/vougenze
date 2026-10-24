"""
Vougenze Mechanical Invariant & Constraint Harness.
Executes positive and negative constraint assertions, partial uniqueness checks,
allowed behaviors, and transactional rollback boundaries with explicit PASS logging.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sqlite3
import unittest
from core.db import Database
from core.models import ConditionState, AvailabilityState


class SchemaInvariantHarness:
    """Explicit test harness that prints pass/fail diagnostics for every mechanical invariant."""

    def __init__(self):
        self.passed = 0
        self.failed = 0

    def assert_pass(self, description: str, condition: bool):
        if condition:
            self.passed += 1
            print(f"PASS: {description}")
        else:
            self.failed += 1
            print(f"FAIL: {description}")
            raise AssertionError(f"Invariant assertion failed: {description}")

    def assert_rejected(self, description: str, conn: sqlite3.Connection, sql: str, params=()):
        try:
            conn.execute(sql, params)
            self.failed += 1
            print(f"FAIL: {description} (unexpectedly accepted)")
            raise AssertionError(f"Expected rejection but statement succeeded: {description}")
        except (sqlite3.IntegrityError, sqlite3.OperationalError):
            self.passed += 1
            print(f"PASS: {description}")

    def run_all(self):
        db = Database(":memory:")
        db.migrate()

        with db.connection() as conn:
            # 1. Fresh installation & table count
            tables = conn.execute(
                "SELECT count(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name != 'SchemaMigration';"
            ).fetchone()[0]
            self.assert_pass("34 expected domain tables exist", tables == 34)

            # 2. Foreign keys on Garment
            fks = conn.execute("PRAGMA foreign_key_list('Garment');").fetchall()
            fk_map = {r["from"]: r["table"] for r in fks}
            self.assert_pass(
                "current_laundry_batch_id references LaundryBatch",
                fk_map.get("current_laundry_batch_id") == "LaundryBatch"
            )
            self.assert_pass(
                "current_condition_assessment_id references GarmentCondition",
                fk_map.get("current_condition_assessment_id") == "GarmentCondition"
            )

            # 3. PRAGMA integrity & FK checks on clean schema
            integrity = conn.execute("PRAGMA integrity_check;").fetchone()[0]
            self.assert_pass("integrity_check = ok", integrity == "ok")
            fk_violations = conn.execute("PRAGMA foreign_key_check;").fetchall()
            self.assert_pass("foreign_key_check = empty", len(fk_violations) == 0)

            # Setup seed user
            conn.execute("INSERT INTO User(id, display_name) VALUES ('u1', 'Alice');")
            conn.execute("INSERT INTO User(id, display_name) VALUES ('u2', 'Bob');")

            # 4. Constraint Rejection Tests
            # 4.1 Invalid like_rating
            self.assert_rejected(
                "invalid like_rating (< 1) rejected",
                conn,
                "INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color, like_rating) VALUES ('g_bad_1', 'u1', 'T', 'top', 't', 'r', 'b', 0);"
            )
            self.assert_rejected(
                "invalid like_rating (> 10) rejected",
                conn,
                "INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color, like_rating) VALUES ('g_bad_2', 'u1', 'T', 'top', 't', 'r', 'b', 11);"
            )

            # 4.2 Invalid formality
            self.assert_rejected(
                "invalid formality (< 1) rejected",
                conn,
                "INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color, formality) VALUES ('g_bad_3', 'u1', 'T', 'top', 't', 'r', 'b', 0);"
            )
            self.assert_rejected(
                "invalid formality (> 5) rejected",
                conn,
                "INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color, formality) VALUES ('g_bad_4', 'u1', 'T', 'top', 't', 'r', 'b', 6);"
            )

            # 4.3 Negative wear count
            self.assert_rejected(
                "negative wear count rejected",
                conn,
                "INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color, wears_since_confirmed_wash) VALUES ('g_bad_5', 'u1', 'T', 'top', 't', 'r', 'b', -1);"
            )

            # 4.4 Invalid condition_state / availability_state
            self.assert_rejected(
                "invalid condition_state rejected",
                conn,
                "INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color, condition_state) VALUES ('g_bad_6', 'u1', 'T', 'top', 't', 'r', 'b', 'READY_FOR_REVIEW');"
            )
            self.assert_rejected(
                "invalid availability_state rejected",
                conn,
                "INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color, availability_state) VALUES ('g_bad_7', 'u1', 'T', 'top', 't', 'r', 'b', 'LOST');"
            )

            # Insert valid garments for FK tests
            conn.execute(
                "INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color) VALUES ('g_shirt', 'u1', 'Shirt', 'top', 'shirt', 'regular', 'blue');"
            )
            conn.execute(
                "INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color) VALUES ('g_pants', 'u1', 'Pants', 'bottom', 'jeans', 'regular', 'black');"
            )

            # 4.5 Orphan foreign keys rejected
            self.assert_rejected(
                "orphan user foreign key rejected",
                conn,
                "INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color) VALUES ('g_bad_8', 'nonexistent_u', 'T', 'top', 't', 'r', 'b');"
            )
            self.assert_rejected(
                "orphan current_condition_assessment_id rejected",
                conn,
                "UPDATE Garment SET current_condition_assessment_id = 'nonexistent_gca' WHERE id = 'g_shirt';"
            )

            # 4.6 LaundryBatch expected_return_date >= sent_date
            self.assert_rejected(
                "laundry batch expected_return_date < sent_date rejected",
                conn,
                "INSERT INTO LaundryBatch(id, user_id, sent_date, expected_return_date) VALUES ('lb_bad', 'u1', '2026-03-10', '2026-03-05');"
            )
            conn.execute(
                "INSERT INTO LaundryBatch(id, user_id, sent_date, expected_return_date) VALUES ('lb_valid', 'u1', '2026-03-05', '2026-03-10');"
            )

            # 4.7 Contradictory laundry outcome
            self.assert_rejected(
                "contradictory laundry outcome RETURNED_CLEAN with wash_confirmed=0 rejected",
                conn,
                "INSERT INTO LaundryBatchItem(batch_id, garment_id, item_status, wash_confirmed) VALUES ('lb_valid', 'g_shirt', 'RETURNED_CLEAN', 0);"
            )
            self.assert_rejected(
                "contradictory laundry outcome RETURNED_UNWASHED with wash_confirmed=1 rejected",
                conn,
                "INSERT INTO LaundryBatchItem(batch_id, garment_id, item_status, wash_confirmed) VALUES ('lb_valid', 'g_shirt', 'RETURNED_UNWASHED', 1);"
            )

            # 4.8 Contradictory WashEvent: FULL_CLEAN spot_clean rejected
            self.assert_rejected(
                "FULL_CLEAN spot-clean rejected",
                conn,
                "INSERT INTO WashEvent(id, user_id, garment_id, cleaning_scope, method, confirmed_clean_at) VALUES ('w_bad', 'u1', 'g_shirt', 'FULL_CLEAN', 'spot_clean', '2026-03-01T10:00:00Z');"
            )

            # 4.9 Contradictory GarmentWearAuthorization
            self.assert_rejected(
                "contradictory authorization CONSUMED with null wear_event_id rejected",
                conn,
                "INSERT INTO GarmentWearAuthorization(id, garment_id, status, consumed_by_wear_event_id) VALUES ('gwa_bad_1', 'g_shirt', 'CONSUMED', NULL);"
            )
            self.assert_rejected(
                "contradictory authorization AVAILABLE with non-null wear_event_id rejected",
                conn,
                "INSERT INTO GarmentWearAuthorization(id, garment_id, status, consumed_by_wear_event_id) VALUES ('gwa_bad_2', 'g_shirt', 'AVAILABLE', 'some_event');"
            )

            # 4.10 Date-range checks
            self.assert_rejected(
                "LaundryPolicy effective_until < effective_from rejected",
                conn,
                "INSERT INTO LaundryPolicy(id, user_id, policy_type, effective_from, effective_until) VALUES ('lp_bad', 'u1', 'weekly', '2026-05-01', '2026-04-01');"
            )
            self.assert_rejected(
                "WearSessionTemplate effective_until < effective_from rejected",
                conn,
                "INSERT INTO WearSessionTemplate(id, user_id, template_name, effective_from, effective_until) VALUES ('wst_bad', 'u1', 'temp', '2026-05-01', '2026-04-01');"
            )
            self.assert_rejected(
                "RecommendationPlan horizon_end_date < horizon_start_date rejected",
                conn,
                "INSERT INTO RecommendationPlan(id, user_id, strategy_mode, horizon_start_date, horizon_end_date) VALUES ('rp_bad', 'u1', 'SAFE', '2026-05-10', '2026-05-01');"
            )
            self.assert_rejected(
                "OccasionProfile formality_min > formality_max rejected",
                conn,
                "INSERT INTO OccasionProfile(id, user_id, name, formality_min, formality_max) VALUES ('op_bad', 'u1', 'profile', 4, 2);"
            )

            # 5. Partial Uniqueness Invariants
            conn.execute(
                "INSERT INTO WearSession(id, user_id, session_date, session_type) VALUES ('ws_1', 'u1', '2026-03-01', 'casual');"
            )
            conn.execute(
                "INSERT INTO OutfitWearLog(id, user_id, session_id, source, local_wear_date, timezone_id, session_type, record_status) VALUES ('owl_1', 'u1', 'ws_1', 'manual_outfit', '2026-03-01', 'UTC', 'casual', 'ACTIVE');"
            )
            self.assert_rejected(
                "Second ACTIVE outfit log for same session rejected",
                conn,
                "INSERT INTO OutfitWearLog(id, user_id, session_id, source, local_wear_date, timezone_id, session_type, record_status) VALUES ('owl_2', 'u1', 'ws_1', 'manual_outfit', '2026-03-01', 'UTC', 'casual', 'ACTIVE');"
            )

            # Mark first log CORRECTED, then new ACTIVE log is accepted
            conn.execute("UPDATE OutfitWearLog SET record_status = 'CORRECTED' WHERE id = 'owl_1';")
            conn.execute(
                "INSERT INTO OutfitWearLog(id, user_id, session_id, source, local_wear_date, timezone_id, session_type, record_status) VALUES ('owl_2', 'u1', 'ws_1', 'manual_outfit', '2026-03-01', 'UTC', 'casual', 'ACTIVE');"
            )
            self.assert_pass("CORRECTED old log plus replacement ACTIVE log accepted", True)

            # WearEvent partial uniqueness
            conn.execute(
                "INSERT INTO WearEvent(id, session_id, garment_id, outfit_wear_log_id, is_voided) VALUES ('we_1', 'ws_1', 'g_shirt', 'owl_2', 0);"
            )
            self.assert_rejected(
                "Second active wear event for same session+garment rejected",
                conn,
                "INSERT INTO WearEvent(id, session_id, garment_id, outfit_wear_log_id, is_voided) VALUES ('we_2', 'ws_1', 'g_shirt', 'owl_2', 0);"
            )

            # Void first event, then replacement active event is accepted
            conn.execute("UPDATE WearEvent SET is_voided = 1 WHERE id = 'we_1';")
            conn.execute(
                "INSERT INTO WearEvent(id, session_id, garment_id, outfit_wear_log_id, is_voided) VALUES ('we_2', 'ws_1', 'g_shirt', 'owl_2', 0);"
            )
            self.assert_pass("VOIDED old event plus replacement active event accepted", True)

            # Authorization partial uniqueness
            conn.execute(
                "INSERT INTO GarmentWearAuthorization(id, garment_id, status, consumed_by_wear_event_id) VALUES ('gwa_1', 'g_shirt', 'CONSUMED', 'we_2');"
            )
            self.assert_rejected(
                "One wear event consuming two authorizations rejected",
                conn,
                "INSERT INTO GarmentWearAuthorization(id, garment_id, status, consumed_by_wear_event_id) VALUES ('gwa_2', 'g_shirt', 'CONSUMED', 'we_2');"
            )

            # 6. Allowed Behavior Tests
            # 6.1 Duplicate SHA-256 values accepted across different image records
            conn.execute(
                "INSERT INTO GarmentImage(id, garment_id, image_path) VALUES ('gi_1', 'g_shirt', 'img1.jpg');"
            )
            conn.execute(
                "INSERT INTO GarmentImage(id, garment_id, image_path) VALUES ('gi_2', 'g_shirt', 'img2.jpg');"
            )
            conn.execute(
                "INSERT INTO GarmentImageFingerprint(id, garment_image_id, sha256) VALUES ('gif_1', 'gi_1', 'sha_abc123');"
            )
            conn.execute(
                "INSERT INTO GarmentImageFingerprint(id, garment_image_id, sha256) VALUES ('gif_2', 'gi_2', 'sha_abc123');"
            )
            self.assert_pass("Duplicate SHA-256 values accepted across images", True)

            # 6.2 Different users may upload the same image fingerprint
            conn.execute(
                "INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color) VALUES ('g_u2_shirt', 'u2', 'Shirt2', 'top', 'shirt', 'regular', 'white');"
            )
            conn.execute(
                "INSERT INTO GarmentImage(id, garment_id, image_path) VALUES ('gi_u2', 'g_u2_shirt', 'img_u2.jpg');"
            )
            conn.execute(
                "INSERT INTO GarmentImageFingerprint(id, garment_image_id, sha256) VALUES ('gif_u2', 'gi_u2', 'sha_abc123');"
            )
            self.assert_pass("Different users may upload the same image fingerprint", True)

            # 6.3 Same one-piece inserted for top and bottom roles accepted
            conn.execute(
                "INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color) VALUES ('g_dress', 'u1', 'Summer Dress', 'one_piece', 'dress', 'regular', 'red');"
            )
            conn.execute(
                "INSERT INTO OutfitWearLogItem(outfit_wear_log_id, garment_id, outfit_role, condition_at_wear, availability_at_wear, wears_before_event) VALUES ('owl_2', 'g_dress', 'top', 'CLEAN', 'AVAILABLE', 0);"
            )
            conn.execute(
                "INSERT INTO OutfitWearLogItem(outfit_wear_log_id, garment_id, outfit_role, condition_at_wear, availability_at_wear, wears_before_event) VALUES ('owl_2', 'g_dress', 'bottom', 'CLEAN', 'AVAILABLE', 0);"
            )
            self.assert_pass("Same one-piece inserted for top and bottom roles accepted", True)

        print(f"\nTOTAL: {self.passed} passed, {self.failed} failed\n")
        return self.failed == 0


class TestSchemaInvariantsHarness(unittest.TestCase):
    def test_run_harness(self):
        harness = SchemaInvariantHarness()
        success = harness.run_all()
        self.assertTrue(success, "All mechanical schema assertions must pass")


if __name__ == "__main__":
    unittest.main()
