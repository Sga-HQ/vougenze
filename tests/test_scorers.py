"""
Unit tests for core.scorers.
Validates Q formula, Scenario 5 (Sleepwear), Scenario 28 (Single Footwear), and Scenario 29 (Cross-Context Repetition).
"""
import unittest
from core.db import Database
from core.models import OutfitCandidate
from core.scorers import DeterministicScorer
from core.wear_service import WearService


class TestDeterministicScorers(unittest.TestCase):

    def setUp(self):
        self.db = Database(":memory:")
        self.db.migrate()
        self.user_id = "u1"

        with self.db.transaction() as conn:
            conn.execute("INSERT INTO User(id, display_name) VALUES (?, 'Test User');", (self.user_id,))
            # Insert top, bottom, shoes
            conn.execute(
                """
                INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color, formality)
                VALUES ('g_shirt', ?, 'Navy Linen Shirt', 'top', 'shirt', 'regular', 'navy', 3);
                """,
                (self.user_id,),
            )
            conn.execute(
                """
                INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color, formality)
                VALUES ('g_chinos', ?, 'Beige Chinos', 'bottom', 'chino', 'slim', 'beige', 3);
                """,
                (self.user_id,),
            )
            conn.execute(
                """
                INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color, formality)
                VALUES ('g_sneakers', ?, 'White Sneakers', 'footwear', 'sneaker', 'regular', 'white', 2);
                """,
                (self.user_id,),
            )
            conn.execute(
                """
                INSERT INTO WearSession(id, user_id, session_date, session_type)
                VALUES ('ws_uni', ?, '2026-03-02', 'university');
                """,
                (self.user_id,),
            )
            conn.execute(
                """
                INSERT INTO WearSession(id, user_id, session_date, session_type)
                VALUES ('ws_sleep', ?, '2026-03-02', 'sleepwear');
                """,
                (self.user_id,),
            )
            conn.execute(
                """
                INSERT INTO WearSession(id, user_id, session_date, session_type)
                VALUES ('ws_party', ?, '2026-03-02', 'party');
                """,
                (self.user_id,),
            )

    def test_formula_and_scoring(self):
        with self.db.transaction() as conn:
            scorer = DeterministicScorer(conn)
            session = conn.execute("SELECT * FROM WearSession WHERE id = 'ws_uni';").fetchone()

            cand = OutfitCandidate(
                id="cand_1",
                garment_ids=["g_shirt", "g_chinos", "g_sneakers"],
                roles={"top": "g_shirt", "bottom": "g_chinos", "footwear": "g_sneakers"},
            )

            q = scorer.score_candidate(
                candidate=cand,
                session=session,
                recent_wear_history=[],
                user_garment_counts_by_role={"top": 5, "bottom": 3, "footwear": 2},
            )

            # Compatibility: navy + beige (both neutrals) + same formality -> high compatibility
            self.assertGreater(cand.compatibility_score, 0.7)
            # Repetition with empty history is 0.0
            self.assertEqual(cand.repetition_penalty, 0.0)
            # Verify formula
            expected = round(
                0.30 * cand.compatibility_score
                + 0.35 * cand.occasion_score
                + 0.25 * cand.preference_score
                - 0.10 * cand.repetition_penalty,
                4,
            )
            self.assertEqual(cand.quality_score, expected)

    def test_sleepwear_reuse_zero_repetition_penalty(self):
        """Scenario 5: Sleepwear context has zero visibility penalty -> repetition penalty is 0.0."""
        with self.db.transaction() as conn:
            # Log wear of shirt in sleepwear on 2026-03-01 (1 day prior)
            service = WearService(conn)
            conn.execute(
                "INSERT INTO WearSession(id, user_id, session_date, session_type) VALUES ('ws_yesterday', ?, '2026-03-01', 'sleepwear');",
                (self.user_id,),
            )
            service.log_outfit_wear(
                user_id=self.user_id,
                session_id="ws_yesterday",
                garment_roles={"top": "g_shirt"},
                local_wear_date="2026-03-01",
                session_type="sleepwear",
            )

            history = service.query_active_history(self.user_id, "2026-03-02")
            session_sleep = conn.execute("SELECT * FROM WearSession WHERE id = 'ws_sleep';").fetchone()

            scorer = DeterministicScorer(conn)
            cand = OutfitCandidate(
                id="cand_sleep",
                garment_ids=["g_shirt"],
                roles={"top": "g_shirt"},
            )
            scorer.score_candidate(cand, session_sleep, history, {"top": 3})
            self.assertEqual(cand.repetition_penalty, 0.0, "Sleepwear repetition penalty must be 0.0")

    def test_constraint_driven_footwear_repetition(self):
        """Scenario 28: If user only owns 1 pair of shoes, repetition penalty is 0.0."""
        with self.db.transaction() as conn:
            # Log wear of sneakers yesterday
            service = WearService(conn)
            conn.execute(
                "INSERT INTO WearSession(id, user_id, session_date, session_type) VALUES ('ws_prev', ?, '2026-03-01', 'university');",
                (self.user_id,),
            )
            service.log_outfit_wear(
                user_id=self.user_id,
                session_id="ws_prev",
                garment_roles={"footwear": "g_sneakers"},
                local_wear_date="2026-03-01",
                session_type="university",
            )

            history = service.query_active_history(self.user_id, "2026-03-02")
            session_uni = conn.execute("SELECT * FROM WearSession WHERE id = 'ws_uni';").fetchone()

            scorer = DeterministicScorer(conn)
            cand = OutfitCandidate(
                id="cand_shoes",
                garment_ids=["g_sneakers"],
                roles={"footwear": "g_sneakers"},
            )

            # User only has 1 footwear!
            scorer.score_candidate(cand, session_uni, history, {"footwear": 1})
            self.assertEqual(cand.repetition_penalty, 0.0, "Constraint-driven footwear repetition must be 0.0")

            # But if user has 3 shoes, repetition penalty should be high (> 0.5) because worn yesterday in same context!
            cand_multi = OutfitCandidate(
                id="cand_shoes_multi",
                garment_ids=["g_sneakers"],
                roles={"footwear": "g_sneakers"},
            )
            scorer.score_candidate(cand_multi, session_uni, history, {"footwear": 3})
            self.assertGreater(cand_multi.repetition_penalty, 0.5)

    def test_cross_context_repetition_isolation(self):
        """Scenario 29: home_casual to party cross-context repetition factor is 0.0."""
        with self.db.transaction() as conn:
            service = WearService(conn)
            conn.execute(
                "INSERT INTO WearSession(id, user_id, session_date, session_type) VALUES ('ws_home', ?, '2026-03-01', 'home_casual');",
                (self.user_id,),
            )
            service.log_outfit_wear(
                user_id=self.user_id,
                session_id="ws_home",
                garment_roles={"top": "g_shirt"},
                local_wear_date="2026-03-01",
                session_type="home_casual",
            )

            history = service.query_active_history(self.user_id, "2026-03-02")
            session_party = conn.execute("SELECT * FROM WearSession WHERE id = 'ws_party';").fetchone()

            scorer = DeterministicScorer(conn)
            cand = OutfitCandidate(
                id="cand_party",
                garment_ids=["g_shirt"],
                roles={"top": "g_shirt"},
            )

            # Wearing in party after home_casual has factor 0.0
            scorer.score_candidate(cand, session_party, history, {"top": 3})
            self.assertEqual(cand.repetition_penalty, 0.0, "Cross-context home_casual to party factor must be 0.0")


if __name__ == "__main__":
    unittest.main()
