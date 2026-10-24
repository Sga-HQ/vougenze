"""
Unit tests for core.candidate_generator.
Validates Scenarios 6, 8, 20, and candidate snapshots.
"""
import json
import unittest
from core.db import Database
from core.models import PriorityLevel
from core.candidate_generator import CandidateGenerator


class TestCandidateGenerator(unittest.TestCase):

    def setUp(self):
        self.db = Database(":memory:")
        self.db.migrate()
        self.user_id = "u1"
        self.session_uni = "ws_uni"
        self.session_home = "ws_home"

        with self.db.transaction() as conn:
            conn.execute("INSERT INTO User(id, display_name) VALUES (?, 'Test User');", (self.user_id,))
            conn.execute(
                """
                INSERT INTO WearSession(id, user_id, session_date, session_type, required_roles)
                VALUES (?, ?, '2026-03-01', 'university', '["top", "bottom", "footwear"]');
                """,
                (self.session_uni, self.user_id),
            )
            conn.execute(
                """
                INSERT INTO WearSession(id, user_id, session_date, session_type, required_roles)
                VALUES (?, ?, '2026-03-01', 'home_casual', '["top", "bottom"]');
                """,
                (self.session_home, self.user_id),
            )

    def test_context_priority_override_and_fallback(self):
        """Scenarios 6 & 8: Context override filters hoodie out of university unless fallback is needed."""
        with self.db.transaction() as conn:
            # Normal shirt
            conn.execute(
                """
                INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color, priority_level)
                VALUES ('g_normal_shirt', ?, 'Polo Shirt', 'top', 'polo', 'regular', 'blue', 'NORMAL');
                """,
                (self.user_id,),
            )
            # Normal pants and shoes
            conn.execute(
                """
                INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color)
                VALUES ('g_pants', ?, 'Chinos', 'bottom', 'chino', 'slim', 'beige');
                """,
                (self.user_id,),
            )
            conn.execute(
                """
                INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color)
                VALUES ('g_shoes', ?, 'Sneakers', 'footwear', 'sneaker', 'regular', 'white');
                """,
                (self.user_id,),
            )

            # Scenario 8: Oversized hoodie: priority_level = FAVOURITE, but context_priority_overrides = {"university": "FALLBACK_ONLY"}
            overrides = json.dumps({"university": "FALLBACK_ONLY"})
            conn.execute(
                """
                INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color,
                                   priority_level, context_priority_overrides)
                VALUES ('g_hoodie', ?, 'Grey Hoodie', 'top', 'hoodie', 'oversized', 'grey', 'FAVOURITE', ?);
                """,
                (self.user_id, overrides),
            )

            gen = CandidateGenerator(conn)

            # In university session, normal shirt is available -> hoodie is excluded (Scenario 6 & 8)
            uni_cands = gen.generate_candidates(self.session_uni)
            top_ids = [c.roles["top"] for c in uni_cands]
            self.assertIn("g_normal_shirt", top_ids)
            self.assertNotIn("g_hoodie", top_ids, "Hoodie must be excluded for university when normal top is available")

            # In home_casual session, hoodie has effective priority FAVOURITE -> included!
            home_cands = gen.generate_candidates(self.session_home)
            home_tops = [c.roles["top"] for c in home_cands]
            self.assertIn("g_hoodie", home_tops, "Hoodie must be included in home_casual as favourite")

            # Now, simulate normal shirt in laundry: hoodie must be selected as fallback (Scenario 6)
            conn.execute("UPDATE Garment SET availability_state = 'IN_LAUNDRY' WHERE id = 'g_normal_shirt';")

            fallback_cands = gen.generate_candidates(self.session_uni)
            self.assertEqual(len(fallback_cands), 1)
            self.assertEqual(fallback_cands[0].roles["top"], "g_hoodie")
            self.assertTrue(fallback_cands[0].is_fallback, "Candidate must be marked as fallback")

    def test_multi_role_one_piece(self):
        """Scenario 20: Jumpsuit satisfies top and bottom roles via fills_roles without duplicate entities."""
        with self.db.transaction() as conn:
            # Add jumpsuit filling ["top", "bottom"]
            conn.execute(
                """
                INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color, fills_roles)
                VALUES ('g_jumpsuit', ?, 'Linen Jumpsuit', 'one_piece', 'jumpsuit', 'regular', 'olive', '["top", "bottom"]');
                """,
                (self.user_id,),
            )
            # Add shoes
            conn.execute(
                """
                INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color)
                VALUES ('g_sandals', ?, 'Leather Sandals', 'footwear', 'sandal', 'regular', 'tan');
                """,
                (self.user_id,),
            )

            gen = CandidateGenerator(conn)
            cands = gen.generate_candidates(self.session_uni)

            # Look for candidate with jumpsuit
            jumpsuit_cands = [c for c in cands if "g_jumpsuit" in c.garment_ids]
            self.assertTrue(len(jumpsuit_cands) >= 1)

            cand = jumpsuit_cands[0]
            self.assertEqual(cand.roles["top"], "g_jumpsuit")
            self.assertEqual(cand.roles["bottom"], "g_jumpsuit")
            self.assertEqual(cand.roles["footwear"], "g_sandals")

            # Persist snapshot
            snap_id = gen.snapshot_candidate(cand, session_id=self.session_uni, user_id=self.user_id)
            self.assertEqual(snap_id, cand.id)

            # Check snapshot items
            items = conn.execute(
                "SELECT * FROM OutfitCandidateSnapshotItem WHERE candidate_id = ?;", (cand.id,)
            ).fetchall()
            self.assertEqual(len(items), 3)  # top, bottom, footwear


if __name__ == "__main__":
    unittest.main()
