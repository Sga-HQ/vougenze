"""
Unit tests for core.intake_service.
Validates Scenarios 37, 38, 39, and 40.
"""
import unittest
from core.db import Database
from core.intake_service import IntakeService


class TestIntakeService(unittest.TestCase):

    def setUp(self):
        self.db = Database(":memory:")
        self.db.migrate()
        self.user_id = "u1"
        self.existing_gid = "g_navy_blazer"

        with self.db.transaction() as conn:
            conn.execute("INSERT INTO User(id, display_name) VALUES (?, 'Test User');", (self.user_id,))
            conn.execute(
                """
                INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color)
                VALUES (?, ?, 'Navy Blazer', 'outerwear', 'blazer', 'slim', 'navy');
                """,
                (self.existing_gid, self.user_id),
            )
            # Add existing catalogued image with known SHA-256
            conn.execute(
                "INSERT INTO GarmentImage(id, garment_id, image_path) VALUES ('gi1', ?, '/images/blazer.jpg');",
                (self.existing_gid,),
            )
            conn.execute(
                "INSERT INTO GarmentImageFingerprint(id, garment_image_id, sha256) VALUES ('gif1', 'gi1', 'hash_blazer_front');"
            )

    def test_pre_garment_duplicate_detection(self):
        """Scenario 37: Staging an image with matching fingerprint flags candidate before garment creation."""
        with self.db.transaction() as conn:
            service = IntakeService(conn)
            draft_id, candidate_id = service.stage_intake_draft(
                user_id=self.user_id,
                temporary_image_path="/tmp/upload_1.jpg",
                sha256_hash="hash_blazer_front",  # exact match
            )

            self.assertIsNotNone(candidate_id, "Candidate must be created for exact fingerprint match")

            # Verify no new Garment was created
            count = conn.execute("SELECT count(*) FROM Garment;").fetchone()[0]
            self.assertEqual(count, 1)

            # Check candidate details
            cand = conn.execute("SELECT * FROM GarmentDuplicateCandidate WHERE id = ?;", (candidate_id,)).fetchone()
            self.assertEqual(cand["existing_garment_id"], self.existing_gid)
            self.assertEqual(cand["status"], "PENDING")
            self.assertEqual(cand["exact_hash_match"], 1)

    def test_resolve_as_same_garment_multi_angle(self):
        """Scenario 38: Attaches image to existing garment and updates draft status."""
        with self.db.transaction() as conn:
            service = IntakeService(conn)
            draft_id, candidate_id = service.stage_intake_draft(
                user_id=self.user_id,
                temporary_image_path="/tmp/blazer_back.jpg",
                sha256_hash="hash_blazer_front",
            )

            service.resolve_as_same_garment(
                candidate_id=candidate_id,
                image_type="detail",
                resolution_note="Back view of the same blazer",
            )

            # Existing garment should now have 2 images
            img_count = conn.execute(
                "SELECT count(*) FROM GarmentImage WHERE garment_id = ?;", (self.existing_gid,)
            ).fetchone()[0]
            self.assertEqual(img_count, 2)

            # Draft status should be ATTACHED_TO_EXISTING
            draft = conn.execute("SELECT * FROM GarmentIntakeDraft WHERE id = ?;", (draft_id,)).fetchone()
            self.assertEqual(draft["status"], "ATTACHED_TO_EXISTING")

    def test_resolve_as_different_garment(self):
        """Scenario 39: Creates new garment when confirmed distinct."""
        with self.db.transaction() as conn:
            service = IntakeService(conn)
            draft_id, candidate_id = service.stage_intake_draft(
                user_id=self.user_id,
                temporary_image_path="/tmp/blazer_twin.jpg",
                sha256_hash="hash_blazer_front",
            )

            new_gid = service.resolve_as_different_garment(
                candidate_id=candidate_id,
                garment_data={
                    "name": "Second Navy Blazer",
                    "physical_category": "outerwear",
                    "subcategory": "blazer",
                    "primary_color": "navy",
                },
                resolution_note="Second blazer bought for office backup",
            )

            # Total garments should now be 2
            count = conn.execute("SELECT count(*) FROM Garment;").fetchone()[0]
            self.assertEqual(count, 2)

            # Draft status should be CREATED_NEW_GARMENT
            draft = conn.execute("SELECT * FROM GarmentIntakeDraft WHERE id = ?;", (draft_id,)).fetchone()
            self.assertEqual(draft["status"], "CREATED_NEW_GARMENT")

    def test_duplicate_candidate_advisory_and_non_blocking(self):
        """Scenario 40: Pending duplicate candidate does not block garment usage."""
        with self.db.transaction() as conn:
            service = IntakeService(conn)
            draft_id, candidate_id = service.stage_intake_draft(
                user_id=self.user_id,
                temporary_image_path="/tmp/upload_pending.jpg",
                sha256_hash="hash_blazer_front",
            )

            # Candidate is PENDING
            cand = conn.execute("SELECT * FROM GarmentDuplicateCandidate WHERE id = ?;", (candidate_id,)).fetchone()
            self.assertEqual(cand["status"], "PENDING")

            # Existing garment is still AVAILABLE and eligible
            g = conn.execute("SELECT * FROM Garment WHERE id = ?;", (self.existing_gid,)).fetchone()
            self.assertEqual(g["availability_state"], "AVAILABLE")
            self.assertEqual(g["condition_state"], "CLEAN")


if __name__ == "__main__":
    unittest.main()
