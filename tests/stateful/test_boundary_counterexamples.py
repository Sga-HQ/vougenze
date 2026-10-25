"""
Targeted Hypothesis Counterexample Verification for Known Defects:
1. VZ-W2: Missing modification reason enforcement during modified wear logging.
2. Scenario 4: Laundry-delay plan supersession consequence absent from mark_batch_delayed.
"""
from __future__ import annotations
import os
import shutil
import tempfile
import unittest
from hypothesis import given, settings, HealthCheck
import hypothesis.strategies as st

from core.db import Database
from core.models import ConditionState, AvailabilityState, PlanStatus
from core.state_engine import StateEngine
from core.wear_service import WearService
from core.laundry_service import LaundryService
import tests.strategies.domain_strategies as ds


class TestBoundaryCounterexamples(unittest.TestCase):
    """
    Executes generative property attacks specifically designed to expose
    classified defects and record reproducible minimized counterexamples.
    """

    def setUp(self):
        self.db = Database(":memory:")
        self.db.migrate()
        self.conn = self.db.get_connection()
        self.user_id = "u_counterexample"

        self.state_engine = StateEngine(self.conn)
        self.wear_service = WearService(self.conn)
        self.laundry_service = LaundryService(self.conn)

        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO User(id, display_name, timezone_id, active_history_window) VALUES (?, 'Test User', 'UTC', 100);",
                (self.user_id,),
            )
            # Seed basic garments
            conn.execute(
                """
                INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit,
                                    primary_color, formality, usage_contexts, hard_limit_wears,
                                    condition_state, availability_state, fills_roles, owner_verified)
                VALUES ('g_top_1', ?, 'Top 1', 'top', 'shirt', 'regular', 'white', 2, '["casual"]', 5, 'CLEAN', 'AVAILABLE', '["top"]', 1),
                       ('g_bot_1', ?, 'Bottom 1', 'bottom', 'jeans', 'regular', 'blue', 2, '["casual"]', 5, 'CLEAN', 'AVAILABLE', '["bottom"]', 1);
                """,
                (self.user_id, self.user_id),
            )
            # Create planned session
            conn.execute(
                """
                INSERT INTO WearSession(id, user_id, session_date, session_type, importance, status)
                VALUES ('sess_test', ?, '2026-03-05', 'casual', 'NORMAL', 'PLANNED');
                """,
                (self.user_id,),
            )

    def tearDown(self):
        self.db.close()

    @given(blank_reason=st.sampled_from([None, "", "   ", "\t\n"]))
    @settings(max_examples=10, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    def test_vzw2_modification_reason_counterexample(self, blank_reason):
        """
        Regression for VZ-W2:
        Governed Expectation: was_modified_before_wear = True MUST require a non-empty, non-whitespace modification_reason.
        Current Code Behavior: raises ValueError when was_modified_before_wear = True with blank or whitespace reason.
        """
        import uuid
        top_id = f"g_top_{uuid.uuid4().hex[:8]}"
        bot_id = f"g_bot_{uuid.uuid4().hex[:8]}"
        sess_id = f"sess_{uuid.uuid4().hex[:8]}"
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit,
                                    primary_color, formality, usage_contexts, hard_limit_wears,
                                    condition_state, availability_state, fills_roles, owner_verified)
                VALUES (?, ?, 'Top', 'top', 'shirt', 'regular', 'white', 2, '["casual"]', 5, 'CLEAN', 'AVAILABLE', '["top"]', 1),
                       (?, ?, 'Bottom', 'bottom', 'jeans', 'regular', 'blue', 2, '["casual"]', 5, 'CLEAN', 'AVAILABLE', '["bottom"]', 1);
                """,
                (top_id, self.user_id, bot_id, self.user_id),
            )
            conn.execute(
                """
                INSERT INTO WearSession(id, user_id, session_date, session_type, importance, status)
                VALUES (?, ?, '2026-03-05', 'casual', 'NORMAL', 'PLANNED');
                """,
                (sess_id, self.user_id),
            )

        roles = {"top": top_id, "bottom": bot_id}
        with self.assertRaises(ValueError):
            with self.db.transaction() as conn:
                self.wear_service.log_outfit_wear(
                    user_id=self.user_id,
                    session_id=sess_id,
                    garment_roles=roles,
                    was_recommended=True,
                    was_modified_before_wear=True,
                    modification_reason=blank_reason,
                )

    @unittest.expectedFailure
    def test_laundry_delay_plan_consequence_counterexample(self):
        """
        Attacks Scenario 4 Laundry Delay:
        Governed Expectation: When a laundry batch containing garment g is delayed past session date s,
        the proposed plan for s utilizing g must be superseded with LAUNDRY_DELAYED.
        Current Code Behavior: LaundryBatch is updated to DELAYED, but RecommendationPlan remains PROPOSED.
        """
        with self.db.transaction() as conn:
            # Dispatch g_top_1 to laundry
            conn.execute("UPDATE Garment SET condition_state = 'NEEDS_WASH' WHERE id = 'g_top_1';")
            batch_id = self.laundry_service.send_laundry_batch(
                user_id=self.user_id,
                garment_ids=["g_top_1"],
                sent_date="2026-03-01",
                expected_return_date="2026-03-04",
            )
            # Create a proposed recommendation plan for session on 2026-03-05
            plan_id = "plan_dep_test"
            conn.execute(
                """
                INSERT INTO RecommendationPlan(id, user_id, strategy_mode, horizon_start_date, horizon_end_date, status)
                VALUES (?, ?, 'BALANCED', '2026-03-05', '2026-03-05', 'PROPOSED');
                """,
                (plan_id, self.user_id),
            )
            conn.execute(
                """
                INSERT INTO PlanSessionAssignment(id, plan_id, session_id, assignment_status)
                VALUES ('psa_1', ?, 'sess_test', 'PROPOSED');
                """,
                (plan_id,),
            )
            conn.execute(
                """
                INSERT INTO PlanAssignmentItem(assignment_id, garment_id, outfit_role)
                VALUES ('psa_1', 'g_top_1', 'top');
                """,
            )

        # Execute delay past session date (return expected 2026-03-10, session is 2026-03-05)
        with self.db.transaction() as conn:
            self.laundry_service.mark_batch_delayed(
                batch_id=batch_id,
                delay_reason="Logistics delay",
                new_expected_return_date="2026-03-10",
            )

        # Inspect plan status
        cur = self.conn.execute("SELECT status, invalidation_reason FROM RecommendationPlan WHERE id = ?;", (plan_id,))
        plan_row = cur.fetchone()

        if plan_row["status"] == "PROPOSED":
            self.fail(
                f"COUNTEREXAMPLE DETECTED (Scenario 4 / Laundry Delay): Batch {batch_id} was delayed to 2026-03-10, "
                f"past dependent session date 2026-03-05, but RecommendationPlan {plan_id} remained status={plan_row['status']} "
                f"(expected SUPERSEDED with invalidation_reason='LAUNDRY_DELAYED')."
            )


if __name__ == "__main__":
    unittest.main()
