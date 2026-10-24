"""
Vougenze State-Transition Engine (Frozen Spec Part 3).
Enforces two orthogonal state dimensions (condition vs availability),
prohibits silent or same-state transitions, disallows un-retirement (terminal RETIRED state),
validates wear event identity in post-wear assessments, and guarantees audit trail in GarmentStateTransition.
"""
from __future__ import annotations
import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, List

from core.models import (
    ConditionState,
    AvailabilityState,
    CleaningScope,
    PostWearChoice,
)


class InvalidStateTransitionError(ValueError):
    """Raised when a state transition violates the state machine invariants."""
    pass


# ==============================================================================
# Allowed State Transitions Graphs
# ==============================================================================

# Condition Dimension:
ALLOWED_CONDITION_TRANSITIONS: Dict[ConditionState, set[ConditionState]] = {
    ConditionState.CLEAN: {
        ConditionState.WORN_AWAITING_ASSESSMENT,  # via logged wear
        ConditionState.UNKNOWN_CONDITION,         # via owner override / unconfirmed state
        ConditionState.NEEDS_WASH,                # via owner manual dirty mark / spill
    },
    ConditionState.WORN_AWAITING_ASSESSMENT: {
        ConditionState.WORN_REUSABLE,             # owner assessment: ready_to_reuse
        ConditionState.AIRING,                    # owner assessment: needs_airing
        ConditionState.NEEDS_WASH,                # owner assessment: needs_wash, stained, wet
        ConditionState.UNKNOWN_CONDITION,         # owner skips or timeout
    },
    ConditionState.WORN_REUSABLE: {
        ConditionState.WORN_AWAITING_ASSESSMENT,  # via logged wear
        ConditionState.AIRING,                    # owner sets to air
        ConditionState.NEEDS_WASH,                # owner notices spot/odor
        ConditionState.UNKNOWN_CONDITION,         # owner override
        ConditionState.CLEAN,                     # via wash
    },
    ConditionState.AIRING: {
        ConditionState.WORN_REUSABLE,             # owner confirms airing inspection passed
        ConditionState.NEEDS_WASH,                # owner determines still stale/soiled
        ConditionState.UNKNOWN_CONDITION,         # owner override
        ConditionState.CLEAN,                     # via wash
    },
    ConditionState.NEEDS_WASH: {
        ConditionState.CLEAN,                     # via confirmed WashEvent (FULL_CLEAN)
        ConditionState.UNKNOWN_CONDITION,         # owner override
    },
    ConditionState.UNKNOWN_CONDITION: {
        ConditionState.CLEAN,                     # owner inspection/wash
        ConditionState.WORN_REUSABLE,             # owner inspection confirms clean/reusable
        ConditionState.AIRING,                    # owner hangs to air
        ConditionState.NEEDS_WASH,                # owner decides needs wash
    },
}

# Availability Dimension (RETIRED is terminal in V0):
ALLOWED_AVAILABILITY_TRANSITIONS: Dict[AvailabilityState, set[AvailabilityState]] = {
    AvailabilityState.AVAILABLE: {
        AvailabilityState.IN_LAUNDRY,   # sent in laundry batch
        AvailabilityState.UNAVAILABLE,  # misplaced, packed away
        AvailabilityState.REPAIR,       # tailoring, mending
        AvailabilityState.RETIRED,      # donated, discarded, sold
    },
    AvailabilityState.IN_LAUNDRY: {
        AvailabilityState.AVAILABLE,    # batch returned confirmed
        AvailabilityState.REPAIR,       # returned damaged
        AvailabilityState.UNAVAILABLE,  # missing/disputed
    },
    AvailabilityState.REPAIR: {
        AvailabilityState.AVAILABLE,    # repair completed
        AvailabilityState.RETIRED,      # repair unfeasible
        AvailabilityState.UNAVAILABLE,  # misplaced at tailor
    },
    AvailabilityState.UNAVAILABLE: {
        AvailabilityState.AVAILABLE,    # item found/unpacked
        AvailabilityState.RETIRED,      # item retired
    },
    AvailabilityState.RETIRED: set(),   # Terminal: un-retire is disallowed in V0
}


