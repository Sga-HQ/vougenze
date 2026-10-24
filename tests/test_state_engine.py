"""
Unit tests for core.state_engine.
Validates state machine transitions, orthogonal dimensions, and wash semantics.
"""
import unittest
from core.db import Database
from core.models import ConditionState, AvailabilityState, CleaningScope, PostWearChoice
from core.state_engine import StateEngine, InvalidStateTransitionError


class TestStateEngine(unittest.TestCase):

    def setUp(self):
        self.db = Database(":memory:")
        self.db.migrate()
        self.user_id = "u1"
        self.garment_id = "g1"

        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO User(id, display_name) VALUES (?, 'Test User');",
                (self.user_id,),
            )
            conn.execute(
                """
                INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color)
                VALUES (?, ?, 'Oxford Shirt', 'top', 'shirt', 'regular', 'white');
                """,
                (self.garment_id, self.user_id),
            )

    def test_condition_lifecycle_and_audit_trail(self):
        with self.db.transaction() as conn:
            engine = StateEngine(conn)

            # CLEAN -> WORN_AWAITING_ASSESSMENT
            engine.transition_condition(
                self.garment_id,
                ConditionState.WORN_AWAITING_ASSESSMENT,
                trigger_event="logged_wear",
            )
            g = engine.get_garment(self.garment_id)
            self.assertEqual(g["condition_state"], ConditionState.WORN_AWAITING_ASSESSMENT.value)

            # WORN_AWAITING_ASSESSMENT -> AIRING
            engine.transition_condition(
                self.garment_id,
                ConditionState.AIRING,
                trigger_event="owner_assessment_needs_airing",
            )
            g = engine.get_garment(self.garment_id)
            self.assertEqual(g["condition_state"], ConditionState.AIRING.value)

            # AIRING -> WORN_REUSABLE
            engine.transition_condition(
                self.garment_id,
                ConditionState.WORN_REUSABLE,
                trigger_event="airing_inspection_passed",
            )
            g = engine.get_garment(self.garment_id)
            self.assertEqual(g["condition_state"], ConditionState.WORN_REUSABLE.value)

            # WORN_REUSABLE -> NEEDS_WASH
            engine.transition_condition(
                self.garment_id,
                ConditionState.NEEDS_WASH,
                trigger_event="owner_found_odor",
            )
            g = engine.get_garment(self.garment_id)
            self.assertEqual(g["condition_state"], ConditionState.NEEDS_WASH.value)

            # Check audit transitions
            cursor = conn.execute(
                "SELECT count(*) FROM GarmentStateTransition WHERE garment_id = ? AND state_dimension = 'condition';",
                (self.garment_id,),
            )
            self.assertEqual(cursor.fetchone()[0], 4)

    def test_availability_dimension_preserves_condition(self):
        """Scenario 16: Garment with condition NEEDS_WASH sent to REPAIR returns as NEEDS_WASH."""
        with self.db.transaction() as conn:
            engine = StateEngine(conn)

            # Mark garment NEEDS_WASH
            engine.transition_condition(
                self.garment_id,
                ConditionState.NEEDS_WASH,
                trigger_event="manual_dirty",
            )

            # Send to REPAIR
            engine.transition_availability(
                self.garment_id,
                AvailabilityState.REPAIR,
                trigger_event="sent_for_mending",
                unavailability_reason="Torn seam",
            )
            g = engine.get_garment(self.garment_id)
            self.assertEqual(g["availability_state"], AvailabilityState.REPAIR.value)
            self.assertEqual(g["condition_state"], ConditionState.NEEDS_WASH.value)

            # Return from REPAIR to AVAILABLE
            engine.transition_availability(
                self.garment_id,
                AvailabilityState.AVAILABLE,
                trigger_event="mending_completed",
            )
            g = engine.get_garment(self.garment_id)
            self.assertEqual(g["availability_state"], AvailabilityState.AVAILABLE.value)
            # Invariant: condition MUST still be NEEDS_WASH!
            self.assertEqual(g["condition_state"], ConditionState.NEEDS_WASH.value)

    def test_full_clean_vs_partial_clean_wash(self):
        """Scenario 11 & Scenario 19: Hand-wash vs Spot-cleaning."""
        with self.db.transaction() as conn:
            engine = StateEngine(conn)

            # Increment wear count and set condition NEEDS_WASH
            conn.execute(
                "UPDATE Garment SET wears_since_confirmed_wash = 3, condition_state = 'NEEDS_WASH' WHERE id = ?;",
                (self.garment_id,),
            )

            # Scenario 19: Spot clean (PARTIAL_CLEAN)
            engine.record_wash_event(
                garment_id=self.garment_id,
                user_id=self.user_id,
                cleaning_scope=CleaningScope.PARTIAL_CLEAN,
                method="spot_clean",
            )
            g = engine.get_garment(self.garment_id)
            # Must NOT reset wear count or transition to CLEAN
            self.assertEqual(g["wears_since_confirmed_wash"], 3)
            self.assertEqual(g["condition_state"], ConditionState.NEEDS_WASH.value)

            # Scenario 11: Full clean hand wash (FULL_CLEAN)
            engine.record_wash_event(
                garment_id=self.garment_id,
                user_id=self.user_id,
                cleaning_scope=CleaningScope.FULL_CLEAN,
                method="hand_wash",
            )
            g = engine.get_garment(self.garment_id)
            # Must reset wear count to 0 and transition to CLEAN
            self.assertEqual(g["wears_since_confirmed_wash"], 0)
            self.assertEqual(g["condition_state"], ConditionState.CLEAN.value)

    def test_illegal_transition_rejection(self):
        with self.db.transaction() as conn:
            engine = StateEngine(conn)
            # CLEAN -> AIRING directly is illegal (must pass through wear event / assessment)
            with self.assertRaises(InvalidStateTransitionError):
                engine.transition_condition(
                    self.garment_id,
                    ConditionState.AIRING,
                    trigger_event="unauthorized_jump",
                )


if __name__ == "__main__":
    unittest.main()
