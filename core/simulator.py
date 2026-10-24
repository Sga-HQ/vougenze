"""
Vougenze Headless Wardrobe Simulator (Frozen Spec Part 1.4, Part 3, Part 4, Part 5).
Executes multi-day/multi-week realistic lifecycle simulations to validate:
- Longitudinal state transitions across condition & availability dimensions
- Bounded continuation planning & candidate rotation
- Wear logging, post-wear reviews, and laundry batch lifecycles
- Preference evidence accumulation
- SQLite storage economics and database invariants
"""
from __future__ import annotations
import json
import os
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, List, Dict, Any

from core.models import (
    ConditionState,
    AvailabilityState,
    PostWearChoice,
    CleaningScope,
    StrategyMode,
    LaundryItemStatus,
    OutfitCandidate,
)
from core.state_engine import StateEngine
from core.wear_service import WearService
from core.laundry_service import LaundryService
from core.session_service import SessionService
from core.planner import ContinuationPlanner
from core.evidence_service import PreferenceEvidenceService


@dataclass
class SimulationResult:
    total_days: int
    sessions_materialized: int
    wears_logged: int
    laundry_batches_processed: int
    preference_evidence_events: int
    db_size_bytes: int
    foreign_keys_valid: bool
    domain_invariants_valid: bool
    invariants_passed: bool
    summary_notes: str


