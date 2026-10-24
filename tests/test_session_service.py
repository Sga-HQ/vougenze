"""
Unit tests for core.session_service.
Validates Scenarios 13, 14, and horizon routine materialization.
"""
import unittest
from core.db import Database
from core.models import PlanStatus, InvalidationReason
from core.session_service import SessionService


class TestSessionService(unittest.TestCase):

    def setUp(self):
        self.db = Database(":memory:")
        self.db.migrate()
        self.user_id = "u1"

        with self.db.transaction() as conn:
            conn.execute("INSERT INTO User(id, display_name) VALUES (?, 'Test User');", (self.user_id,))

    def test_multi_session_day_and_materialization(self):
        """Scenario 13: 3 sessions on Monday (university, casual, party) materialized from template."""
        with self.db.transaction() as conn:
            service = SessionService(conn)
            tid = service.create_template(
                user_id=self.user_id,
                template_name="University & Social Routine",
                effective_from="2026-03-01",
            )

            # Monday = 0: 3 distinct sessions
            service.add_template_day(
                template_id=tid,
                day_of_week=0,
                session_type="university",
                importance="HIGH",
                start_time="09:00",
                expected_duration_hours=6.0,
                sort_order=1,
            )
            service.add_template_day(
                template_id=tid,
                day_of_week=0,
                session_type="casual",
                importance="NORMAL",
                start_time="16:00",
                expected_duration_hours=3.0,
                sort_order=2,
            )
            service.add_template_day(
                template_id=tid,
                day_of_week=0,
                session_type="party",
                importance="HIGH",
                start_time="20:00",
                expected_duration_hours=4.0,
                sort_order=3,
            )

            # Materialize for Monday 2026-03-02
            session_ids = service.materialize_sessions_for_horizon(
                user_id=self.user_id,
                start_date="2026-03-02",
                end_date="2026-03-02",
                template_id=tid,
            )

            self.assertEqual(len(session_ids), 3, "Monday must have 3 distinct sessions")

            sessions = service.get_horizon_sessions(self.user_id, "2026-03-02", "2026-03-02")
            types = [s["session_type"] for s in sessions]
            self.assertEqual(types, ["university", "casual", "party"])

    def test_event_injection_invalidation(self):
        """Scenario 14: Ad-hoc mid-week event injection invalidates proposed plan with EVENT_ADDED_OR_MODIFIED."""
        with self.db.transaction() as conn:
            # Create existing proposed plan for the week
            conn.execute(
                """
                INSERT INTO RecommendationPlan(
                    id, user_id, strategy_mode, horizon_start_date, horizon_end_date, status
                )
                VALUES ('rp_week1', ?, 'BALANCED', '2026-03-02', '2026-03-08', 'PROPOSED');
                """,
                (self.user_id,),
            )

            service = SessionService(conn)
            # Inject unexpected high-importance presentation on Wednesday
            service.create_session(
                user_id=self.user_id,
                session_date="2026-03-04",
                session_type="presentation",
                importance="HIGH",
            )

            # Check that rp_week1 was superseded with EVENT_ADDED_OR_MODIFIED
            plan = conn.execute("SELECT * FROM RecommendationPlan WHERE id = 'rp_week1';").fetchone()
            self.assertEqual(plan["status"], PlanStatus.SUPERSEDED.value)
            self.assertEqual(plan["invalidation_reason"], InvalidationReason.EVENT_ADDED_OR_MODIFIED.value)


if __name__ == "__main__":
    unittest.main()
