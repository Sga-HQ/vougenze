"""
Vougenze Laundry Lifecycle Service (Frozen Spec 2.11, Part 3.2).
Manages laundry policies, batch dispatches with preflight validation,
atomic terminal item outcomes, damaged item routing to REPAIR,
and unwashed return mapping (condition -> NEEDS_WASH, availability -> AVAILABLE).
"""
from __future__ import annotations
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any

from core.models import (
    ConditionState,
    AvailabilityState,
    LaundryBatchStatus,
    LaundryItemStatus,
    CleaningScope,
)
from core.state_engine import StateEngine


class LaundryService:
    """Service managing laundry policies, batches, and item returns."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.state_engine = StateEngine(conn)

    def _require_transaction(self) -> None:
        if not self.conn.in_transaction:
            raise RuntimeError("Laundry mutations require an active transaction")

    @staticmethod
    def _utc_now() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def create_policy(
        self,
        user_id: str,
        policy_type: str,
        effective_from: str,
        handover_weekday: Optional[int] = None,
        return_weekday: Optional[int] = None,
        interval_days: Optional[int] = None,
        dirty_threshold: Optional[int] = None,
        effective_until: Optional[str] = None,
    ) -> str:
        policy_id = f"lp_{uuid.uuid4().hex[:12]}"
        now = self._utc_now()
        self.conn.execute(
            """
            INSERT INTO LaundryPolicy(
                id, user_id, policy_type, handover_weekday, return_weekday,
                interval_days, dirty_threshold, effective_from, effective_until, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                policy_id,
                user_id,
                policy_type,
                handover_weekday,
                return_weekday,
                interval_days,
                dirty_threshold,
                effective_from,
                effective_until,
                now,
            ),
        )
        return policy_id

    def update_policy(
        self,
        user_id: str,
        old_policy_id: str,
        policy_type: Optional[str] = None,
        effective_from: str = "",
        new_policy_data: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> str:
        """
        Scenario 15: Updates laundry policy by retiring old policy (effective_until)
        and atomically creating a new policy. Requires active transaction.
        Enforces ownership, active status, date validity, and atomic replacement.
        """
        self._require_transaction()
        now = self._utc_now()

        old = self.conn.execute(
            "SELECT * FROM LaundryPolicy WHERE id = ?;", (old_policy_id,)
        ).fetchone()
        if old is None:
            raise KeyError(f"LaundryPolicy '{old_policy_id}' not found")
        if old["user_id"] != user_id:
            raise PermissionError("Cross-owner policy update rejected")
        if old["effective_until"] is not None:
            raise ValueError(f"Policy '{old_policy_id}' is already closed")

        eff_from = effective_from
        p_data = dict(new_policy_data or {})
        if not eff_from and "effective_from" in p_data:
            eff_from = p_data["effective_from"]
        if not eff_from:
            eff_from = now[:10]

        if eff_from < old["effective_from"]:
            raise ValueError("New effective_from cannot be earlier than old policy effective_from")

        # 1. Close old policy
        self.conn.execute(
            "UPDATE LaundryPolicy SET effective_until = ? WHERE id = ?;",
            (eff_from, old_policy_id),
        )

        # 2. Insert new policy
        pol_type = policy_type or p_data.get("policy_type") or old["policy_type"]
        new_id = p_data.get("id") or f"lp_{uuid.uuid4().hex[:12]}"
        handover = p_data.get("handover_weekday", kwargs.get("handover_weekday", old["handover_weekday"]))
        ret_day = p_data.get("return_weekday", kwargs.get("return_weekday", old["return_weekday"]))
        interval = p_data.get("interval_days", kwargs.get("interval_days", old["interval_days"]))
        threshold = p_data.get("dirty_threshold", kwargs.get("dirty_threshold", old["dirty_threshold"]))

        self.conn.execute(
            """
            INSERT INTO LaundryPolicy(
                id, user_id, policy_type, handover_weekday, return_weekday,
                interval_days, dirty_threshold, effective_from, effective_until, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?);
            """,
            (
                new_id,
                user_id,
                pol_type,
                handover,
                ret_day,
                interval,
                threshold,
                eff_from,
                now,
            ),
        )
        return new_id

    def send_laundry_batch(
        self,
        user_id: str,
        garment_ids: List[str],
        sent_date: str,
        expected_return_date: str,
        policy_id: Optional[str] = None,
        wash_method: str = "laundry_service",
        notes: Optional[str] = None,
    ) -> str:
        """
        Dispatches a laundry batch after complete preflight validation of all garments.
        Atomically records batch, batch items, unlinks/links, and transitions availability.
        """
        self._require_transaction()
        if expected_return_date < sent_date:
            raise ValueError("expected_return_date must be >= sent_date")

        unique_ids = list(dict.fromkeys(garment_ids))
        if not unique_ids:
            raise ValueError("Laundry batch must contain at least one garment")
        if len(unique_ids) != len(garment_ids):
            raise ValueError("Duplicate garment IDs in laundry batch")

        if policy_id:
            pol_cur = self.conn.execute(
                "SELECT user_id FROM LaundryPolicy WHERE id = ?;", (policy_id,)
            )
            pol = pol_cur.fetchone()
            if not pol:
                raise KeyError(f"LaundryPolicy {policy_id} not found")
            if pol["user_id"] != user_id:
                raise PermissionError("LaundryPolicy belongs to a different user")

        placeholders = ",".join("?" for _ in unique_ids)
        cur = self.conn.execute(
            f"SELECT * FROM Garment WHERE id IN ({placeholders});", unique_ids
        )
        rows = cur.fetchall()
        if len(rows) != len(unique_ids):
            raise ValueError("One or more garments do not exist")

        for g in rows:
            if g["user_id"] != user_id:
                raise PermissionError("Cross-owner laundry dispatch rejected")
            if g["availability_state"] == AvailabilityState.IN_LAUNDRY.value:
                raise ValueError(f"Garment '{g['id']}' is already in laundry")
            if g["availability_state"] == AvailabilityState.RETIRED.value:
                raise ValueError(f"Garment '{g['id']}' is retired")
            if g["current_laundry_batch_id"] is not None:
                raise ValueError(f"Garment '{g['id']}' belongs to another active batch")

        batch_id = f"lb_{uuid.uuid4().hex[:12]}"
        now = self._utc_now()

        # Insert LaundryBatch
        self.conn.execute(
            """
            INSERT INTO LaundryBatch(
                id, user_id, policy_id, sent_date, expected_return_date,
                status, wash_method, notes, created_at
            )
            VALUES (?, ?, ?, ?, ?, 'IN_LAUNDRY', ?, ?, ?);
            """,
            (
                batch_id,
                user_id,
                policy_id,
                sent_date,
                expected_return_date,
                wash_method,
                notes,
                now,
            ),
        )

        for gid in unique_ids:
            # Insert item
            self.conn.execute(
                """
                INSERT INTO LaundryBatchItem(batch_id, garment_id, item_status, wash_confirmed)
                VALUES (?, ?, 'IN_LAUNDRY', 0);
                """,
                (batch_id, gid),
            )

            # Link batch and transition availability
            self.conn.execute(
                "UPDATE Garment SET current_laundry_batch_id = ? WHERE id = ?;",
                (batch_id, gid),
            )
            self.state_engine.transition_availability(
                garment_id=gid,
                to_state=AvailabilityState.IN_LAUNDRY,
                trigger_event="sent_in_laundry_batch",
                trigger_details={"batch_id": batch_id, "sent_date": sent_date},
                occurred_at=now,
            )

        return batch_id

    def record_item_return(
        self,
        batch_id: str,
        garment_id: str,
        item_status: LaundryItemStatus,
        wash_confirmed: bool,
        return_date: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> None:
        """
        Processes terminal outcome for an item in a laundry batch.
        Validates terminal outcome consistency:
        - RETURNED_CLEAN requires wash_confirmed=True
        - RETURNED_UNWASHED requires wash_confirmed=False
        - MISSING_RESOLVED requires wash_confirmed=False
        - RETURNED_UNWASHED transitions condition to NEEDS_WASH without WashEvent.
        """
        self._require_transaction()
        # Strict outcome guards
        if item_status == LaundryItemStatus.RETURNED_CLEAN and not wash_confirmed:
            raise ValueError("RETURNED_CLEAN requires wash_confirmed=True")
        if item_status in (LaundryItemStatus.RETURNED_UNWASHED, LaundryItemStatus.MISSING_RESOLVED) and wash_confirmed:
            raise ValueError(f"{item_status.value} requires wash_confirmed=False")

        # Verify batch exists and is not closed
        batch_cur = self.conn.execute("SELECT * FROM LaundryBatch WHERE id = ?;", (batch_id,))
        batch = batch_cur.fetchone()
        if not batch:
            raise KeyError(f"LaundryBatch {batch_id} not found")
        if batch["status"] == LaundryBatchStatus.RETURNED_CONFIRMED.value:
            raise ValueError(f"Batch {batch_id} is already closed")

        # Verify garment item exists in batch and is pending
        item_cur = self.conn.execute(
            "SELECT * FROM LaundryBatchItem WHERE batch_id = ? AND garment_id = ?;",
            (batch_id, garment_id),
        )
        item = item_cur.fetchone()
        if not item:
            raise KeyError(f"Garment {garment_id} is not pending in batch {batch_id}")
        if item["item_status"] != LaundryItemStatus.IN_LAUNDRY.value:
            raise ValueError(f"Garment {garment_id} has already reached a terminal outcome in batch {batch_id}")

        # Verify garment state
        g = self.state_engine.get_garment(garment_id)
        if g["user_id"] != batch["user_id"]:
            raise PermissionError("Cross-owner batch/garment operation rejected")
        if g["current_laundry_batch_id"] != batch_id:
            raise ValueError(f"Garment {garment_id} current_laundry_batch_id does not match batch {batch_id}")

        now = return_date or self._utc_now()
        wash_confirmed_int = 1 if wash_confirmed else 0

        # Update item status in batch
        res = self.conn.execute(
            """
            UPDATE LaundryBatchItem
            SET item_status = ?,
                wash_confirmed = ?
            WHERE batch_id = ? AND garment_id = ?;
            """,
            (item_status.value, wash_confirmed_int, batch_id, garment_id),
        )
        if res.rowcount != 1:
            raise KeyError(f"Garment {garment_id} is not pending in batch {batch_id}")

        # Unlink garment current batch id
        self.conn.execute(
            "UPDATE Garment SET current_laundry_batch_id = NULL WHERE id = ?;",
            (garment_id,),
        )

        user_id = g["user_id"]

        if item_status == LaundryItemStatus.RETURNED_CLEAN:
            # Confirmed wash resets wear counter and transitions condition to CLEAN
            self.state_engine.record_wash_event(
                garment_id=garment_id,
                user_id=user_id,
                cleaning_scope=CleaningScope.FULL_CLEAN,
                method="laundry_service",
                confirmed_clean_at=now,
                laundry_batch_id=batch_id,
                notes=notes,
            )
            # Availability becomes AVAILABLE
            self.state_engine.transition_availability(
                garment_id=garment_id,
                to_state=AvailabilityState.AVAILABLE,
                trigger_event="laundry_return_clean",
                trigger_details={"batch_id": batch_id},
                occurred_at=now,
            )

        elif item_status == LaundryItemStatus.RETURNED_DAMAGED:
            # Scenario 42: Damaged return routes directly to REPAIR
            if wash_confirmed:
                self.state_engine.record_wash_event(
                    garment_id=garment_id,
                    user_id=user_id,
                    cleaning_scope=CleaningScope.FULL_CLEAN,
                    method="laundry_service",
                    confirmed_clean_at=now,
                    laundry_batch_id=batch_id,
                    notes=notes,
                )
            self.state_engine.transition_availability(
                garment_id=garment_id,
                to_state=AvailabilityState.REPAIR,
                trigger_event="laundry_return_damaged",
                trigger_details={"batch_id": batch_id, "wash_confirmed": wash_confirmed},
                unavailability_reason="Damaged in laundry",
                occurred_at=now,
            )

        elif item_status == LaundryItemStatus.RETURNED_UNWASHED:
            # Frozen contract: condition -> NEEDS_WASH, availability -> AVAILABLE, NO WashEvent
            if g["condition_state"] != ConditionState.NEEDS_WASH.value:
                self.state_engine.transition_condition(
                    garment_id=garment_id,
                    to_state=ConditionState.NEEDS_WASH,
                    trigger_event="laundry_return_unwashed",
                    trigger_details={"batch_id": batch_id},
                    occurred_at=now,
                )
            self.state_engine.transition_availability(
                garment_id=garment_id,
                to_state=AvailabilityState.AVAILABLE,
                trigger_event="laundry_return_unwashed",
                trigger_details={"batch_id": batch_id},
                occurred_at=now,
            )

        elif item_status == LaundryItemStatus.MISSING_RESOLVED:
            # Marked UNAVAILABLE
            self.state_engine.transition_availability(
                garment_id=garment_id,
                to_state=AvailabilityState.UNAVAILABLE,
                trigger_event="laundry_missing_resolved",
                trigger_details={"batch_id": batch_id},
                unavailability_reason="Lost by laundry service",
                occurred_at=now,
            )

        # Check remaining items in batch to determine batch status (Scenarios 12 & 41)
        cur = self.conn.execute(
            "SELECT count(*) FROM LaundryBatchItem WHERE batch_id = ? AND item_status = 'IN_LAUNDRY';",
            (batch_id,),
        )
        remaining_in_laundry = cur.fetchone()[0]

        if remaining_in_laundry == 0:
            self.conn.execute(
                """
                UPDATE LaundryBatch
                SET status = 'RETURNED_CONFIRMED',
                    confirmed_return_date = ?
                WHERE id = ?;
                """,
                (now, batch_id),
            )
        else:
            self.conn.execute(
                "UPDATE LaundryBatch SET status = 'PARTIAL_RETURN' WHERE id = ?;",
                (batch_id,),
            )

    def mark_batch_delayed(
        self,
        batch_id: str,
        reason: Optional[str] = None,
        new_expected_return_date: Optional[str] = None,
        delay_reason: Optional[str] = None,
    ) -> None:
        """Scenario 4: Marks a batch as delayed. Rejects closed or terminal batches."""
        self._require_transaction()
        cur = self.conn.execute("SELECT status FROM LaundryBatch WHERE id = ?;", (batch_id,))
        row = cur.fetchone()
        if not row:
            raise KeyError(f"LaundryBatch {batch_id} not found")
        if row["status"] == "RETURNED_CONFIRMED":
            raise ValueError(f"Cannot mark batch {batch_id} as delayed from terminal status RETURNED_CONFIRMED")
        if row["status"] not in ("IN_LAUNDRY", "PARTIAL_RETURN"):
            raise ValueError(f"Cannot mark batch {batch_id} delayed from status '{row['status']}'")

        effective_reason = delay_reason or reason
        note_suffix = f"Delayed: {effective_reason}" if effective_reason else "Delayed"
        if new_expected_return_date:
            res = self.conn.execute(
                """
                UPDATE LaundryBatch
                SET status = 'DELAYED',
                    expected_return_date = ?,
                    notes = CASE WHEN notes IS NULL THEN ? ELSE notes || '; ' || ? END
                WHERE id = ? AND status IN ('IN_LAUNDRY', 'PARTIAL_RETURN');
                """,
                (new_expected_return_date, note_suffix, note_suffix, batch_id),
            )
        else:
            res = self.conn.execute(
                """
                UPDATE LaundryBatch
                SET status = 'DELAYED',
                    notes = CASE WHEN notes IS NULL THEN ? ELSE notes || '; ' || ? END
                WHERE id = ? AND status IN ('IN_LAUNDRY', 'PARTIAL_RETURN');
                """,
                (note_suffix, note_suffix, batch_id),
            )
        if res.rowcount != 1:
            raise ValueError(f"Batch {batch_id} is not in delayable status")
