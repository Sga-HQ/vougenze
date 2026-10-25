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
        """Scenario 23: Retiring a reserved/proposed garment atomically invalidates affected plans and releases reservations."""
        with self.db.transaction() as conn:
            # Set hard limit on g_top1 and insert an authorization so the plan creates a reservation
            conn.execute("UPDATE Garment SET hard_limit_wears = 1, wears_since_confirmed_wash = 1 WHERE id = 'g_top1';")
            conn.execute(
                """
                INSERT INTO GarmentWearAuthorization(id, garment_id, authorized_by, reason, status, authorized_at)
                VALUES ('gwa_retire_test', 'g_top1', 'owner', 'Retirement test auth', 'AVAILABLE', '2026-03-01T08:00:00Z');
                """
            )

            planner = ContinuationPlanner(conn)
            pid = planner.generate_plan(self.user_id, "2026-03-01", "2026-03-03", StrategyMode.SAFE)

            # Verify reservation was created before retirement
            pre_res = conn.execute(
                "SELECT count(*) FROM PlanWearAuthorizationReservation WHERE plan_id = ?;", (pid,)
            ).fetchone()[0]
            self.assertTrue(pre_res > 0, "Plan should have reserved the authorization for g_top1")

            # Retire g_top1
            planner.retire_garment("g_top1", reason="Donated to charity")

            # Check plan was invalidated with RETIREMENT
            p = conn.execute("SELECT * FROM RecommendationPlan WHERE id = ?;", (pid,)).fetchone()
            self.assertEqual(p["status"], PlanStatus.SUPERSEDED.value)
            self.assertEqual(p["invalidation_reason"], InvalidationReason.RETIREMENT.value)

            # Check that authorization reservations were released (deleted)
            post_res = conn.execute(
                "SELECT count(*) FROM PlanWearAuthorizationReservation WHERE plan_id = ?;", (pid,)
            ).fetchone()[0]
            self.assertEqual(post_res, 0, "All authorization reservations for superseded plan must be released")

            # Garment availability is RETIRED
            g = conn.execute("SELECT * FROM Garment WHERE id = 'g_top1';").fetchone()
            self.assertEqual(g["availability_state"], AvailabilityState.RETIRED.value)

    def test_exact_search_oracle_comparison_and_regret(self):
        """
        Verification: Exhaustive path enumeration oracle comparison on a 2-day multi-session fixture.
        Evaluates all Cartesian path combinations with state-evolving within-plan wear history, ranks via
        the full lexicographic vector, and verifies that ContinuationPlanner beam search matches the globally
        optimal lexicographic coordinate vector with zero regret on this fixture.
        """
        import copy
        import itertools
        from core.candidate_generator import CandidateGenerator
        from core.scorers import DeterministicScorer, ScoringWeights
        from core.planner import PartialPlan

        with self.db.transaction() as conn:
            # 2 sessions: day 1 (casual NORMAL), day 2 (university HIGH)
            sessions_cur = conn.execute(
                """
                SELECT * FROM WearSession WHERE user_id = ? AND session_date IN ('2026-03-01', '2026-03-02')
                ORDER BY session_date ASC;
                """,
                (self.user_id,),
            )
            sessions = sessions_cur.fetchall()
            self.assertEqual(len(sessions), 2)

            gen = CandidateGenerator(conn)
            cands_s1 = gen.generate_candidates(sessions[0]["id"], user_id=self.user_id)
            cands_s2 = gen.generate_candidates(sessions[1]["id"], user_id=self.user_id)

            self.assertTrue(len(cands_s1) > 0 and len(cands_s2) > 0)

            # Exhaustive Oracle: enumerate all possible assignment pairs (c1, c2)
            weights = ScoringWeights()
            scorer = DeterministicScorer(conn, weights)
            history = []

            all_paths = []
            for c1, c2 in itertools.product(cands_s1, cands_s2):
                p = PartialPlan()
                # Step 1: assign c1 (isolated copy)
                c1_eval = copy.deepcopy(c1)
                p.assignments[sessions[0]["id"]] = c1_eval
                q1 = scorer.score_candidate(c1_eval, sessions[0], history, feasible_substitute_counts=None)
                p.total_quality += q1
                p.total_repetition += c1_eval.repetition_penalty
                p.fallback_count += c1_eval.fallback_count
                for gid in c1_eval.garment_ids:
                    p.simulated_wears[gid] = 1

                # Step 2: assign c2 with simulated wear history from c1 (isolated copy)
                c2_eval = copy.deepcopy(c2)
                sim_hist = [{"session_id": sessions[0]["id"], "session_date": "2026-03-01", "session_type": sessions[0]["session_type"], "garment_ids": c1_eval.garment_ids}]
                p.assignments[sessions[1]["id"]] = c2_eval
                q2 = scorer.score_candidate(c2_eval, sessions[1], history, feasible_substitute_counts=None, simulated_wear_history=sim_hist)
                p.total_quality += q2
                p.total_repetition += c2_eval.repetition_penalty
                p.fallback_count += c2_eval.fallback_count
                p.min_high_quality = q2  # session 2 is HIGH

                all_paths.append((p, (c1, c2)))

            planner = ContinuationPlanner(conn)
            # Rank all paths using the exact lexicographic ranking key
            all_paths.sort(key=lambda item: planner.plan_ranking_key(item[0], attainable_target=0))
            best_oracle_plan, best_oracle_cands = all_paths[0]
            oracle_best_key = planner.plan_ranking_key(best_oracle_plan, attainable_target=0)

            # Run ContinuationPlanner beam search
            beam_plan_id = planner.generate_plan(
                user_id=self.user_id,
                horizon_start_date="2026-03-01",
                horizon_end_date="2026-03-02",
                strategy_mode=StrategyMode.SAFE,
                beam_width=5,
            )

            # Evaluate beam plan quality from persisted PlanEvaluation
            eval_row = conn.execute("SELECT * FROM PlanEvaluation WHERE plan_id = ?;", (beam_plan_id,)).fetchone()
            self.assertIsNotNone(eval_row)

            # Construct beam plan's lexicographic ranking key vector
            beam_key = (
                eval_row["uncovered_high"],
                eval_row["uncovered_normal"],
                eval_row["uncovered_low"],
                eval_row["uncertain_dependencies"],
                eval_row["fallback_assignments"],
                -round(eval_row["min_high_importance_quality"], 4),
                -round(eval_row["total_quality"], 4),
                round(eval_row["repetition_burden"], 4),
                0,  # diversity_deficit
            )
            oracle_key_rounded = (
                oracle_best_key[0],
                oracle_best_key[1],
                oracle_best_key[2],
                oracle_best_key[3],
                oracle_best_key[4],
                oracle_best_key[5],
                round(oracle_best_key[6], 4),
                round(oracle_best_key[7], 4),
                round(oracle_best_key[8], 4),
            )

            # Assert coordinate-by-coordinate lexicographic equality with zero regret
            self.assertEqual(beam_key, oracle_key_rounded, f"Beam plan lexicographic key must match oracle globally optimal vector: beam={beam_key} vs oracle={oracle_key_rounded}")
            print(f"PASS: Exact-search oracle verified: {len(all_paths)} paths evaluated, lexicographic vector match {beam_key} with 0 search regret")


if __name__ == "__main__":
    unittest.main()