class LifecycleSmokeSimulator:
    """Headless smoke harness for multi-day longitudinal wardrobe lifecycle simulation."""

    def __init__(self, conn: sqlite3.Connection, user_id: str = "sim_user", db_file_path: Optional[str] = None):
        self.conn = conn
        self.user_id = user_id
        self.db_file_path = db_file_path

        self.state_engine = StateEngine(conn)
        self.wear_service = WearService(conn)
        self.laundry_service = LaundryService(conn)
        self.session_service = SessionService(conn)
        self.planner = ContinuationPlanner(conn)
        self.evidence_service = PreferenceEvidenceService(conn)

    def seed_initial_wardrobe(self) -> None:
        """Seeds a diverse capsule wardrobe with varied categories, formalities, and wear limits."""
        # Ensure user exists
        self.conn.execute(
            """
            INSERT OR IGNORE INTO User(id, display_name, timezone_id, active_history_window)
            VALUES (?, 'Simulator User', 'UTC', 100);
            """,
            (self.user_id,),
        )

        # Weekly laundry policy: laundry sent on Sundays (weekday 6), expected Tuesday (weekday 1)
        self.laundry_service.create_policy(
            user_id=self.user_id,
            policy_type="weekly",
            handover_weekday=6,
            return_weekday=1,
            effective_from="2026-03-01",
        )

        # Capsule garments
        garments = [
            # Tops
            ("top_white_shirt", "Crisp White Shirt", "top", "shirt", "tailored", "white", 4, '["office", "presentation"]', 1),
            ("top_blue_oxford", "Blue Oxford Shirt", "top", "shirt", "regular", "blue", 3, '["university", "office"]', 2),
            ("top_grey_tee", "Grey Graphic Tee", "top", "tee", "regular", "grey", 1, '["casual", "home_casual"]', 2),
            ("top_navy_polo", "Navy Polo", "top", "polo", "regular", "navy", 2, '["casual", "university"]', 2),
            ("top_linen_shirt", "Beige Linen Shirt", "top", "shirt", "relaxed", "beige", 3, '["casual", "party"]', 2),
            # Bottoms
            ("bot_dark_jeans", "Dark Wash Denim", "bottom", "jeans", "slim", "navy", 2, '["casual", "university"]', 5),
            ("bot_khaki_chinos", "Khaki Chinos", "bottom", "chinos", "regular", "beige", 3, '["office", "university"]', 4),
            ("bot_grey_trousers", "Grey Wool Trousers", "bottom", "trousers", "tailored", "grey", 4, '["office", "presentation"]', 3),
            ("bot_casual_shorts", "Linen Casual Shorts", "bottom", "shorts", "relaxed", "olive", 1, '["casual", "home_casual"]', 3),
            # Footwear
            ("shoe_white_sneakers", "Clean White Sneakers", "footwear", "sneakers", "regular", "white", 2, '["casual", "university"]', 10),
            ("shoe_brown_loafers", "Brown Leather Loafers", "footwear", "loafers", "regular", "brown", 4, '["office", "presentation", "party"]', 10),
            ("shoe_running", "Athletic Running Shoes", "footwear", "sneakers", "sporty", "black", 1, '["exercise", "casual"]', 10),
            # Outerwear
            ("outer_blazer", "Navy Wool Blazer", "outerwear", "blazer", "tailored", "navy", 4, '["office", "presentation", "party"]', 8),
            # One-piece
            ("onepiece_jumpsuit", "Black Utility Jumpsuit", "one_piece", "jumpsuit", "relaxed", "black", 3, '["casual", "party"]', 2),
        ]

        for gid, name, cat, subcat, cut, color, form, contexts, hard_limit in garments:
            self.conn.execute(
                """
                INSERT OR IGNORE INTO Garment(
                    id, user_id, name, physical_category, subcategory, cut_and_fit,
                    primary_color, formality, usage_contexts, hard_limit_wears,
                    condition_state, availability_state, owner_verified
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'CLEAN', 'AVAILABLE', 1);
                """,
                (gid, self.user_id, name, cat, subcat, cut, color, form, contexts, hard_limit),
            )

        # Recurring WearSession Templates
        t_id = self.session_service.create_template(
            user_id=self.user_id,
            template_name="Standard Weekly Routine",
            effective_from="2026-03-01",
        )
        self.session_service.add_template_day(
            template_id=t_id,
            day_of_week=0,  # Monday
            session_type="office",
            importance="NORMAL",
            required_roles=["top", "bottom", "footwear"],
        )
        self.session_service.add_template_day(
            template_id=t_id,
            day_of_week=1,  # Tuesday
            session_type="presentation",
            importance="HIGH",
            required_roles=["top", "bottom", "footwear"],
        )
        self.session_service.add_template_day(
            template_id=t_id,
            day_of_week=2,  # Wednesday
            session_type="university",
            importance="NORMAL",
            required_roles=["top", "bottom", "footwear"],
        )
        self.session_service.add_template_day(
            template_id=t_id,
            day_of_week=5,  # Saturday
            session_type="casual",
            importance="LOW",
            required_roles=["top", "bottom", "footwear"],
        )

    def run_simulation(
        self,
        start_date: str = "2026-03-01",
        days: int = 14,
        strategy_mode: StrategyMode = StrategyMode.BALANCED,
    ) -> SimulationResult:
        """Runs day-by-day longitudinal wardrobe simulation."""
        self.seed_initial_wardrobe()

        cur_date = datetime.strptime(start_date, "%Y-%m-%d").date()
        wears_logged = 0
        sessions_materialized = 0
        laundry_batches_processed = 0
        evidence_count = 0

        active_laundry_batch_id: Optional[str] = None
        laundry_return_date: Optional[str] = None

        for day_offset in range(days):
            date_str = cur_date.strftime("%Y-%m-%d")
            weekday = cur_date.weekday()  # 0=Monday, 6=Sunday

            # 1. Materialize routine sessions for today
            mat_ids = self.session_service.materialize_sessions_for_horizon(
                user_id=self.user_id,
                start_date=date_str,
                end_date=date_str,
            )
            sessions_materialized += len(mat_ids)

            # 2. Check Laundry Cycle
            # On Sundays (weekday 6): Send dirty garments to laundry
            if weekday == 6 and not active_laundry_batch_id:
                dirty_garments = self.conn.execute(
                    """
                    SELECT id FROM Garment
                    WHERE user_id = ? AND condition_state = 'NEEDS_WASH' AND availability_state = 'AVAILABLE';
                    """,
                    (self.user_id,),
                ).fetchall()

                if dirty_garments:
                    batch_gids = [r["id"] for r in dirty_garments]
                    ret_date = (cur_date + timedelta(days=2)).strftime("%Y-%m-%d")
                    active_laundry_batch_id = self.laundry_service.send_laundry_batch(
                        user_id=self.user_id,
                        garment_ids=batch_gids,
                        sent_date=date_str,
                        expected_return_date=ret_date,
                    )
                    laundry_return_date = ret_date
                    laundry_batches_processed += 1

            # On Tuesday (weekday 1): Return laundry if due
            if active_laundry_batch_id and date_str >= (laundry_return_date or ""):
                # Return all items clean
                batch_items = self.conn.execute(
                    "SELECT garment_id FROM LaundryBatchItem WHERE batch_id = ?;",
                    (active_laundry_batch_id,),
                ).fetchall()
                for r in batch_items:
                    self.laundry_service.record_item_return(
                        batch_id=active_laundry_batch_id,
                        garment_id=r["garment_id"],
                        item_status=LaundryItemStatus.RETURNED_CLEAN,
                        wash_confirmed=True,
                        return_date=f"{date_str}T10:00:00Z",
                    )
                active_laundry_batch_id = None
                laundry_return_date = None

            # 3. If sessions exist for today, plan & wear
            day_sessions = self.conn.execute(
                """
                SELECT * FROM WearSession
                WHERE user_id = ? AND session_date = ? AND status = 'PLANNED';
                """,
                (self.user_id, date_str),
            ).fetchall()

            if day_sessions:
                # Run continuation planner for horizon (today to today + 3 days)
                horizon_end = (cur_date + timedelta(days=3)).strftime("%Y-%m-%d")
                pid = self.planner.generate_plan(
                    user_id=self.user_id,
                    horizon_start_date=date_str,
                    horizon_end_date=horizon_end,
                    strategy_mode=strategy_mode,
                )

                for session in day_sessions:
                    sid = session["id"]
                    # Get assigned outfit
                    assign = self.conn.execute(
                        "SELECT * FROM PlanSessionAssignment WHERE plan_id = ? AND session_id = ?;",
                        (pid, sid),
                    ).fetchone()

                    if assign:
                        items = self.conn.execute(
                            "SELECT garment_id, outfit_role FROM PlanAssignmentItem WHERE assignment_id = ?;",
                            (assign["id"],),
                        ).fetchall()
                        assigned_garments = [r["garment_id"] for r in items]
                        roles_map = {r["outfit_role"]: r["garment_id"] for r in items}

                        cand = OutfitCandidate(
                            id=f"cand_{sid}",
                            garment_ids=assigned_garments,
                            roles=roles_map,
                            quality_score=assign["quality_score"] or 0.8,
                        )

                        # Capture preference evidence
                        self.evidence_service.record_selection_evidence(
                            user_id=self.user_id,
                            session_id=sid,
                            occasion_context=session["session_type"],
                            selected_candidate=cand,
                            alternatives=[],
                            selection_reason=["planned_continuation_optimal"],
                        )
                        evidence_count += 1

                        # Commit wear log
                        self.wear_service.log_outfit_wear(
                            user_id=self.user_id,
                            session_id=sid,
                            garment_roles=roles_map,
                            source="recommended_selected",
                            session_type=session["session_type"],
                            importance=session["importance"],
                            local_wear_date=date_str,
                            timezone_id="UTC",
                            recommendation_plan_id=pid,
                            plan_assignment_id=assign["id"],
                            was_recommended=1,
                            satisfaction_rating=8,
                        )
                        wears_logged += 1

                        # Post-wear assessment (once per unique garment)
                        for gid in dict.fromkeys(assigned_garments):
                            grow = self.conn.execute(
                                "SELECT wears_since_confirmed_wash, hard_limit_wears FROM Garment WHERE id = ?;",
                                (gid,),
                            ).fetchone()
                            we_row = self.conn.execute(
                                "SELECT id FROM WearEvent WHERE session_id = ? AND garment_id = ?;",
                                (sid, gid),
                            ).fetchone()

                            if we_row:
                                limit = grow["hard_limit_wears"] or 2
                                if grow["wears_since_confirmed_wash"] >= limit:
                                    choice = PostWearChoice.NEEDS_WASH
                                else:
                                    choice = PostWearChoice.READY_TO_REUSE

                                self.state_engine.record_post_wear_assessment(
                                    wear_event_id=we_row["id"],
                                    garment_id=gid,
                                    choice=choice,
                                    notes=f"Simulated day {day_offset} review",
                                    assessed_at=f"{date_str}T22:00:00Z",
                                )

            # Advance to next civil calendar day
            cur_date += timedelta(days=1)

        # 4. Verify SQLite schema invariants & foreign keys
        fk_violations = self.conn.execute("PRAGMA foreign_key_check;").fetchall()
        foreign_keys_valid = len(fk_violations) == 0

        # Domain invariants check
        integrity_check = self.conn.execute("PRAGMA integrity_check;").fetchone()[0]
        orphan_in_laundry = self.conn.execute(
            """
            SELECT count(*) FROM Garment
            WHERE availability_state = 'IN_LAUNDRY' AND current_laundry_batch_id IS NULL;
            """
        ).fetchone()[0]
        domain_invariants_valid = (integrity_check == "ok" and orphan_in_laundry == 0)
        invariants_passed = foreign_keys_valid and domain_invariants_valid

        # 5. Measure storage economics (Spec 1.4)
        db_size = 0
        if self.db_file_path and os.path.exists(self.db_file_path):
            db_size = os.path.getsize(self.db_file_path)
        else:
            try:
                page_count = self.conn.execute("PRAGMA page_count;").fetchone()[0]
                page_size = self.conn.execute("PRAGMA page_size;").fetchone()[0]
                db_size = page_count * page_size
            except Exception:
                db_size = 0

        summary = (
            f"Smoke simulation completed across {days} days. "
            f"Materialized {sessions_materialized} sessions, logged {wears_logged} wears, "
            f"processed {laundry_batches_processed} laundry batches. DB size: {db_size} bytes. "
            f"Foreign keys intact: {foreign_keys_valid}. Domain invariants: {domain_invariants_valid}."
        )

        return SimulationResult(
            total_days=days,
            sessions_materialized=sessions_materialized,
            wears_logged=wears_logged,
            laundry_batches_processed=laundry_batches_processed,
            preference_evidence_events=evidence_count,
            db_size_bytes=db_size,
            foreign_keys_valid=foreign_keys_valid,
            domain_invariants_valid=domain_invariants_valid,
            invariants_passed=invariants_passed,
            summary_notes=summary,
        )


# Backward compatibility alias
WardrobeSimulator = LifecycleSmokeSimulator
