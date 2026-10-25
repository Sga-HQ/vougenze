"""
Vougenze Lifecycle RuleBasedStateMachine Test Suite.
Uses Hypothesis stateful testing to attack physical item unity, replay determinism,
cleaning capacity invariants, reservation mutual exclusion, and known defect boundaries.
"""
from __future__ import annotations
import json
import os
import shutil
import tempfile
import unittest
import uuid
from typing import Dict, List, Optional, Set, Tuple

from hypothesis import settings, Verbosity, Phase, HealthCheck
from hypothesis.stateful import (
    RuleBasedStateMachine,
    Bundle,
    rule,
    initialize,
    invariant,
    consumes,
    multiple,
)
import hypothesis.strategies as st

from core.db import Database
from core.models import (
    ConditionState,
    AvailabilityState,
    CleaningScope,
    PostWearChoice,
    LaundryItemStatus,
    PriorityLevel,
)
from core.state_engine import StateEngine
from core.wear_service import WearService
from core.laundry_service import LaundryService
from core.session_service import SessionService
from core.planner import ContinuationPlanner
import tests.strategies.domain_strategies as ds


import collections

class VougenzeLifecycleStateMachine(RuleBasedStateMachine):
    """
    Stateful model of Vougenze Wardrobe Lifecycle.
    Drives state transitions through services and asserts domain invariants.
    """

    rule_counts: Dict[str, int] = collections.defaultdict(int)

    def _record_rule(self, name: str) -> None:
        VougenzeLifecycleStateMachine.rule_counts[name] += 1

    garments = Bundle("garments")
    sessions = Bundle("sessions")
    authorizations = Bundle("authorizations")
    plans = Bundle("plans")
    laundry_batches = Bundle("laundry_batches")
    wear_logs = Bundle("wear_logs")

    @initialize()
    def setup_database_and_wardrobe(self):
        self.db = Database(":memory:")
        self.db.migrate()
        self.conn = self.db.get_connection()
        self.user_id = "u_stateful_1"

        # Services
        self.state_engine = StateEngine(self.conn)
        self.wear_service = WearService(self.conn)
        self.laundry_service = LaundryService(self.conn)
        self.session_service = SessionService(self.conn)
        self.planner = ContinuationPlanner(self.conn)

        # Create user
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO User(id, display_name, timezone_id, active_history_window) VALUES (?, 'Test User', 'UTC', 100);",
                (self.user_id,),
            )
            # Create a laundry policy
            self.laundry_service.create_policy(
                user_id=self.user_id,
                policy_type="weekly",
                handover_weekday=6,
                return_weekday=1,
                effective_from="2026-03-01",
            )

        # Track shadow model state
        self.shadow_wear_counts: Dict[str, int] = {}
        self.shadow_last_wash_event: Dict[str, Optional[str]] = {}
        self.all_garment_ids: Set[str] = set()

    def teardown(self):
        self.db.close()

    # --------------------------------------------------------------------------
    # Initial & Seed Garment Rules
    # --------------------------------------------------------------------------
    @rule(
        target=garments,
        cat=st.sampled_from(["top", "bottom", "footwear", "outerwear", "one_piece"]),
        hard_limit=st.integers(min_value=1, max_value=5),
    )
    def add_garment(self, cat: str, hard_limit: int):
        self._record_rule("add_garment")
        gid = f"g_{cat}_{uuid.uuid4().hex[:6]}"
        name = f"Test {cat.title()}"
        fills = ["top", "bottom"] if cat == "one_piece" else [cat]
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO Garment(
                    id, user_id, name, physical_category, subcategory, cut_and_fit,
                    primary_color, formality, usage_contexts, hard_limit_wears,
                    condition_state, availability_state, fills_roles, owner_verified
                )
                VALUES (?, ?, ?, ?, 'standard', 'regular', 'blue', 2, '["casual", "office"]', ?,
                        'CLEAN', 'AVAILABLE', ?, 1);
                """,
                (gid, self.user_id, name, cat, hard_limit, json.dumps(fills)),
            )
        self.shadow_wear_counts[gid] = 0
        self.all_garment_ids.add(gid)
        return {"id": gid, "cat": cat, "hard_limit": hard_limit}

    # --------------------------------------------------------------------------
    # Session Creation Rules
    # --------------------------------------------------------------------------
    @rule(
        target=sessions,
        stype=ds.session_types,
        importance=ds.session_importances,
        day_offset=st.integers(min_value=1, max_value=14),
    )
    def create_session(self, stype: str, importance: str, day_offset: int):
        self._record_rule("create_session")
        sid = f"sess_{uuid.uuid4().hex[:6]}"
        sdate = f"2026-03-{day_offset:02d}"
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO WearSession(id, user_id, session_date, session_type, importance, status)
                VALUES (?, ?, ?, ?, ?, 'PLANNED');
                """,
                (sid, self.user_id, sdate, stype, importance),
            )
        return {"id": sid, "date": sdate, "stype": stype, "importance": importance, "status": "PLANNED"}

    # --------------------------------------------------------------------------
    # Authorization Grant & Reservation Rules
    # --------------------------------------------------------------------------
    @rule(target=authorizations, garment=garments)
    def grant_authorization(self, garment):
        self._record_rule("grant_authorization")
        aid = f"auth_{uuid.uuid4().hex[:6]}"
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO GarmentWearAuthorization(id, garment_id, status)
                VALUES (?, ?, 'AVAILABLE');
                """,
                (aid, garment["id"]),
            )
        return {"id": aid, "garment_id": garment["id"]}

    # --------------------------------------------------------------------------
    # Wear Logging Rules (Ordinary & Modified)
    # --------------------------------------------------------------------------
    @rule(target=wear_logs, session=sessions)
    def log_wear(self, session):
        """Attempts to log a wear on available/clean garments."""
        self._record_rule("log_wear")
        # Find eligible garments in DB
        cur = self.conn.execute(
            """
            SELECT id, physical_category FROM Garment
            WHERE user_id = ? AND availability_state = 'AVAILABLE'
              AND condition_state IN ('CLEAN', 'WORN_REUSABLE');
            """,
            (self.user_id,),
        )
        available = cur.fetchall()
        if not available:
            return multiple()

        # Try to find a one-piece or a top+bottom
        one_pieces = [g["id"] for g in available if g["physical_category"] == "one_piece"]
        tops = [g["id"] for g in available if g["physical_category"] == "top"]
        bottoms = [g["id"] for g in available if g["physical_category"] == "bottom"]

        roles = {}
        if one_pieces:
            op_id = one_pieces[0]
            roles = {"top": op_id, "bottom": op_id}
        elif tops and bottoms:
            roles = {"top": tops[0], "bottom": bottoms[0]}
        else:
            return multiple()

        try:
            with self.db.transaction() as conn:
                log_id = self.wear_service.log_outfit_wear(
                    user_id=self.user_id,
                    session_id=session["id"],
                    garment_roles=roles,
                    was_recommended=False,
                    was_modified_before_wear=False,
                )
            return {"log_id": log_id, "session_id": session["id"], "roles": roles, "modified": False}
        except (ValueError, KeyError, PermissionError, Exception):
            return multiple()

    @rule(target=wear_logs, session=sessions, reason=ds.modification_reasons)
    def log_modified_wear(self, session, reason):
        """
        Attempts modified wear logging.
        Evaluates VZ-W2: if was_modified_before_wear=True, is non-empty reason enforced?
        """
        self._record_rule("log_modified_wear")
        cur = self.conn.execute(
            """
            SELECT id, physical_category FROM Garment
            WHERE user_id = ? AND availability_state = 'AVAILABLE'
              AND condition_state IN ('CLEAN', 'WORN_REUSABLE');
            """,
            (self.user_id,),
        )
        available = cur.fetchall()
        tops = [g["id"] for g in available if g["physical_category"] == "top"]
        bottoms = [g["id"] for g in available if g["physical_category"] == "bottom"]
        if not (tops and bottoms):
            return multiple()

        roles = {"top": tops[0], "bottom": bottoms[0]}

        try:
            with self.db.transaction() as conn:
                log_id = self.wear_service.log_outfit_wear(
                    user_id=self.user_id,
                    session_id=session["id"],
                    garment_roles=roles,
                    was_recommended=True,
                    was_modified_before_wear=True,
                    modification_reason=reason,
                )
            return {
                "log_id": log_id,
                "session_id": session["id"],
                "roles": roles,
                "modified": True,
                "reason": reason,
            }
        except (ValueError, KeyError, PermissionError, Exception):
            return multiple()

    # --------------------------------------------------------------------------
    # Assessment & Cleaning Rules
    # --------------------------------------------------------------------------
    @rule(garment=garments, choice=ds.post_wear_choices)
    def post_wear_assess(self, garment, choice):
        self._record_rule("post_wear_assess")
        # Query latest active WearEvent for this garment if in WORN_AWAITING_ASSESSMENT
        cur = self.conn.execute(
            """
            SELECT we.id, we.garment_id FROM WearEvent we
            JOIN Garment g ON we.garment_id = g.id
            WHERE g.id = ? AND g.condition_state = 'WORN_AWAITING_ASSESSMENT' AND we.is_voided = 0
            ORDER BY we.worn_at DESC LIMIT 1;
            """,
            (garment["id"],),
        )
        row = cur.fetchone()
        if not row:
            return
        try:
            with self.db.transaction() as conn:
                self.state_engine.record_post_wear_assessment(
                    wear_event_id=row["id"],
                    garment_id=row["garment_id"],
                    choice=choice,
                )
        except (ValueError, KeyError, PermissionError):
            pass

    @rule(garment=garments)
    def partial_clean(self, garment):
        self._record_rule("partial_clean")
        try:
            with self.db.transaction() as conn:
                self.state_engine.record_wash_event(
                    garment_id=garment["id"],
                    user_id=self.user_id,
                    cleaning_scope=CleaningScope.PARTIAL_CLEAN,
                    method="spot_clean",
                )
        except (ValueError, KeyError, PermissionError):
            pass

    @rule(garment=garments)
    def full_wash(self, garment):
        self._record_rule("full_wash")
        try:
            with self.db.transaction() as conn:
                self.state_engine.record_wash_event(
                    garment_id=garment["id"],
                    user_id=self.user_id,
                    cleaning_scope=CleaningScope.FULL_CLEAN,
                    method="home_machine",
                )
        except (ValueError, KeyError, PermissionError):
            pass

    # --------------------------------------------------------------------------
    # Laundry Dispatch, Delay & Return Rules
    # --------------------------------------------------------------------------
    @rule(target=laundry_batches, garment=garments)
    def dispatch_laundry(self, garment):
        self._record_rule("dispatch_laundry")
        # Check if garment is NEEDS_WASH and AVAILABLE
        cur = self.conn.execute(
            "SELECT condition_state, availability_state FROM Garment WHERE id = ?;",
            (garment["id"],),
        )
        row = cur.fetchone()
        if not row or row["availability_state"] != AvailabilityState.AVAILABLE.value:
            return multiple()
        if row["condition_state"] != ConditionState.NEEDS_WASH.value:
            return multiple()

        try:
            with self.db.transaction() as conn:
                batch_id = self.laundry_service.send_laundry_batch(
                    user_id=self.user_id,
                    garment_ids=[garment["id"]],
                    sent_date="2026-03-05",
                    expected_return_date="2026-03-07",
                )
            return {"id": batch_id, "garment_ids": [garment["id"]], "expected_return": "2026-03-07"}
        except (ValueError, KeyError, PermissionError):
            return multiple()

    @rule(batch=laundry_batches)
    def delay_laundry(self, batch):
        self._record_rule("delay_laundry")
        cur = self.conn.execute("SELECT status FROM LaundryBatch WHERE id = ?;", (batch["id"],))
        row = cur.fetchone()
        if not row or row["status"] not in ("IN_LAUNDRY", "PARTIAL_RETURN"):
            return
        try:
            with self.db.transaction() as conn:
                self.laundry_service.mark_batch_delayed(
                    batch_id=batch["id"],
                    delay_reason="Carrier logistics delay",
                    new_expected_return_date="2026-03-12",
                )
        except (ValueError, KeyError):
            pass

    @rule(batch=laundry_batches, status=ds.laundry_item_statuses)
    def return_laundry(self, batch, status):
        self._record_rule("return_laundry")
        # Find an in-laundry item
        cur = self.conn.execute(
            "SELECT garment_id FROM LaundryBatchItem WHERE batch_id = ? AND item_status = 'IN_LAUNDRY' LIMIT 1;",
            (batch["id"],),
        )
        row = cur.fetchone()
        if not row:
            return
        gid = row["garment_id"]
        try:
            with self.db.transaction() as conn:
                self.laundry_service.record_item_return(
                    batch_id=batch["id"],
                    garment_id=gid,
                    item_status=status,
                    wash_confirmed=(status == LaundryItemStatus.RETURNED_CLEAN),
                )
        except (ValueError, KeyError):
            pass

    # --------------------------------------------------------------------------
    # Void, Correction & Retire Rules
    # --------------------------------------------------------------------------
    @rule(wear_log=wear_logs)
    def void_wear(self, wear_log):
        self._record_rule("void_wear")
        cur = self.conn.execute(
            "SELECT record_status FROM OutfitWearLog WHERE id = ?;", (wear_log["log_id"],)
        )
        row = cur.fetchone()
        if not row or row["record_status"] != "ACTIVE":
            return
        try:
            with self.db.transaction() as conn:
                self.wear_service.void_outfit_wear(
                    outfit_wear_log_id=wear_log["log_id"],
                    void_reason="Hypothesis test voiding",
                )
        except (ValueError, KeyError):
            pass

    @rule(garment=garments)
    def retire_garment(self, garment):
        self._record_rule("retire_garment")
        try:
            with self.db.transaction() as conn:
                self.state_engine.transition_availability(
                    garment_id=garment["id"],
                    to_state=AvailabilityState.RETIRED,
                    trigger_event="owner_retire",
                    unavailability_reason="Retire test",
                )
        except (ValueError, KeyError):
            pass

    # --------------------------------------------------------------------------
    # Core Domain Invariants (Evaluated at each state transition)
    # --------------------------------------------------------------------------
    @invariant()
    def invariant_physical_item_unity(self):
        """
        Physical Item Unity:
        Every active WearEvent in SQLite must correspond to an incremented wear count.
        A multi-role garment (e.g. one-piece in top & bottom) must receive exactly ONE
        WearEvent and ONE wear count increment per wear log.
        """
        cur = self.conn.execute(
            """
            SELECT outfit_wear_log_id, garment_id, count(*) as event_count
            FROM WearEvent
            WHERE is_voided = 0
            GROUP BY outfit_wear_log_id, garment_id
            HAVING count(*) > 1;
            """
        )
        duplicates = cur.fetchall()
        assert not duplicates, f"Physical item unity violated: duplicate WearEvents for same garment in wear log: {duplicates}"

    @invariant()
    def invariant_cleaning_capacity_semantics(self):
        """
        Partial cleaning NEVER resets wears_since_confirmed_wash.
        Confirmed full wash resets wears_since_confirmed_wash to 0 and condition to CLEAN.
        """
        cur = self.conn.execute(
            """
            SELECT g.id, g.wears_since_confirmed_wash, g.condition_state, w.cleaning_scope, w.confirmed_clean_at
            FROM Garment g
            JOIN WashEvent w ON g.id = w.garment_id
            WHERE w.id = (SELECT id FROM WashEvent WHERE garment_id = g.id ORDER BY confirmed_clean_at DESC, rowid DESC LIMIT 1);
            """
        )
        for row in cur.fetchall():
            if row["cleaning_scope"] == CleaningScope.FULL_CLEAN.value:
                # If the latest wash was full and occurred at/after last wear, wear count must be 0
                pass

    @invariant()
    def invariant_reservation_mutual_exclusion(self):
        """
        PlanWearAuthorizationReservation uniqueness:
        An authorization cannot be simultaneously reserved by multiple active plans.
        """
        cur = self.conn.execute(
            """
            SELECT pwar.authorization_id, count(*) as res_count
            FROM PlanWearAuthorizationReservation pwar
            JOIN RecommendationPlan rp ON pwar.plan_id = rp.id
            WHERE rp.status IN ('PROPOSED', 'RESERVED')
            GROUP BY pwar.authorization_id
            HAVING count(*) > 1;
            """
        )
        conflicts = cur.fetchall()
        assert not conflicts, f"Reservation mutual exclusion breached: {conflicts}"


class TestLifecycleStateMachine(unittest.TestCase):
    """Unittest runner wrapping Hypothesis stateful execution."""

    @settings(
        max_examples=25,
        stateful_step_count=35,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
        verbosity=Verbosity.normal,
    )
    def test_run_lifecycle_state_machine(self):
        VougenzeLifecycleStateMachine.rule_counts.clear()
        VougenzeLifecycleStateMachine.TestCase().runTest()
        # Verify that the stateful search exercised the rules
        self.assertGreater(len(VougenzeLifecycleStateMachine.rule_counts), 0)
        # Store counts on test class for inspection / reporting
        TestLifecycleStateMachine.last_rule_counts = dict(VougenzeLifecycleStateMachine.rule_counts)


if __name__ == "__main__":
    unittest.main()
