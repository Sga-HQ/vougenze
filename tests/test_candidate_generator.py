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

    def test_compute_candidate_substitutes_canonical_cores(self):
        """
        Direct unit tests for CandidateGenerator.compute_candidate_substitutes:
        1. One-piece replaced by one top and one bottom.
        2. Top plus bottom replaced by a one-piece.
        3. Role-map ordering does not affect core identity.
        4. Same replacement core through different supporting garments counts once.
        5. Incomplete restoration of target roles does not count.
        6. Selected physical garment remaining in alternative does not count as replacement.
        """
        with self.db.transaction() as conn:
            gen = CandidateGenerator(conn)

        from core.models import OutfitCandidate

        def make_cand(cand_id: str, roles: dict) -> OutfitCandidate:
            return OutfitCandidate(
                id=cand_id,
                roles=roles,
                garment_ids=list(set(roles.values())),
            )

        # 1. One-piece replaced by one top and one bottom
        cand_one_piece = make_cand("c_dress", {"top": "g_dress", "bottom": "g_dress", "footwear": "g_shoes1"})
        cand_two_piece = make_cand("c_sep", {"top": "g_shirt1", "bottom": "g_pants1", "footwear": "g_shoes1"})
        candidates = [cand_one_piece, cand_two_piece]

        subs = gen.compute_candidate_substitutes(candidates, cand_one_piece)
        self.assertEqual(subs["top"], 2, "One-piece top role should count two-piece replacement core")
        self.assertEqual(subs["bottom"], 2, "One-piece bottom role should count two-piece replacement core")
        self.assertEqual(subs["footwear"], 1, "Footwear identical across both candidates, substitute count is 1")

        # 2. Top plus bottom replaced by a one-piece
        subs_rev = gen.compute_candidate_substitutes(candidates, cand_two_piece)
        self.assertEqual(subs_rev["top"], 2, "Separate top should count one-piece replacement core")
        self.assertEqual(subs_rev["bottom"], 2, "Separate bottom should count one-piece replacement core")

        # 3. Role-map ordering does not affect core identity
        cand_unordered_1 = make_cand("c_u1", {"top": "g_shirt2", "bottom": "g_pants2", "footwear": "g_shoes1"})
        cand_unordered_2 = make_cand("c_u2", {"bottom": "g_pants2", "top": "g_shirt2", "footwear": "g_shoes1"})
        cands_ordering = [cand_two_piece, cand_unordered_1, cand_unordered_2]
        subs_order = gen.compute_candidate_substitutes(cands_ordering, cand_two_piece)
        self.assertEqual(subs_order["top"], 2, "Reordered roles must yield the same canonical core and count once")
        self.assertEqual(subs_order["bottom"], 2, "Reordered roles must yield the same canonical core and count once")

        # 4. Same replacement core through different supporting garments counts once
        cand_diff_shoes = make_cand("c_diff_shoes", {"top": "g_shirt1", "bottom": "g_pants1", "footwear": "g_shoes2"})
        cands_multi_support = [cand_one_piece, cand_two_piece, cand_diff_shoes]
        subs_supp = gen.compute_candidate_substitutes(cands_multi_support, cand_one_piece)
        self.assertEqual(subs_supp["top"], 2, "Different supporting garments must not inflate replacement-core count")
        self.assertEqual(subs_supp["bottom"], 2, "Different supporting garments must not inflate replacement-core count")

        # 5. Incomplete restoration of target roles does not count
        cand_incomplete = make_cand("c_incomp", {"top": "g_shirt1", "footwear": "g_shoes1"})
        cands_incomp = [cand_one_piece, cand_incomplete]
        subs_incomp = gen.compute_candidate_substitutes(cands_incomp, cand_one_piece)
        self.assertEqual(subs_incomp["top"], 1, "Incomplete restoration of one-piece target roles must not count")
        self.assertEqual(subs_incomp["bottom"], 1, "Incomplete restoration of one-piece target roles must not count")

        # 6. Selected physical garment remaining in alternative does not count as replacement
        cand_partial_reuse = make_cand("c_reuse", {"top": "g_shirt1", "bottom": "g_pants2", "footwear": "g_shoes1"})
        cands_reuse = [cand_two_piece, cand_partial_reuse]
        subs_reuse = gen.compute_candidate_substitutes(cands_reuse, cand_two_piece)
        self.assertEqual(subs_reuse["top"], 1, "Alternative containing selected top cannot count as substitute for top")
        self.assertEqual(subs_reuse["bottom"], 2, "Alternative replacing pants1 with pants2 counts as substitute for bottom")

    def test_substitute_operational_boundary_exclusions(self):
        """
        7. Dirty or reserved replacement garments are excluded at candidate generation boundary:
        - Dirty garments (NEEDS_WASH) are not authorized for candidate generation.
        - Garments reserved by PROPOSED or RESERVED plans (VZ-P6) are excluded from hard-limit candidates.
        """
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color,
                                    condition_state, availability_state)
                VALUES ('g_top_clean', ?, 'Clean Top', 'top', 'shirt', 'regular', 'white', 'CLEAN', 'AVAILABLE'),
                       ('g_top_dirty', ?, 'Dirty Top', 'top', 'shirt', 'regular', 'black', 'NEEDS_WASH', 'AVAILABLE');
                """,
                (self.user_id, self.user_id),
            )
            conn.execute(
                """
                INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color,
                                    condition_state, availability_state)
                VALUES ('g_b_1', ?, 'Pants', 'bottom', 'pants', 'regular', 'blue', 'CLEAN', 'AVAILABLE'),
                       ('g_f_1', ?, 'Shoes', 'footwear', 'shoes', 'regular', 'brown', 'CLEAN', 'AVAILABLE');
                """,
                (self.user_id, self.user_id),
            )
            gen = CandidateGenerator(conn)
            cands = gen.generate_candidates(self.session_uni)
            tops_in_cands = {c.roles["top"] for c in cands}
            self.assertIn("g_top_clean", tops_in_cands)
            self.assertNotIn("g_top_dirty", tops_in_cands, "Dirty garment must be excluded from candidate generation")

            # Check VZ-P6: Garment at hard limit with available authorization reserved by active plan
            conn.execute(
                """
                INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color,
                                    hard_limit_wears, wears_since_confirmed_wash, condition_state, availability_state)
                VALUES ('g_top_hl', ?, 'Hard Limit Top', 'top', 'shirt', 'regular', 'grey', 1, 1, 'CLEAN', 'AVAILABLE');
                """,
                (self.user_id,),
            )
            conn.execute(
                """
                INSERT INTO GarmentWearAuthorization(id, garment_id, authorized_at, status)
                VALUES ('auth_p6', 'g_top_hl', '2026-03-01T00:00:00Z', 'AVAILABLE');
                """
            )
            cands_before = gen.generate_candidates(self.session_uni)
            self.assertIn("g_top_hl", {c.roles["top"] for c in cands_before})

            # Reserve auth_p6 in a PROPOSED plan
            conn.execute(
                """
                INSERT INTO RecommendationPlan(id, user_id, strategy_mode, horizon_start_date, horizon_end_date, status)
                VALUES ('plan_p6', ?, 'SAFE', '2026-03-01', '2026-03-02', 'PROPOSED');
                """,
                (self.user_id,),
            )
            conn.execute(
                """
                INSERT INTO PlanWearAuthorizationReservation(plan_id, session_id, garment_id, authorization_id, created_at)
                VALUES ('plan_p6', ?, 'g_top_hl', 'auth_p6', '2026-03-01T00:00:00Z');
                """,
                (self.session_uni,),
            )

            # After reservation: g_top_hl must be excluded by VZ-P6 guard!
            cands_after = gen.generate_candidates(self.session_uni)
            self.assertNotIn("g_top_hl", {c.roles["top"] for c in cands_after}, "Reserved authorization must exclude garment from candidates")


if __name__ == "__main__":
    unittest.main()

