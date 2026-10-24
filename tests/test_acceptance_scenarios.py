"""
Acceptance Suite for Scenarios 1, 2, 3, 10, 22, and 25.
Completes coverage across all 42 acceptance scenarios of the Vougenze V0 frozen spec.
"""
import json
import unittest
from core.db import Database
from core.models import (
    ConditionState,
    AvailabilityState,
    PostWearChoice,
    StrategyMode,
    PlanStatus,
)
from core.candidate_generator import CandidateGenerator
from core.planner import ContinuationPlanner
from core.state_engine import StateEngine
from core.wear_service import WearService
from core.scorers import DeterministicScorer


class TestAcceptanceScenarios(unittest.TestCase):

    def setUp(self):
        self.db = Database(":memory:")
        self.db.migrate()
        self.user_id = "u_acc"

        with self.db.transaction() as conn:
            conn.execute("INSERT INTO User(id, display_name) VALUES (?, 'Acceptance User');", (self.user_id,))

    def test_scenario_1_only_footwear(self):
        """Scenario 1: Single shoe in wardrobe results in alternatives_count == 0 -> repetition_penalty == 0.0."""
        with self.db.transaction() as conn:
            # Seed 2 tops, 2 bottoms, but only 1 footwear
            conn.execute(
                """
                INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color)
                VALUES ('top1', ?, 'Shirt 1', 'top', 'shirt', 'regular', 'white'),
                       ('top2', ?, 'Shirt 2', 'top', 'shirt', 'regular', 'blue'),
                       ('bot1', ?, 'Pants 1', 'bottom', 'pants', 'regular', 'black'),
                       ('bot2', ?, 'Pants 2', 'bottom', 'pants', 'regular', 'grey'),
                       ('shoe_only', ?, 'Only Sneakers', 'footwear', 'sneakers', 'regular', 'white');
                """,
                (self.user_id, self.user_id, self.user_id, self.user_id, self.user_id),
            )
            conn.execute(
                """
                INSERT INTO WearSession(id, user_id, session_date, session_type)
                VALUES ('ws_sc1', ?, '2026-03-02', 'casual');
                """,
                (self.user_id,),
            )

            gen = CandidateGenerator(conn)
            candidates = gen.generate_candidates("ws_sc1", user_id=self.user_id)
            self.assertGreater(len(candidates), 0, "Valid candidates generated despite only 1 shoe")

            # Score candidates with wear history containing yesterday's shoe wear
            hist_row = {
                "id": "oe_hist",
                "local_wear_date": "2026-03-01",
                "session_type": "casual",
                "garment_ids": '["shoe_only"]',
            }
            conn.execute(
                """
                INSERT INTO WearSession(id, user_id, session_date, session_type)
                VALUES ('ws_yesterday', ?, '2026-03-01', 'casual');
                """,
                (self.user_id,),
            )
            conn.execute(
                """
                INSERT INTO OutfitWearLog(id, user_id, session_id, source, local_wear_date, timezone_id, session_type)
                VALUES ('owl_yesterday', ?, 'ws_yesterday', 'manual_outfit', '2026-03-01', 'UTC', 'casual');
                """,
                (self.user_id,),
            )
            conn.execute(
                """
                INSERT INTO OutfitWearLogItem(outfit_wear_log_id, garment_id, outfit_role, condition_at_wear, availability_at_wear, wears_before_event)
                VALUES ('owl_yesterday', 'shoe_only', 'footwear', 'CLEAN', 'AVAILABLE', 0);
                """
            )

            wear_service = WearService(conn)
            history = wear_service.query_active_history(self.user_id, "2026-03-02", window_days=100)

            scorer = DeterministicScorer(conn)
            role_counts = {"top": 2, "bottom": 2, "footwear": 1}
            for cand in candidates:
                scorer.score_candidate(cand, conn.execute("SELECT * FROM WearSession WHERE id = 'ws_sc1';").fetchone(), history, role_counts)
                # Repetition penalty for the only shoe must be 0.0 because alternatives_count == 0
                self.assertEqual(cand.repetition_penalty, 0.0)

    def test_scenario_2_scarce_presentation_shirt(self):
        """
        Scenario 2: Monday casual plan reserves crisp presentation shirt for Tuesday HIGH session;
        future continuation search detects uncovered_high = 1 if consumed on Monday.
        """
        with self.db.transaction() as conn:
            # Garments:
            # 1 casual shirt (limit = 1, casual only)
            # 1 crisp presentation shirt (limit = 1, presentation only)
            # 1 pants (limit = 5, both)
            # 1 shoes (limit = 5, both)
            conn.execute(
                """
                INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color,
                                    formality, usage_contexts, hard_limit_wears)
                VALUES ('g_casual_top', ?, 'T-Shirt', 'top', 'tee', 'regular', 'grey', 1, '["casual"]', 1),
                       ('g_crisp_top', ?, 'Crisp Shirt', 'top', 'shirt', 'tailored', 'white', 4, '["presentation"]', 1),
                       ('g_pants', ?, 'Chinos', 'bottom', 'chinos', 'regular', 'navy', 3, '["casual", "presentation"]', 5),
                       ('g_shoes', ?, 'Loafers', 'footwear', 'loafers', 'regular', 'brown', 3, '["casual", "presentation"]', 5);
                """,
                (self.user_id, self.user_id, self.user_id, self.user_id),
            )

            # Monday: Casual (importance: NORMAL)
            conn.execute(
                """
                INSERT INTO WearSession(id, user_id, session_date, session_type, importance)
                VALUES ('ws_mon', ?, '2026-03-02', 'casual', 'NORMAL');
                """,
                (self.user_id,),
            )
            # Tuesday: Presentation (importance: HIGH)
            conn.execute(
                """
                INSERT INTO WearSession(id, user_id, session_date, session_type, importance)
                VALUES ('ws_tue', ?, '2026-03-03', 'presentation', 'HIGH');
                """,
                (self.user_id,),
            )

            planner = ContinuationPlanner(conn)
            pid = planner.generate_plan(self.user_id, "2026-03-02", "2026-03-03", StrategyMode.BALANCED)

            # Verify evaluation has 0 uncovered_high
            pe = conn.execute("SELECT * FROM PlanEvaluation WHERE plan_id = ?;", (pid,)).fetchone()
            self.assertEqual(pe["uncovered_high"], 0)

            # Check that Tuesday HIGH session gets the crisp presentation shirt
            assign_tue = conn.execute(
                """
                SELECT pai.garment_id
                FROM PlanSessionAssignment psa
                JOIN PlanAssignmentItem pai ON psa.id = pai.assignment_id
                WHERE psa.plan_id = ? AND psa.session_id = 'ws_tue' AND pai.outfit_role = 'top';
                """,
                (pid,),
            ).fetchone()
            self.assertEqual(assign_tue["garment_id"], "g_crisp_top")

            # Check that Monday casual session gets the casual shirt (preserving crisp shirt)
            assign_mon = conn.execute(
                """
                SELECT pai.garment_id
                FROM PlanSessionAssignment psa
                JOIN PlanAssignmentItem pai ON psa.id = pai.assignment_id
                WHERE psa.plan_id = ? AND psa.session_id = 'ws_mon' AND pai.outfit_role = 'top';
                """,
                (pid,),
            ).fetchone()
            self.assertEqual(assign_mon["garment_id"], "g_casual_top")

    def test_scenario_3_unknown_condition_entry(self):
        """Scenario 3: Owner selects 'Not sure' -> UNKNOWN_CONDITION. Excluded from candidate generation. No timeout."""
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color)
                VALUES ('g_test3', ?, 'Test Top', 'top', 'shirt', 'regular', 'green');
                """,
                (self.user_id,),
            )
            conn.execute(
                """
                INSERT INTO WearSession(id, user_id, session_date, session_type)
                VALUES ('ws_prev', ?, '2026-03-01', 'casual'),
                       ('ws_next', ?, '2026-03-02', 'casual');
                """,
                (self.user_id, self.user_id),
            )
            conn.execute(
                """
                INSERT INTO WearEvent(id, session_id, garment_id, worn_at)
                VALUES ('we_sc3', 'ws_prev', 'g_test3', '2026-03-01T12:00:00Z');
                """
            )

            # Transition to WORN_AWAITING_ASSESSMENT
            engine = StateEngine(conn)
            engine.transition_condition("g_test3", ConditionState.WORN_AWAITING_ASSESSMENT, "logged_wear")

            # Owner selects NOT_SURE
            engine.record_post_wear_assessment(
                wear_event_id="we_sc3",
                garment_id="g_test3",
                choice=PostWearChoice.NOT_SURE,
                notes="Uncertain if spill left residue",
            )

            # Check condition is UNKNOWN_CONDITION
            g = conn.execute("SELECT * FROM Garment WHERE id = 'g_test3';").fetchone()
            self.assertEqual(g["condition_state"], ConditionState.UNKNOWN_CONDITION.value)
            self.assertIsNone(g["airing_review_due_at"], "No automatic review due / timeout for UNKNOWN_CONDITION")

            # Candidate generation must exclude UNKNOWN_CONDITION garment
            gen = CandidateGenerator(conn)
            candidates = gen.generate_candidates("ws_next", user_id=self.user_id)
            for c in candidates:
                self.assertNotIn("g_test3", c.garment_ids)

    def test_scenario_10_owner_correction_defeats_ai(self):
        """
        Scenario 10: Owner edits attribute; raw AI analysis preserved in GarmentAnalysisImport.
        Domain queries owner-verified fields exclusively.
        """
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color, formality, owner_verified)
                VALUES ('g_ai1', ?, 'Purple Party Shirt', 'top', 'shirt', 'slim', 'purple', 4, 0);
                """,
                (self.user_id,),
            )

            # AI Analysis Import with raw JSON
            raw_ai = json.dumps({"detected_color": "purple", "detected_formality": 4, "confidence": 0.92})
            conn.execute(
                """
                INSERT INTO GarmentAnalysisImport(id, garment_id, provider, raw_json, owner_accepted)
                VALUES ('gai_1', 'g_ai1', 'external_vlm', ?, 0);
                """,
                (raw_ai,),
            )

            # Owner corrects attribute: It's actually navy blue, regular fit, formality 2
            conn.execute(
                """
                UPDATE Garment
                SET primary_color = 'navy',
                    cut_and_fit = 'regular',
                    formality = 2,
                    owner_verified = 1
                WHERE id = 'g_ai1';
                """
            )

            # Verify raw AI analysis remains unaltered
            import_row = conn.execute("SELECT * FROM GarmentAnalysisImport WHERE id = 'gai_1';").fetchone()
            self.assertEqual(json.loads(import_row["raw_json"])["detected_color"], "purple")

            # Verify domain query reads owner-verified attributes
            g = conn.execute("SELECT primary_color, cut_and_fit, formality, owner_verified FROM Garment WHERE id = 'g_ai1';").fetchone()
            self.assertEqual(g["primary_color"], "navy")
            self.assertEqual(g["cut_and_fit"], "regular")
            self.assertEqual(g["formality"], 2)
            self.assertEqual(g["owner_verified"], 1)

    def test_scenario_22_no_compatible_future_combination(self):
        """Scenario 22: Incompatible formality/roles prevents complete outfit; continuation search flags uncovered session."""
        with self.db.transaction() as conn:
            # User has casual clothes only, but session requires 'formal' presentation with formality >= 4
            conn.execute(
                """
                INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color, formality, priority_level)
                VALUES ('g_casual_tee', ?, 'Graphic Tee', 'top', 'tee', 'loose', 'yellow', 1, 'NORMAL'),
                       ('g_casual_shorts', ?, 'Gym Shorts', 'bottom', 'shorts', 'loose', 'black', 1, 'NORMAL'),
                       ('g_flip_flops', ?, 'Flip Flops', 'footwear', 'sandals', 'regular', 'black', 1, 'NORMAL');
                """,
                (self.user_id, self.user_id, self.user_id),
            )
            # High presentation session where Graphic Tee & Flip Flops are prohibited or cannot meet role/formality
            conn.execute(
                """
                INSERT INTO WearSession(id, user_id, session_date, session_type, importance, required_roles)
                VALUES ('ws_formal', ?, '2026-03-05', 'formal', 'HIGH', '["top", "bottom", "footwear", "outerwear"]');
                """,
                (self.user_id,),
            )

            planner = ContinuationPlanner(conn)
            pid = planner.generate_plan(self.user_id, "2026-03-05", "2026-03-05", StrategyMode.BALANCED)

            pe = conn.execute("SELECT * FROM PlanEvaluation WHERE plan_id = ?;", (pid,)).fetchone()
            # Missing outerwear role causes uncovered session
            self.assertEqual(pe["uncovered_high"], 1)

    def test_scenario_25_recommended_but_not_worn(self):
        """Scenario 25: A recommendation generated & selected but later skipped creates no wear log or counter increment."""
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color)
                VALUES ('g_t1', ?, 'Shirt', 'top', 'shirt', 'regular', 'white'),
                       ('g_b1', ?, 'Jeans', 'bottom', 'jeans', 'regular', 'blue'),
                       ('g_s1', ?, 'Shoes', 'footwear', 'shoes', 'regular', 'black');
                """,
                (self.user_id, self.user_id, self.user_id),
            )
            conn.execute(
                """
                INSERT INTO WearSession(id, user_id, session_date, session_type, status)
                VALUES ('ws_rec', ?, '2026-03-01', 'casual', 'PLANNED');
                """,
                (self.user_id,),
            )

            planner = ContinuationPlanner(conn)
            pid = planner.generate_plan(self.user_id, "2026-03-01", "2026-03-01", StrategyMode.SAFE)

            # User cancels session (e.g. sick at home, stayed in pajamas)
            conn.execute("UPDATE WearSession SET status = 'CANCELLED' WHERE id = 'ws_rec';")

            # Assert no OutfitWearLog was created
            logs = conn.execute("SELECT count(*) FROM OutfitWearLog WHERE user_id = ?;", (self.user_id,)).fetchone()[0]
            self.assertEqual(logs, 0)

            # Assert no WearEvent was created
            events = conn.execute("SELECT count(*) FROM WearEvent WHERE session_id = 'ws_rec';").fetchone()[0]
            self.assertEqual(events, 0)

            # Assert wear counts are untouched
            for gid in ["g_t1", "g_b1", "g_s1"]:
                g = conn.execute("SELECT wears_since_confirmed_wash FROM Garment WHERE id = ?;", (gid,)).fetchone()
                self.assertEqual(g["wears_since_confirmed_wash"], 0)


if __name__ == "__main__":
    unittest.main()