class StateEngine:
    """Manages garment condition and availability transitions with atomic audit records."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    @staticmethod
    def _utc_now() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def get_garment(self, garment_id: str) -> sqlite3.Row:
        cursor = self.conn.execute("SELECT * FROM Garment WHERE id = ?;", (garment_id,))
        row = cursor.fetchone()
        if not row:
            raise KeyError(f"Garment {garment_id} not found")
        return row

    def transition_condition(
        self,
        garment_id: str,
        to_state: ConditionState,
        trigger_event: str,
        trigger_details: Optional[Dict[str, Any]] = None,
        occurred_at: Optional[str] = None,
        airing_review_due_at: Optional[str] = None,
    ) -> None:
        """
        Transitions a garment's condition state and writes an atomic audit record.
        Preserves availability_state. Rejects same-state transitions.
        """
        garment = self.get_garment(garment_id)
        from_state = ConditionState(garment["condition_state"])

        if from_state == to_state:
            raise InvalidStateTransitionError(
                f"Same-state condition transition rejected for garment '{garment_id}': {from_state.value} -> {to_state.value}"
            )

        allowed = ALLOWED_CONDITION_TRANSITIONS.get(from_state, set())
        if to_state not in allowed:
            raise InvalidStateTransitionError(
                f"Illegal condition transition for garment '{garment_id}': "
                f"{from_state.value} -> {to_state.value} via '{trigger_event}'"
            )

        now = occurred_at or self._utc_now()
        details_json = json.dumps(trigger_details or {})

        self.conn.execute(
            """
            UPDATE Garment
            SET condition_state = ?,
                airing_review_due_at = ?,
                updated_at = ?
            WHERE id = ?;
            """,
            (to_state.value, airing_review_due_at, now, garment_id),
        )

        trans_id = f"gst_{uuid.uuid4().hex[:12]}"
        self.conn.execute(
            """
            INSERT INTO GarmentStateTransition(
                id, garment_id, state_dimension, from_state, to_state,
                trigger_event, trigger_details, occurred_at
            )
            VALUES (?, ?, 'condition', ?, ?, ?, ?, ?);
            """,
            (
                trans_id,
                garment_id,
                from_state.value,
                to_state.value,
                trigger_event,
                details_json,
                now,
            ),
        )

    def transition_availability(
        self,
        garment_id: str,
        to_state: AvailabilityState,
        trigger_event: str,
        trigger_details: Optional[Dict[str, Any]] = None,
        unavailability_reason: Optional[str] = None,
        occurred_at: Optional[str] = None,
    ) -> None:
        """
        Transitions a garment's availability state and writes an atomic audit record.
        Strictly preserves condition_state. Rejects same-state transitions.
        """
        garment = self.get_garment(garment_id)
        from_state = AvailabilityState(garment["availability_state"])

        if from_state == to_state:
            raise InvalidStateTransitionError(
                f"Same-state availability transition rejected for garment '{garment_id}': {from_state.value} -> {to_state.value}"
            )

        allowed = ALLOWED_AVAILABILITY_TRANSITIONS.get(from_state, set())
        if to_state not in allowed:
            raise InvalidStateTransitionError(
                f"Illegal availability transition for garment '{garment_id}': "
                f"{from_state.value} -> {to_state.value} via '{trigger_event}'"
            )

        now = occurred_at or self._utc_now()
        details_json = json.dumps(trigger_details or {})

        self.conn.execute(
            """
            UPDATE Garment
            SET availability_state = ?,
                unavailability_reason = ?,
                updated_at = ?
            WHERE id = ?;
            """,
            (to_state.value, unavailability_reason, now, garment_id),
        )

        trans_id = f"gst_{uuid.uuid4().hex[:12]}"
        self.conn.execute(
            """
            INSERT INTO GarmentStateTransition(
                id, garment_id, state_dimension, from_state, to_state,
                trigger_event, trigger_details, occurred_at
            )
            VALUES (?, ?, 'availability', ?, ?, ?, ?, ?);
            """,
            (
                trans_id,
                garment_id,
                from_state.value,
                to_state.value,
                trigger_event,
                details_json,
                now,
            ),
        )

    def record_post_wear_assessment(
        self,
        wear_event_id: str,
        garment_id: str,
        choice: PostWearChoice,
        notes: Optional[str] = None,
        airing_hours: float = 8.0,
        assessed_at: Optional[str] = None,
    ) -> None:
        """
        Records the owner's assessment after a wear event.
        Validates event existence, garment ownership, active status, and condition state.
        Calculates airing due time relative to assessed_at.
        """
        # 1. Validate wear event identity
        event_cur = self.conn.execute(
            "SELECT * FROM WearEvent WHERE id = ? AND garment_id = ? AND is_voided = 0;",
            (wear_event_id, garment_id),
        )
        event = event_cur.fetchone()
        if not event:
            raise ValueError(f"Active wear event '{wear_event_id}' for garment '{garment_id}' not found")

        # 2. Validate garment condition state
        garment = self.get_garment(garment_id)
        if garment["condition_state"] != ConditionState.WORN_AWAITING_ASSESSMENT.value:
            raise InvalidStateTransitionError(
                f"Garment '{garment_id}' is not in WORN_AWAITING_ASSESSMENT (currently {garment['condition_state']})"
            )

        # 3. Validate no operative assessment already exists
        pwa_cur = self.conn.execute(
            "SELECT id FROM PostWearAssessment WHERE wear_event_id = ?;", (wear_event_id,)
        )
        if pwa_cur.fetchone():
            raise ValueError(f"PostWearAssessment already exists for wear event '{wear_event_id}'")

        now = assessed_at or self._utc_now()
        pwa_id = f"pwa_{uuid.uuid4().hex[:12]}"

        self.conn.execute(
            """
            INSERT INTO PostWearAssessment(id, wear_event_id, garment_id, assessment, notes, assessed_at)
            VALUES (?, ?, ?, ?, ?, ?);
            """,
            (pwa_id, wear_event_id, garment_id, choice.value, notes, now),
        )

        if choice == PostWearChoice.READY_TO_REUSE:
            self.transition_condition(
                garment_id,
                ConditionState.WORN_REUSABLE,
                trigger_event="owner_assessment_ready_to_reuse",
                trigger_details={"wear_event_id": wear_event_id, "notes": notes},
                occurred_at=now,
            )
        elif choice == PostWearChoice.NEEDS_AIRING:
            # Derived from assessed_at, not wall clock!
            clean_iso = now.replace("Z", "+00:00")
            dt = datetime.fromisoformat(clean_iso) + timedelta(hours=airing_hours)
            due_at = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            self.transition_condition(
                garment_id,
                ConditionState.AIRING,
                trigger_event="owner_assessment_needs_airing",
                trigger_details={"wear_event_id": wear_event_id, "airing_hours": airing_hours},
                occurred_at=now,
                airing_review_due_at=due_at,
            )
        elif choice in (PostWearChoice.NEEDS_WASH, PostWearChoice.STAINED, PostWearChoice.WET):
            self.transition_condition(
                garment_id,
                ConditionState.NEEDS_WASH,
                trigger_event=f"owner_assessment_{choice.value}",
                trigger_details={"wear_event_id": wear_event_id, "notes": notes},
                occurred_at=now,
            )
        elif choice == PostWearChoice.NOT_SURE:
            self.transition_condition(
                garment_id,
                ConditionState.UNKNOWN_CONDITION,
                trigger_event="owner_assessment_not_sure",
                trigger_details={"wear_event_id": wear_event_id, "notes": notes},
                occurred_at=now,
            )

    def record_wash_event(
        self,
        garment_id: str,
        user_id: str,
        cleaning_scope: CleaningScope,
        method: str,
        confirmed_clean_at: Optional[str] = None,
        laundry_batch_id: Optional[str] = None,
        confirmed_by: str = "owner",
        notes: Optional[str] = None,
    ) -> str:
        """
        Records a confirmed wash event.
        - Validates owner identity against Garment row.
        - Validates that retired garments cannot be washed.
        - Validates spot_clean implies PARTIAL_CLEAN.
        - FULL_CLEAN resets wears_since_confirmed_wash to 0 and transitions condition to CLEAN.
        - PARTIAL_CLEAN (spot cleaning) does NOT reset wear count and leaves condition unchanged.
        """
        garment = self.get_garment(garment_id)
        if garment["user_id"] != user_id:
            raise PermissionError("User does not own this garment")
        if garment["availability_state"] == AvailabilityState.RETIRED.value:
            raise ValueError(f"Cannot record wash event for retired garment '{garment_id}'")
        if method == "spot_clean" and cleaning_scope != CleaningScope.PARTIAL_CLEAN:
            raise ValueError("spot_clean requires cleaning_scope = PARTIAL_CLEAN")

        now = confirmed_clean_at or self._utc_now()
        wash_id = f"wash_{uuid.uuid4().hex[:12]}"

        self.conn.execute(
            """
            INSERT INTO WashEvent(
                id, user_id, garment_id, laundry_batch_id, cleaning_scope,
                method, confirmed_clean_at, confirmed_by, notes
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                wash_id,
                user_id,
                garment_id,
                laundry_batch_id,
                cleaning_scope.value,
                method,
                now,
                confirmed_by,
                notes,
            ),
        )

        if cleaning_scope == CleaningScope.FULL_CLEAN:
            # Reset wear counter
            self.conn.execute(
                """
                UPDATE Garment
                SET wears_since_confirmed_wash = 0,
                    last_confirmed_wash_at = ?
                WHERE id = ?;
                """,
                (now, garment_id),
            )
            # Transition to CLEAN if not already CLEAN
            if garment["condition_state"] != ConditionState.CLEAN.value:
                self.transition_condition(
                    garment_id,
                    ConditionState.CLEAN,
                    trigger_event="confirmed_full_clean_wash",
                    trigger_details={"wash_event_id": wash_id, "method": method},
                    occurred_at=now,
                )
        elif cleaning_scope == CleaningScope.PARTIAL_CLEAN:
            # Audit partial cleaning without resetting wear count or changing to CLEAN
            pass

        return wash_id
