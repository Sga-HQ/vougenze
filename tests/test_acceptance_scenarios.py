"""
Acceptance Suite for representative Scenarios 1, 2, 3, 10, 22, 22b, and 25.
Mechanically verifies opportunity-cost reasoning, owner overrides, formality constraints, and skipped sessions.
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
        Scenario 2: Opportunity-cost reasoning. Both shirts are feasible on Monday, with the special shirt
        preferred on Monday (like_rating 8 > 5). But the special shirt is uniquely feasible on Tuesday (HIGH).
        Using the special shirt on Monday hits its hard limit (1/1), leaving Tuesday uncovered (uncovered_high=1).
        The planner sacrifices Monday preference quality to preserve the special shirt for Tuesday (uncovered_high=0).
        """
        import itertools
        from core.candidate_generator import CandidateGenerator
        from core.scorers import DeterministicScorer, ScoringWeights
        from core.planner import PartialPlan

        with self.db.transaction() as conn:
            # Occasion profiles:
            # - Casual: formality 1-4
            # - Presentation: formality 4-5
            conn.execute(
                """
                INSERT INTO OccasionProfile(id, user_id, name, formality_min, formality_max)
                VALUES ('op_cas', ?, 'casual', 1, 4),
                       ('op_pres', ?, 'presentation', 4, 5);
                """,
                (self.user_id, self.user_id),
            )

            # Garments:
            # 1 casual shirt (formality=2, like=5, casual only, hard_limit=1)
            # 1 crisp shirt (formality=4, like=8, both casual & presentation feasible, hard_limit=1)
            # 1 pants (formality=4, like=5, hard_limit=5)
            # 1 shoes (formality=4, like=5, hard_limit=5)
            conn.execute(
                """
                INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color,
                                    formality, like_rating, usage_contexts, hard_limit_wears, wears_since_confirmed_wash)
                VALUES ('g_casual_top', ?, 'Ordinary Top', 'top', 'tee', 'regular', 'grey', 2, 5, '["casual"]', 1, 0),
                       ('g_crisp_top', ?, 'Special Crisp Shirt', 'top', 'shirt', 'tailored', 'white', 4, 8, '["casual", "presentation"]', 1, 0),
                       ('g_pants', ?, 'Chinos', 'bottom', 'chinos', 'regular', 'navy', 4, 5, '["casual", "presentation"]', 5, 0),
                       ('g_shoes', ?, 'Loafers', 'footwear', 'loafers', 'regular', 'brown', 4, 5, '["casual", "presentation"]', 5, 0);
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

            gen = CandidateGenerator(conn)
            cands_mon = gen.generate_candidates("ws_mon", user_id=self.user_id)
            cands_tue = gen.generate_candidates("ws_tue", user_id=self.user_id)

            # Verify: Both shirts are feasible for Monday
            mon_tops = {c.roles["top"] for c in cands_mon}
            self.assertIn("g_casual_top", mon_tops)
            self.assertIn("g_crisp_top", mon_tops, "Special crisp shirt must be feasible on Monday")

            # Verify: Only crisp shirt is feasible for Tuesday
            tue_tops = {c.roles["top"] for c in cands_tue}
            self.assertNotIn("g_casual_top", tue_tops, "Ordinary shirt must be infeasible on Tuesday")
            self.assertIn("g_crisp_top", tue_tops, "Crisp shirt must be uniquely feasible on Tuesday")

            # Run ContinuationPlanner beam search
            planner = ContinuationPlanner(conn)
            pid = planner.generate_plan(self.user_id, "2026-03-02", "2026-03-03", StrategyMode.SAFE)

            # Verify evaluation has 0 uncovered_high
            pe = conn.execute("SELECT * FROM PlanEvaluation WHERE plan_id = ?;", (pid,)).fetchone()
            self.assertEqual(pe["uncovered_high"], 0, "HIGH session on Tuesday must be covered")

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
        Candidate generation, scoring, and snapshot capture strictly use owner-verified fields.
        """
        from core.candidate_generator import CandidateGenerator
        from core.evidence_service import PreferenceEvidenceService

        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color, formality, owner_verified)
                VALUES ('g_ai1', ?, 'Purple Party Shirt', 'top', 'shirt', 'slim', 'purple', 4, 0),
                       ('g_bot_10', ?, 'Trousers', 'bottom', 'trousers', 'regular', 'black', 4, 1),
                       ('g_shoe_10', ?, 'Leather Shoes', 'footwear', 'shoes', 'regular', 'black', 4, 1);
                """,
                (self.user_id, self.user_id, self.user_id),
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

            # Occasion profiles:
            # Formal profile requiring formality 4-5
            # Casual profile requiring formality 1-4
            conn.execute(
                """
                INSERT INTO OccasionProfile(id, user_id, name, formality_min, formality_max)
                VALUES ('op_formal_10', ?, 'formal_event', 4, 5),
                       ('op_casual_10', ?, 'casual_outing', 1, 4);
                """,
                (self.user_id, self.user_id),
            )

            conn.execute(
                """
                INSERT INTO WearSession(id, user_id, session_date, session_type, importance)
                VALUES ('ws_formal_10', ?, '2026-03-05', 'formal_event', 'HIGH'),
                       ('ws_casual_10', ?, '2026-03-06', 'casual_outing', 'NORMAL');
                """,
                (self.user_id, self.user_id),
            )

            # Before owner edit: AI detected formality 4, so it would qualify for formal_event
            gen = CandidateGenerator(conn)
            pre_cands = gen.generate_candidates("ws_formal_10", user_id=self.user_id)
            self.assertTrue(any("g_ai1" in c.garment_ids for c in pre_cands))

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

            # 1. Candidate Generation: Now excluded from formal session (formality 2 < 4)
            post_formal = gen.generate_candidates("ws_formal_10", user_id=self.user_id)
            self.assertFalse(any("g_ai1" in c.garment_ids for c in post_formal), "Owner formality 2 must exclude garment from formal session")

            # But included in casual session (formality 2 is within 1-3)
            post_casual = gen.generate_candidates("ws_casual_10", user_id=self.user_id)
            self.assertTrue(any("g_ai1" in c.garment_ids for c in post_casual), "Owner formality 2 must include garment in casual session")

            # 2. Snapshot capture: Snapshot item must record owner-verified attributes
            ev_svc = PreferenceEvidenceService(conn)
            casual_cand = [c for c in post_casual if "g_ai1" in c.garment_ids][0]
            snap_id = ev_svc.create_candidate_snapshot(self.user_id, casual_cand, "ws_casual_10")
            item_row = conn.execute(
                "SELECT garment_snapshot FROM OutfitCandidateSnapshotItem WHERE candidate_id = ? AND garment_id = 'g_ai1';",
                (snap_id,),
            ).fetchone()
            snap_data = json.loads(item_row["garment_snapshot"])
            self.assertEqual(snap_data["primary_color"], "navy")
            self.assertEqual(snap_data["formality"], 2)
            self.assertEqual(snap_data["owner_verified"], 1)

            # 3. Raw AI analysis in GarmentAnalysisImport remains intact
            import_row = conn.execute("SELECT * FROM GarmentAnalysisImport WHERE id = 'gai_1';").fetchone()
            self.assertEqual(json.loads(import_row["raw_json"])["detected_color"], "purple")
            self.assertEqual(json.loads(import_row["raw_json"])["detected_formality"], 4)

    def test_scenario_22_missing_required_role_causes_uncovered_high_session(self):
        """Scenario 22: Missing required role (outerwear) prevents complete outfit; continuation search flags uncovered session."""
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color, formality, priority_level)
                VALUES ('g_casual_tee', ?, 'Graphic Tee', 'top', 'tee', 'loose', 'yellow', 1, 'NORMAL'),
                       ('g_casual_shorts', ?, 'Gym Shorts', 'bottom', 'shorts', 'loose', 'black', 1, 'NORMAL'),
                       ('g_flip_flops', ?, 'Flip Flops', 'footwear', 'sandals', 'regular', 'black', 1, 'NORMAL');
                """,
                (self.user_id, self.user_id, self.user_id),
            )
            # Session requires outerwear but user has none
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
            self.assertEqual(pe["uncovered_high"], 1, "Missing required outerwear must flag uncovered_high")

    def test_scenario_22b_incompatible_formality_prohibition_causes_uncovered_high_session(self):
        """Scenario 22b: All required roles exist, but incompatible formality (all formality 1 vs required 4-5) flags uncovered session."""
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO OccasionProfile(id, user_id, name, formality_min, formality_max)
                VALUES ('op_black_tie', ?, 'black_tie', 4, 5);
                """,
                (self.user_id,),
            )
            # Wardrobe contains all roles, but ONLY casual (formality 1)
            conn.execute(
                """
                INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color, formality)
                VALUES ('g_beach_tee', ?, 'Beach Tee', 'top', 'tee', 'regular', 'yellow', 1),
                       ('g_swim_shorts', ?, 'Swim Shorts', 'bottom', 'shorts', 'regular', 'red', 1),
                       ('g_sandals', ?, 'Sandals', 'footwear', 'sandals', 'regular', 'brown', 1);
                """,
                (self.user_id, self.user_id, self.user_id),
            )
            # HIGH session requires black_tie (formality 4-5) with roles top, bottom, footwear
            conn.execute(
                """
                INSERT INTO WearSession(id, user_id, session_date, session_type, importance, required_roles)
                VALUES ('ws_black_tie', ?, '2026-03-05', 'black_tie', 'HIGH', '["top", "bottom", "footwear"]');
                """,
                (self.user_id,),
            )

            planner = ContinuationPlanner(conn)
            pid = planner.generate_plan(self.user_id, "2026-03-05", "2026-03-05", StrategyMode.BALANCED)

            pe = conn.execute("SELECT * FROM PlanEvaluation WHERE plan_id = ?;", (pid,)).fetchone()
            self.assertEqual(pe["uncovered_high"], 1, "Formality incompatibility must flag uncovered_high even when all roles are present")

    def test_scenario_25_recommended_but_not_worn(self):
        """
        Scenario 25: Recommendation created and reserved, then user records SKIPPED/NOT_WORN outcome.
        Verified:
        - no WearEvent created
        - no wear counter incremented
        - plan authorization reservation released
        - preference evidence records non-penalizing skipped outcome distinguishing it from rejection
        """
        from core.session_service import SessionService
        from core.evidence_service import PreferenceEvidenceService

        with self.db.transaction() as conn:
            # 1 limited garment with hard_limit = 1, wear count = 1
            conn.execute(
                """
                INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color,
                                    hard_limit_wears, wears_since_confirmed_wash)
                VALUES ('g_t1', ?, 'Shirt', 'top', 'shirt', 'regular', 'white', 1, 1),
                       ('g_b1', ?, 'Jeans', 'bottom', 'jeans', 'regular', 'blue', 5, 0),
                       ('g_s1', ?, 'Shoes', 'footwear', 'shoes', 'regular', 'black', 5, 0);
                """,
                (self.user_id, self.user_id, self.user_id),
            )
            # Insert available wear authorization for g_t1
            conn.execute(
                """
                INSERT INTO GarmentWearAuthorization(id, garment_id, authorized_by, reason, status, authorized_at)
                VALUES ('gwa_25', 'g_t1', 'owner', 'Extra wear', 'AVAILABLE', '2026-03-01T08:00:00Z');
                """
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

            # Verify authorization was reserved
            pre_res = conn.execute(
                "SELECT count(*) FROM PlanWearAuthorizationReservation WHERE plan_id = ? AND session_id = 'ws_rec';",
                (pid,),
            ).fetchone()[0]
            self.assertEqual(pre_res, 1)

            # User skips session / records NOT_WORN
            session_svc = SessionService(conn)
            session_svc.skip_or_cancel_session("ws_rec", reason="SKIPPED")

            # Record non-penalizing skipped evidence
            plan = planner.get_plan(pid)
            cand = plan.assignments["ws_rec"]
            ev_svc = PreferenceEvidenceService(conn)
            ev_id = ev_svc.record_skipped_evidence(
                user_id=self.user_id,
                session_id="ws_rec",
                occasion_context="casual",
                skipped_candidate=cand,
                skip_reason="USER_STAYED_HOME",
            )

            # Assert no OutfitWearLog was created
            logs = conn.execute("SELECT count(*) FROM OutfitWearLog WHERE user_id = ?;", (self.user_id,)).fetchone()[0]
            self.assertEqual(logs, 0)

            # Assert no WearEvent was created
            events = conn.execute("SELECT count(*) FROM WearEvent WHERE session_id = 'ws_rec';").fetchone()[0]
            self.assertEqual(events, 0)

            # Assert wear counts are untouched
            g = conn.execute("SELECT wears_since_confirmed_wash FROM Garment WHERE id = 'g_t1';").fetchone()
            self.assertEqual(g["wears_since_confirmed_wash"], 1)

            # Assert reservation was released
            post_res = conn.execute(
                "SELECT count(*) FROM PlanWearAuthorizationReservation WHERE plan_id = ? AND session_id = 'ws_rec';",
                (pid,),
            ).fetchone()[0]
            self.assertEqual(post_res, 0, "Authorization reservation must be released upon skip")

            # Assert preference evidence recorded skipped outcome distinguishing it from rejection
            ev_row = conn.execute("SELECT evidence_type, rejection_reason, constraint_flags FROM PreferenceEvidence WHERE id = ?;", (ev_id,)).fetchone()
            self.assertEqual(ev_row["evidence_type"], "skipped")
            self.assertEqual(ev_row["rejection_reason"], "USER_STAYED_HOME")
            flags = json.loads(ev_row["constraint_flags"])
            self.assertEqual(flags.get("non_penalizing"), 1)


if __name__ == "__main__":
    unittest.main()
