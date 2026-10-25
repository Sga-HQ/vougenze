"""
Vougenze Wear Logging, Correction, and History Service (Frozen Spec 2.8, 2.9, 2.5, Part 4).
Handles immutable wear event capture with strict preflight validation, snapshotting,
selective hard-limit authorization consumption, and chronological condition lineage replay
on wear voiding and correction.
Requires active transaction boundaries and enforces authoritative session metadata.
"""
from __future__ import annotations
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List, Set, Tuple

from core.models import (
    ConditionState,
    AvailabilityState,
    RecordStatus,
    InvalidationReason,
    PostWearChoice,
)
from core.state_engine import StateEngine


class DuplicateWearEventError(sqlite3.IntegrityError):
    """Raised when a non-voided wear event already exists for (session_id, garment_id)."""
    pass


class WearService:
    """Service managing wear logs, wear events, wear authorizations, and audit corrections."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.state_engine = StateEngine(conn)

    def _require_transaction(self) -> None:
        if not self.conn.in_transaction:
            raise RuntimeError("Wear service operations require an active transaction")

    @staticmethod
    def _utc_now() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def create_wear_authorization(
        self,
        garment_id: str,
        reason: str,
        authorized_by: str = "owner",
        authorized_at: Optional[str] = None,
    ) -> str:
        """Creates an auditable wear authorization for an item exceeding wear guidelines."""
        auth_id = f"gwa_{uuid.uuid4().hex[:12]}"
        now = authorized_at or self._utc_now()
        self.conn.execute(
            """
            INSERT INTO GarmentWearAuthorization(
                id, garment_id, authorized_by, reason, authorized_at, status
            )
            VALUES (?, ?, ?, ?, ?, 'AVAILABLE');
            """,
            (auth_id, garment_id, authorized_by, reason, now),
        )
        return auth_id

    def log_outfit_wear(
        self,
        user_id: str,
        session_id: str,
        garment_roles: Dict[str, str],  # role -> garment_id
        source: str = "manual_outfit",
        session_type: Optional[str] = None,
        importance: Optional[str] = None,
        local_wear_date: Optional[str] = None,
        timezone_id: str = "UTC",
        recommendation_plan_id: Optional[str] = None,
        plan_assignment_id: Optional[str] = None,
        was_recommended: int = 0,
        was_modified_before_wear: int = 0,
        modification_reason: Optional[str] = None,
        satisfaction_rating: Optional[int] = None,
        comfort_rating: Optional[int] = None,
        occasion_fit_rating: Optional[int] = None,
        would_wear_again: Optional[int] = None,
        feedback_notes: Optional[str] = None,
        started_at_utc: Optional[str] = None,
        ended_at_utc: Optional[str] = None,
        reserved_authorizations: Optional[Dict[str, str]] = None,
        operation_mode: str = "NEW_WEAR",
    ) -> str:
        """
        Logs a committed outfit wear after complete preflight validation:
        - Every garment belongs to user_id.
        - Every garment is AVAILABLE and not retired.
        - Every garment is CLEAN or WORN_REUSABLE.
        - Every garment satisfies its recorded role.
        - Session row is the authoritative source for session_type, importance, and date.
        - Plan assignment is verified against plan_id, session_id, and owner.
        - Hard-limit authorization is enforced: garments at or beyond hard_limit_wears
          require an available authorization, matching any plan reservation.
        """
        self._require_transaction()
        now = self._utc_now()
        log_id = f"owl_{uuid.uuid4().hex[:12]}"

        # Preflight: Verify session ownership and use authoritative session fields
        s_cur = self.conn.execute("SELECT * FROM WearSession WHERE id = ?;", (session_id,))
        session = s_cur.fetchone()
        if not session:
            raise KeyError(f"WearSession {session_id} not found")
        if session["user_id"] != user_id:
            raise PermissionError("User does not own this wear session")
        if operation_mode == "NEW_WEAR":
            if session["status"] != "PLANNED":
                raise ValueError(
                    f"New wear requires PLANNED session; session '{session_id}' is {session['status']}"
                )
        elif operation_mode == "CORRECTIVE_REPLACEMENT":
            if session["status"] != "COMPLETED":
                raise ValueError(
                    f"Corrective replacement requires COMPLETED session; session '{session_id}' is {session['status']}"
                )
        else:
            raise ValueError(f"Unsupported wear operation_mode: {operation_mode}")
        if was_modified_before_wear and not (modification_reason or "").strip():
            raise ValueError("Modified wear requires a non-empty modification_reason")

        # Authoritative session metadata
        eff_session_type = session["session_type"]
        eff_importance = session["importance"]
        eff_date_str = session["session_date"]

        # Validate plan_assignment_id if provided
        if plan_assignment_id:
            pa_cur = self.conn.execute(
                """
                SELECT psa.*, rp.user_id as plan_user_id
                FROM PlanSessionAssignment psa
                JOIN RecommendationPlan rp ON psa.plan_id = rp.id
                WHERE psa.id = ?;
                """,
                (plan_assignment_id,)
            )
            pa = pa_cur.fetchone()
            if not pa:
                raise KeyError(f"PlanSessionAssignment '{plan_assignment_id}' not found")
            if pa["plan_user_id"] != user_id:
                raise PermissionError("PlanSessionAssignment belongs to another user")
            if pa["session_id"] != session_id:
                raise ValueError("PlanSessionAssignment belongs to a different session")
            if recommendation_plan_id and pa["plan_id"] != recommendation_plan_id:
                raise ValueError("PlanSessionAssignment does not belong to specified recommendation_plan_id")

            # Verify exact garment-role correspondence unless was_modified_before_wear is explicitly asserted
            pai_cur = self.conn.execute(
                "SELECT outfit_role, garment_id FROM PlanAssignmentItem WHERE assignment_id = ?;",
                (plan_assignment_id,),
            )
            expected_roles = {r["outfit_role"]: r["garment_id"] for r in pai_cur.fetchall()}
            if not was_modified_before_wear and expected_roles != garment_roles:
                raise ValueError(
                    f"Logged garment roles {garment_roles} do not match recommended plan assignment {expected_roles}. "
                    "Set was_modified_before_wear=1 with modification_reason if this wear intentionally modifies the recommendation."
                )

        # Preflight: Verify each garment and resolve authorizations needed
        garments_by_id: Dict[str, sqlite3.Row] = {}
        auths_to_consume: Dict[str, str] = {}  # garment_id -> auth_id

        for role, gid in garment_roles.items():
            if gid not in garments_by_id:
                g_cur = self.conn.execute("SELECT * FROM Garment WHERE id = ?;", (gid,))
                g = g_cur.fetchone()
                if not g:
                    raise KeyError(f"Garment '{gid}' does not exist")
                if g["user_id"] != user_id:
                    raise PermissionError(f"Cross-owner wear rejected for garment '{gid}'")
                if g["availability_state"] != AvailabilityState.AVAILABLE.value:
                    raise ValueError(f"Garment '{gid}' is not AVAILABLE (state: {g['availability_state']})")
                if g["condition_state"] not in (ConditionState.CLEAN.value, ConditionState.WORN_REUSABLE.value):
                    raise ValueError(f"Garment '{gid}' is not CLEAN or WORN_REUSABLE (state: {g['condition_state']})")

                # Verify role filling
                cat = g["physical_category"]
                fills = json.loads(g["fills_roles"] or "[]")
                if not (role in fills or role == cat or (role in ("top", "bottom") and cat == "one_piece")):
                    raise ValueError(f"Garment '{gid}' cannot fill role '{role}'")

                # Check hard wear limit
                effective_limit = g["hard_limit_wears"]
                current_wears = g["wears_since_confirmed_wash"]
                if effective_limit is not None and current_wears >= effective_limit:
                    auth_id = None
                    # Check persistent plan reservation first
                    if recommendation_plan_id:
                        res_cur = self.conn.execute(
                            """
                            SELECT authorization_id FROM PlanWearAuthorizationReservation
                            WHERE plan_id = ? AND session_id = ? AND garment_id = ?;
                            """,
                            (recommendation_plan_id, session_id, gid),
                        )
                        res_row = res_cur.fetchone()
                        if res_row:
                            auth_id = res_row["authorization_id"]

                    if not auth_id and reserved_authorizations and gid in reserved_authorizations:
                        auth_id = reserved_authorizations[gid]

                    if not auth_id:
                        auth_cur = self.conn.execute(
                            """
                            SELECT gwa.id FROM GarmentWearAuthorization gwa
                            WHERE gwa.garment_id = ? AND gwa.status = 'AVAILABLE' AND gwa.consumed_by_wear_event_id IS NULL
                              AND NOT EXISTS (
                                  SELECT 1 FROM PlanWearAuthorizationReservation pwar
                                  JOIN RecommendationPlan rp ON pwar.plan_id = rp.id
                                  WHERE pwar.authorization_id = gwa.id
                                    AND rp.status IN ('PROPOSED', 'RESERVED')
                              )
                            ORDER BY gwa.authorized_at ASC LIMIT 1;
                            """,
                            (gid,),
                        )
                        auth_row = auth_cur.fetchone()
                        if auth_row:
                            auth_id = auth_row["id"]

                    if not auth_id:
                        raise ValueError(f"Garment '{gid}' reached hard wear limit ({current_wears}/{effective_limit}) without wear authorization")

                    # Verify authorization belongs to garment and is AVAILABLE
                    chk_cur = self.conn.execute(
                        "SELECT garment_id, status, consumed_by_wear_event_id FROM GarmentWearAuthorization WHERE id = ?;",
                        (auth_id,)
                    )
                    chk = chk_cur.fetchone()
                    if not chk or chk["garment_id"] != gid or chk["status"] != "AVAILABLE" or chk["consumed_by_wear_event_id"] is not None:
                        raise ValueError(f"Authorization '{auth_id}' is not AVAILABLE or does not belong to garment '{gid}'")

                    auths_to_consume[gid] = auth_id

                garments_by_id[gid] = g

        # 1. Insert OutfitWearLog
        self.conn.execute(
            """
            INSERT INTO OutfitWearLog(
                id, user_id, session_id, recommendation_plan_id, plan_assignment_id,
                source, started_at_utc, ended_at_utc, local_wear_date, timezone_id,
                session_type, importance, was_recommended, was_modified_before_wear,
                modification_reason, satisfaction_rating, comfort_rating, occasion_fit_rating,
                would_wear_again, record_status, feedback_notes, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?);
            """,
            (
                log_id,
                user_id,
                session_id,
                recommendation_plan_id,
                plan_assignment_id,
                source,
                started_at_utc or now,
                ended_at_utc,
                eff_date_str,
                timezone_id,
                eff_session_type,
                eff_importance,
                was_recommended,
                was_modified_before_wear,
                modification_reason,
                satisfaction_rating,
                comfort_rating,
                occasion_fit_rating,
                would_wear_again,
                feedback_notes,
                now,
            ),
        )

        # 2. Insert OutfitWearLogItems
        for role, gid in garment_roles.items():
            garment = garments_by_id[gid]
            snapshot = {
                "name": garment["name"],
                "physical_category": garment["physical_category"],
                "subcategory": garment["subcategory"],
                "primary_color": garment["primary_color"],
                "secondary_colors": garment["secondary_colors"],
                "formality": garment["formality"],
                "cut_and_fit": garment["cut_and_fit"],
                "wears_before_event": garment["wears_since_confirmed_wash"],
                "condition_at_wear": garment["condition_state"],
                "availability_at_wear": garment["availability_state"],
            }
            self.conn.execute(
                """
                INSERT INTO OutfitWearLogItem(
                    outfit_wear_log_id, garment_id, outfit_role,
                    condition_at_wear, availability_at_wear, wears_before_event, garment_snapshot
                )
                VALUES (?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    log_id,
                    gid,
                    role,
                    garment["condition_state"],
                    garment["availability_state"],
                    garment["wears_since_confirmed_wash"],
                    json.dumps(snapshot),
                ),
            )

        # 3. Create WearEvents and advance condition
        event_time = started_at_utc or now
        for gid, garment in garments_by_id.items():
            event_id = f"we_{uuid.uuid4().hex[:12]}"
            self.conn.execute(
                """
                INSERT INTO WearEvent(id, session_id, garment_id, outfit_wear_log_id, worn_at, is_voided)
                VALUES (?, ?, ?, ?, ?, 0);
                """,
                (event_id, session_id, gid, log_id, event_time),
            )

            # Consume authorization ONLY if hard limit was reached and resolved
            if gid in auths_to_consume:
                auth_id = auths_to_consume[gid]
                res = self.conn.execute(
                    """
                    UPDATE GarmentWearAuthorization
                    SET status = 'CONSUMED',
                        consumed_by_wear_event_id = ?
                    WHERE id = ?
                      AND garment_id = ?
                      AND status = 'AVAILABLE'
                      AND consumed_by_wear_event_id IS NULL;
                    """,
                    (event_id, auth_id, gid),
                )
                if res.rowcount != 1:
                    raise ValueError(f"Authorization {auth_id} could not be consumed for garment {gid}")

            # Increment wear counter
            self.conn.execute(
                """
                UPDATE Garment
                SET wears_since_confirmed_wash = wears_since_confirmed_wash + 1,
                    last_worn_at = ?
                WHERE id = ?;
                """,
                (event_time, gid),
            )

            # Transition condition via StateEngine
            self.state_engine.transition_condition(
                garment_id=gid,
                to_state=ConditionState.WORN_AWAITING_ASSESSMENT,
                trigger_event="logged_wear_session",
                trigger_details={"session_id": session_id, "outfit_wear_log_id": log_id},
                occurred_at=event_time,
            )

        # Mark session as COMPLETED
        self.conn.execute(
            "UPDATE WearSession SET status = 'COMPLETED' WHERE id = ?;",
            (session_id,),
        )

        return log_id

    def void_outfit_wear(self, outfit_wear_log_id: str, void_reason: str) -> None:
        """
        Scenario 30: Voids an outfit wear log.
        Preserves audit trail (record_status = 'VOIDED'), marks WearEvents is_voided=1,
        releases consumed authorizations, and reconstructs each garment's condition truth
        and wear count by replaying the chronological lineage from the last confirmed wash.
        """
        self._require_transaction()
        now = self._utc_now()

        log_row = self.conn.execute(
            "SELECT record_status FROM OutfitWearLog WHERE id = ?;",
            (outfit_wear_log_id,),
        ).fetchone()
        if log_row is None:
            raise KeyError(f"OutfitWearLog {outfit_wear_log_id} not found")
        if log_row["record_status"] != RecordStatus.ACTIVE.value:
            raise ValueError(
                f"OutfitWearLog {outfit_wear_log_id} is not ACTIVE (status: {log_row['record_status']})"
            )
        if not (void_reason or "").strip():
            raise ValueError("void_reason must be non-empty")
        cursor = self.conn.execute(
            "SELECT DISTINCT garment_id FROM OutfitWearLogItem WHERE outfit_wear_log_id = ?;",
            (outfit_wear_log_id,),
        )
        garment_ids = [row["garment_id"] for row in cursor.fetchall()]

        # Mark OutfitWearLog as VOIDED
        self.conn.execute(
            """
            UPDATE OutfitWearLog
            SET record_status = 'VOIDED',
                correction_reason = ?
            WHERE id = ?;
            """,
            (void_reason, outfit_wear_log_id),
        )

        # Mark WearEvents as voided
        self.conn.execute(
            "UPDATE WearEvent SET is_voided = 1 WHERE outfit_wear_log_id = ?;",
            (outfit_wear_log_id,),
        )

        # Release consumed authorizations
        self.conn.execute(
            """
            UPDATE GarmentWearAuthorization
            SET status = 'AVAILABLE',
                consumed_by_wear_event_id = NULL
            WHERE consumed_by_wear_event_id IN (
                SELECT id FROM WearEvent WHERE outfit_wear_log_id = ?
            );
            """,
            (outfit_wear_log_id,),
        )

        # Reconstruct wear and condition state for each affected garment
        for gid in garment_ids:
            self.reconstruct_garment_lineage(gid, now)

    def reconstruct_garment_lineage(self, garment_id: str, now: str) -> None:
        """
        Reconstructs wear count, last wear and condition from full washes plus active wear events
        and their operative post-wear assessments.
        """
        # Find last confirmed full wash
        wash_cur = self.conn.execute(
            """
            SELECT confirmed_clean_at FROM WashEvent
            WHERE garment_id = ? AND cleaning_scope = 'FULL_CLEAN'
            ORDER BY confirmed_clean_at DESC LIMIT 1;
            """,
            (garment_id,),
        )
        last_wash = wash_cur.fetchone()
        base_clean_at = last_wash["confirmed_clean_at"] if last_wash else None

        # Fetch active wear events since last full wash
        if base_clean_at:
            events_cur = self.conn.execute(
                """
                SELECT id, worn_at FROM WearEvent
                WHERE garment_id = ? AND is_voided = 0 AND worn_at >= ?
                ORDER BY worn_at ASC;
                """,
                (garment_id, base_clean_at),
            )
        else:
            events_cur = self.conn.execute(
                """
                SELECT id, worn_at FROM WearEvent
                WHERE garment_id = ? AND is_voided = 0
                ORDER BY worn_at ASC;
                """,
                (garment_id,),
            )
        active_events = events_cur.fetchall()

        wear_count = len(active_events)
        last_worn_at = active_events[-1]["worn_at"] if active_events else None

        # Determine condition state by replaying operative assessments
        if wear_count == 0:
            reconstructed_condition = ConditionState.CLEAN.value
        else:
            latest_event_id = active_events[-1]["id"]
            pwa_cur = self.conn.execute(
                "SELECT assessment FROM PostWearAssessment WHERE wear_event_id = ?;",
                (latest_event_id,),
            )
            pwa = pwa_cur.fetchone()
            if pwa:
                choice = pwa["assessment"]
                if choice == PostWearChoice.READY_TO_REUSE.value:
                    reconstructed_condition = ConditionState.WORN_REUSABLE.value
                elif choice == PostWearChoice.NEEDS_AIRING.value:
                    reconstructed_condition = ConditionState.AIRING.value
                elif choice in (PostWearChoice.NEEDS_WASH.value, PostWearChoice.STAINED.value, PostWearChoice.WET.value):
                    reconstructed_condition = ConditionState.NEEDS_WASH.value
                elif choice == PostWearChoice.NOT_SURE.value:
                    reconstructed_condition = ConditionState.UNKNOWN_CONDITION.value
                else:
                    reconstructed_condition = ConditionState.WORN_AWAITING_ASSESSMENT.value
            else:
                reconstructed_condition = ConditionState.WORN_AWAITING_ASSESSMENT.value

        self.conn.execute(
            """
            UPDATE Garment
            SET wears_since_confirmed_wash = ?,
                last_worn_at = ?,
                condition_state = ?,
                updated_at = ?
            WHERE id = ?;
            """,
            (wear_count, last_worn_at, reconstructed_condition, now, garment_id),
        )

    def correct_outfit_wear(
        self,
        original_log_id: str,
        new_garment_roles: Dict[str, str],
        correction_reason: str,
        **kwargs,
    ) -> str:
        """
        Scenario 30: Corrects a wear log by voiding the original and creating an active replacement.
        Preserves original wear chronology (started_at_utc and ended_at_utc).
        """
        self._require_transaction()
        orig_cur = self.conn.execute(
            "SELECT * FROM OutfitWearLog WHERE id = ?;", (original_log_id,)
        )
        orig = orig_cur.fetchone()
        if not orig:
            raise KeyError(f"OutfitWearLog {original_log_id} not found")

        # Void original
        self.void_outfit_wear(original_log_id, f"Corrected: {correction_reason}")

        # Create replacement log preserving original wear chronology
        new_log_id = self.log_outfit_wear(
            user_id=orig["user_id"],
            session_id=orig["session_id"],
            garment_roles=new_garment_roles,
            source=kwargs.get("source", orig["source"]),
            session_type=orig["session_type"],
            importance=orig["importance"],
            local_wear_date=orig["local_wear_date"],
            timezone_id=orig["timezone_id"],
            recommendation_plan_id=orig["recommendation_plan_id"],
            plan_assignment_id=orig["plan_assignment_id"],
            was_recommended=orig["was_recommended"],
            was_modified_before_wear=1,
            modification_reason=correction_reason,
            satisfaction_rating=kwargs.get("satisfaction_rating", orig["satisfaction_rating"]),
            comfort_rating=kwargs.get("comfort_rating", orig["comfort_rating"]),
            occasion_fit_rating=kwargs.get("occasion_fit_rating", orig["occasion_fit_rating"]),
            would_wear_again=kwargs.get("would_wear_again", orig["would_wear_again"]),
            feedback_notes=kwargs.get("feedback_notes", orig["feedback_notes"]),
            started_at_utc=orig["started_at_utc"],
            ended_at_utc=orig["ended_at_utc"],
            operation_mode="CORRECTIVE_REPLACEMENT",
        )

        # Link superseded_by_id on original
        self.conn.execute(
            "UPDATE OutfitWearLog SET superseded_by_id = ? WHERE id = ?;",
            (new_log_id, original_log_id),
        )

        return new_log_id

    def query_active_history(
        self,
        user_id: str,
        before_date: Optional[str] = None,
        window_days: int = 14,
        reference_date: Optional[str] = None,
    ) -> List[sqlite3.Row]:
        """Returns non-voided OutfitWearLog records in window before before_date / reference_date."""
        ref_date = reference_date or before_date
        if ref_date is None:
            raise ValueError("Must provide either before_date or reference_date")
        if window_days is not None and window_days >= 0:
            cur = self.conn.execute(
                """
                SELECT owl.* FROM OutfitWearLog owl
                WHERE owl.user_id = ?
                  AND owl.record_status = 'ACTIVE'
                  AND owl.local_wear_date <= ?
                  AND owl.local_wear_date >= date(?, '-' || ? || ' days')
                ORDER BY owl.local_wear_date DESC;
                """,
                (user_id, ref_date, ref_date, window_days),
            )
        else:
            cur = self.conn.execute(
                """
                SELECT owl.* FROM OutfitWearLog owl
                WHERE owl.user_id = ?
                  AND owl.record_status = 'ACTIVE'
                  AND owl.local_wear_date <= ?
                ORDER BY owl.local_wear_date DESC;
                """,
                (user_id, ref_date),
            )
        return cur.fetchall()

    def retire_garment(self, garment_id: str, reason: str = "Retired by owner") -> None:
        """Scenario 23: Retires a garment and atomically invalidates all active proposed/reserved plans referencing it."""
        self._require_transaction()
        now = self._utc_now()
        g = self.state_engine.get_garment(garment_id)
        if g["availability_state"] == AvailabilityState.RETIRED.value:
            raise ValueError(f"Garment {garment_id} is already RETIRED")

        self.state_engine.transition_availability(
            garment_id=garment_id,
            to_state=AvailabilityState.RETIRED,
            trigger_event="owner_retire_garment",
            unavailability_reason=reason,
            occurred_at=now,
        )

        # Invalidate proposed and reserved plans referencing this garment
        plans_cur = self.conn.execute(
            """
            SELECT DISTINCT psa.plan_id
            FROM PlanAssignmentItem pai
            JOIN PlanSessionAssignment psa ON pai.assignment_id = psa.id
            JOIN RecommendationPlan rp ON psa.plan_id = rp.id
            WHERE pai.garment_id = ? AND rp.status IN ('PROPOSED', 'RESERVED');
            """,
            (garment_id,),
        )
        for pid in [r["plan_id"] for r in plans_cur.fetchall()]:
            self.conn.execute(
                """
                UPDATE RecommendationPlan
                SET status = 'SUPERSEDED',
                    invalidation_reason = ?
                WHERE id = ?;
                """,
                (InvalidationReason.RETIREMENT.value, pid),
            )
            # Release all authorization reservations held by this superseded plan
            self.conn.execute(
                "DELETE FROM PlanWearAuthorizationReservation WHERE plan_id = ?;",
                (pid,),
            )
