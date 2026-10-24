"""
Unit tests for core.planner.
Validates Scenarios 9, 21, 22, 23, and 36.
"""
import unittest
from core.db import Database
from core.models import StrategyMode, PlanStatus, InvalidationReason, AvailabilityState
from core.planner import ContinuationPlanner


class TestContinuationPlanner(unittest.TestCase):

    def setUp(self):
        self.db = Database(":memory:")
        self.db.migrate()
        self.user_id = "u1"

        with self.db.transaction() as conn:
            conn.execute("INSERT INTO User(id, display_name) VALUES (?, 'Test User');", (self.user_id,))
            # Add basic wardrobe items
            for gid, name, cat, col, form in [
                ("g_top1", "White Linen Shirt", "top", "white", 2),
                ("g_top2", "Navy Oxford Shirt", "top", "navy", 3),
                ("g_bot1", "Khaki Chinos", "bottom", "beige", 2),
                ("g_bot2", "Dark Denim", "bottom", "navy", 3),
                ("g_shoe1", "White Sneakers", "footwear", "white", 2),
                ("g_shoe2", "Brown Loafers", "footwear", "brown", 3),
            ]:
                conn.execute(
                    """
                    INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color, formality)
                    VALUES (?, ?, ?, ?, 'standard', 'regular', ?, ?);
                    """,
                    (gid, self.user_id, name, cat, col, form),
                )

            # Create sessions across a 3-day horizon
            conn.execute(
                """
                INSERT INTO WearSession(id, user_id, session_date, session_type, importance)
                VALUES ('ws_d1', ?, '2026-03-01', 'casual', 'NORMAL');
                """,
                (self.user_id,),
            )
            conn.execute(
                """
                INSERT INTO WearSession(id, user_id, session_date, session_type, importance)
                VALUES ('ws_d2', ?, '2026-03-02', 'university', 'HIGH');
                """,
                (self.user_id,),
            )
            conn.execute(
                """
                INSERT INTO WearSession(id, user_id, session_date, session_type, importance)
                VALUES ('ws_d3', ?, '2026-03-03', 'casual', 'LOW');
                """,
                (self.user_id,),
            )

    def test_three_feasible_strategies(self):
        """Scenario 9 & 36: Safe, Balanced, and Exploratory return independently feasible plans."""
        with self.db.transaction() as conn:
            planner = ContinuationPlanner(conn)

            safe_pid = planner.generate_plan(self.user_id, "2026-03-01", "2026-03-03", StrategyMode.SAFE)
            bal_pid = planner.generate_plan(self.user_id, "2026-03-01", "2026-03-03", StrategyMode.BALANCED)
            exp_pid = planner.generate_plan(self.user_id, "2026-03-01", "2026-03-03", StrategyMode.EXPLORATORY)

            # Check that each plan is PROPOSED and covers all sessions
            for pid, mode in [(safe_pid, "SAFE"), (bal_pid, "BALANCED"), (exp_pid, "EXPLORATORY")]:
                p = conn.execute("SELECT * FROM RecommendationPlan WHERE id = ?;", (pid,)).fetchone()
                self.assertEqual(p["strategy_mode"], mode)
                self.assertEqual(p["status"], "PROPOSED")

                eval_row = conn.execute("SELECT * FROM PlanEvaluation WHERE plan_id = ?;", (pid,)).fetchone()
                self.assertEqual(eval_row["uncovered_high"], 0)
                self.assertEqual(eval_row["uncovered_normal"], 0)

                # Check 3 assignments exist
                assigns = conn.execute("SELECT * FROM PlanSessionAssignment WHERE plan_id = ?;", (pid,)).fetchall()
                self.assertEqual(len(assigns), 3)

    def test_limited_strategy_diversity(self):
        """Scenario 21: Wardrobe with only 1 valid outfit returns valid plans for all 3 strategies with diversity_achieved: 0."""
        with self.db.transaction() as conn:
            # Delete all garments and insert exactly 1 valid outfit (1 top, 1 bottom, 1 footwear)
            conn.execute("DELETE FROM Garment WHERE user_id = ?;", (self.user_id,))
            conn.execute(
                """
                INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color)
                VALUES ('g_only_top', ?, 'T-Shirt', 'top', 'tee', 'regular', 'black'),
                       ('g_only_bot', ?, 'Jeans', 'bottom', 'jeans', 'regular', 'blue'),
                       ('g_only_shoes', ?, 'Boots', 'footwear', 'boot', 'regular', 'black');
                """,
                (self.user_id, self.user_id, self.user_id),
            )

            planner = ContinuationPlanner(conn)
            pid = planner.generate_plan(self.user_id, "2026-03-01", "2026-03-03", StrategyMode.BALANCED)

            p = conn.execute("SELECT * FROM RecommendationPlan WHERE id = ?;", (pid,)).fetchone()
            self.assertEqual(p["diversity_achieved"], 0)
            self.assertIn("Limited wardrobe", p["diversity_notes"])

    def test_retirement_invalidation(self):
        """Scenario 23: Retiring a reserved/proposed garment atomically invalidates affected plans."""
        with self.db.transaction() as conn:
            planner = ContinuationPlanner(conn)
            pid = planner.generate_plan(self.user_id, "2026-03-01", "2026-03-03", StrategyMode.SAFE)

            # Retire g_top1
            planner.retire_garment("g_top1", reason="Donated to charity")

            # Check plan was invalidated with RETIREMENT
            p = conn.execute("SELECT * FROM RecommendationPlan WHERE id = ?;", (pid,)).fetchone()
            self.assertEqual(p["status"], PlanStatus.SUPERSEDED.value)
            self.assertEqual(p["invalidation_reason"], InvalidationReason.RETIREMENT.value)

            # Garment availability is RETIRED
            g = conn.execute("SELECT * FROM Garment WHERE id = 'g_top1';").fetchone()
            self.assertEqual(g["availability_state"], AvailabilityState.RETIRED.value)


if __name__ == "__main__":
    unittest.main()
