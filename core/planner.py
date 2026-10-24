"""
Vougenze Bounded Continuation Planner & Multi-Strategy Engine (Frozen Spec 2.14, Part 4).
Implements beam search across the planning horizon, strict ascending lexicographic ranking,
within-plan repetition propagation, hard-limit authorization reservations,
feasible-substitute counting, and slot-by-slot strategy diversity relative to Safe.
"""
from __future__ import annotations
import copy
import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, Set, Tuple

from core.models import (
    StrategyMode,
    PlanStatus,
    InvalidationReason,
    OutfitCandidate,
    AvailabilityState,
)
from core.candidate_generator import CandidateGenerator
from core.scorers import DeterministicScorer, ScoringWeights
from core.wear_service import WearService


@dataclass
class PartialPlan:
    """A partial beam path during continuation planning."""
    assignments: Dict[str, OutfitCandidate] = field(default_factory=dict)  # session_id -> candidate
    simulated_wears: Dict[str, int] = field(default_factory=dict)  # garment_id -> wear_count
    simulated_last_worn: Dict[str, str] = field(default_factory=dict)  # garment_id -> session_date
    reserved_authorizations: Dict[str, str] = field(default_factory=dict)  # (sid, gid) or gid -> auth_id
    uncovered_high: int = 0
    uncovered_normal: int = 0
    uncovered_low: int = 0
    uncertain_dependencies: int = 0
    fallback_count: int = 0
    total_quality: float = 0.0
    total_repetition: float = 0.0
    min_high_quality: float = 1.0


class ContinuationPlanner:
    """Bounded beam search planner for multi-session horizon planning."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.candidate_generator = CandidateGenerator(conn)
        self.wear_service = WearService(conn)

    @staticmethod
    def _utc_now() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    @staticmethod
    def plan_ranking_key(p: PartialPlan) -> tuple:
        """Frozen ascending lexicographic ranking key:
        (uncovered_high, uncovered_normal, uncovered_low, uncertain_dependencies, fallback_count, -min_high_quality, -total_quality, total_repetition)
        """
        return (
            p.uncovered_high,
            p.uncovered_normal,
            p.uncovered_low,
            p.uncertain_dependencies,
            p.fallback_count,
            -p.min_high_quality,
            -p.total_quality,
            p.total_repetition,
        )

    def generate_plan(
        self,
        user_id: str,
        horizon_start_date: str,
        horizon_end_date: str,
        strategy_mode: StrategyMode = StrategyMode.BALANCED,
        beam_width: int = 5,
        reference_safe_assignments: Optional[Dict[str, Tuple[str, ...]]] = None,
    ) -> str:
        """
        Executes bounded continuation planning across the horizon.
        Uses ascending lexicographic ranking and within-plan repetition.
        """
        # 1. Fetch sessions
        sessions_cur = self.conn.execute(
            """
            SELECT * FROM WearSession
            WHERE user_id = ? AND session_date >= ? AND session_date <= ?
              AND status != 'CANCELLED'
            ORDER BY session_date ASC, start_time ASC, id ASC;
            """,
            (user_id, horizon_start_date, horizon_end_date),
        )
        sessions = sessions_cur.fetchall()
        session_lookup = {s["id"]: s for s in sessions}

        history = self.wear_service.query_active_history(user_id, horizon_start_date, window_days=100)

        # Scorer weights
        weights = ScoringWeights()
        if strategy_mode == StrategyMode.SAFE:
            weights.w_repetition = 0.15
            weights.w_compatibility = 0.40
            weights.w_occasion = 0.35
            weights.w_preference = 0.25
        elif strategy_mode == StrategyMode.EXPLORATORY:
            weights.w_preference = 0.35
            weights.w_occasion = 0.35
            weights.w_compatibility = 0.30
        scorer = DeterministicScorer(self.conn, weights)

        # 2. Beam Search Initialization
        initial_plan = PartialPlan()
        g_cur = self.conn.execute(
            "SELECT id, wears_since_confirmed_wash FROM Garment WHERE user_id = ?;", (user_id,)
        )
        for grow in g_cur.fetchall():
            initial_plan.simulated_wears[grow["id"]] = grow["wears_since_confirmed_wash"]

        beam: List[PartialPlan] = [initial_plan]

        hl_cur = self.conn.execute(
            "SELECT id, hard_limit_wears FROM Garment WHERE user_id = ?;", (user_id,)
        )
        hard_limits = {row["id"]: row["hard_limit_wears"] for row in hl_cur.fetchall()}

        for session in sessions:
            sid = session["id"]
            importance = session["importance"]
            next_beam: List[PartialPlan] = []

            # Generate all structurally valid candidates for this session
            candidates = self.candidate_generator.generate_candidates(sid, user_id=user_id)

            # Compute feasible substitute counts per role for this session
            substitute_counts: Dict[str, Set[str]] = {}
            for cand in candidates:
                for role, gid in cand.roles.items():
                    substitute_counts.setdefault(role, set()).add(gid)
            feasible_substitute_counts = {role: len(gids) for role, gids in substitute_counts.items()}

            for partial in beam:
                # Within-plan simulated wear history for repetition scoring
                sim_history = [
                    {
                        "session_date": session_lookup[prev_sid]["session_date"],
                        "session_type": session_lookup[prev_sid]["session_type"],
                        "garment_ids": prev_cand.garment_ids,
                    }
                    for prev_sid, prev_cand in partial.assignments.items()
                ]

                # Filter candidates feasible under partial plan's simulated wear and authorizations
                feasible_candidates: List[Tuple[OutfitCandidate, Dict[str, str]]] = []
                for cand in candidates:
                    cand_feasible = True
                    needed_auths: Dict[str, str] = {}
                    for gid in cand.garment_ids:
                        sim_wears = partial.simulated_wears.get(gid, 0)
                        hl = hard_limits.get(gid)
                        if hl is not None and sim_wears >= hl:
                            # Must reserve an available authorization not already reserved in this path
                            reserved_auth_ids = set(partial.reserved_authorizations.values())
                            auth_cur = self.conn.execute(
                                """
                                SELECT id FROM GarmentWearAuthorization
                                WHERE garment_id = ? AND status = 'AVAILABLE' AND consumed_by_wear_event_id IS NULL
                                ORDER BY authorized_at ASC;
                                """,
                                (gid,),
                            )
                            auth_rows = auth_cur.fetchall()
                            usable_auth = None
                            for a in auth_rows:
                                if a["id"] not in reserved_auth_ids:
                                    usable_auth = a["id"]
                                    break
                            if usable_auth:
                                needed_auths[gid] = usable_auth
                            else:
                                cand_feasible = False
                                break

                    if cand_feasible:
                        feasible_candidates.append((cand, needed_auths))

                if not feasible_candidates:
                    p_copy = copy.deepcopy(partial)
                    if importance == "HIGH":
                        p_copy.uncovered_high += 1
                        p_copy.min_high_quality = 0.0
                    elif importance == "NORMAL":
                        p_copy.uncovered_normal += 1
                    else:
                        p_copy.uncovered_low += 1
                    next_beam.append(p_copy)
                    continue

                for cand, needed_auths in feasible_candidates:
                    # Score candidate in context of this partial plan path
                    q = scorer.score_candidate(
                        candidate=cand,
                        session=session,
                        recent_wear_history=history,
                        feasible_substitute_counts=feasible_substitute_counts,
                        simulated_wear_history=sim_history,
                    )

                    effective_q = q
                    if strategy_mode == StrategyMode.EXPLORATORY:
                        if importance in ("LOW", "NORMAL"):
                            novelty_bonus = sum(
                                0.05 for gid in cand.garment_ids if partial.simulated_wears.get(gid, 0) == 0
                            )
                            effective_q += novelty_bonus

                    p_next = copy.deepcopy(partial)
                    p_next.assignments[sid] = cand
                    p_next.reserved_authorizations.update(needed_auths)
                    p_next.total_quality += effective_q
                    p_next.total_repetition += cand.repetition_penalty
                    p_next.fallback_count += cand.fallback_count

                    if importance == "HIGH":
                        p_next.min_high_quality = min(p_next.min_high_quality, effective_q)

                    # Advance simulated wear state
                    for gid in cand.garment_ids:
                        p_next.simulated_wears[gid] = p_next.simulated_wears.get(gid, 0) + 1
                        p_next.simulated_last_worn[gid] = session["session_date"]

                    next_beam.append(p_next)

            next_beam.sort(key=self.plan_ranking_key)
            beam = next_beam[:beam_width]

        best_partial = beam[0] if beam else PartialPlan()

        # 3. Strategy Diversity Calculation
        # Safe is baseline; Balanced and Exploratory measure diversity slot-by-slot relative to Safe
        current_signatures = {
            sid: tuple(sorted(cand.garment_ids))
            for sid, cand in best_partial.assignments.items()
        }

        if reference_safe_assignments:
            diff_slots = sum(
                1 for sid in best_partial.assignments
                if sid in reference_safe_assignments and current_signatures[sid] != reference_safe_assignments[sid]
            )
            diversity_achieved = diff_slots
        else:
            unique_outfits = set(current_signatures.values())
            diversity_achieved = max(0, len(unique_outfits) - 1)

        diversity_target = 2 if strategy_mode == StrategyMode.EXPLORATORY else 1
        diversity_notes = "Normal multi-outfit rotation"
        if len(set(current_signatures.values())) <= 1:
            diversity_notes = "Limited wardrobe: exactly 1 valid outfit configuration available"

        # 4. Persist RecommendationPlan
        plan_id = f"rp_{uuid.uuid4().hex[:12]}"
        now = self._utc_now()

        self.conn.execute(
            """
            INSERT INTO RecommendationPlan(
                id, user_id, strategy_mode, horizon_start_date, horizon_end_date,
                status, diversity_target, diversity_achieved, diversity_notes, created_at
            )
            VALUES (?, ?, ?, ?, ?, 'PROPOSED', ?, ?, ?, ?);
            """,
            (
                plan_id,
                user_id,
                strategy_mode.value,
                horizon_start_date,
                horizon_end_date,
                diversity_target,
                diversity_achieved,
                diversity_notes,
                now,
            ),
        )

        # Persist PlanEvaluation
        self.conn.execute(
            """
            INSERT INTO PlanEvaluation(
                plan_id, uncovered_high, uncovered_normal, uncovered_low,
                uncertain_dependencies, fallback_assignments, min_high_importance_quality,
                total_quality, repetition_burden
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                plan_id,
                best_partial.uncovered_high,
                best_partial.uncovered_normal,
                best_partial.uncovered_low,
                best_partial.uncertain_dependencies,
                best_partial.fallback_count,
                round(best_partial.min_high_quality, 4) if sessions else 1.0,
                round(best_partial.total_quality, 4),
                round(best_partial.total_repetition, 4),
            ),
        )

        # Persist PlanSessionAssignment and PlanAssignmentItem
        for sid, cand in best_partial.assignments.items():
            assign_id = f"psa_{uuid.uuid4().hex[:12]}"
            self.conn.execute(
                """
                INSERT INTO PlanSessionAssignment(
                    id, plan_id, session_id, assignment_status, quality_score,
                    compatibility_score, preference_score, repetition_penalty,
                    is_fallback
                )
                VALUES (?, ?, ?, 'PROPOSED', ?, ?, ?, ?, ?);
                """,
                (
                    assign_id,
                    plan_id,
                    sid,
                    cand.quality_score,
                    cand.compatibility_score,
                    cand.preference_score,
                    cand.repetition_penalty,
                    1 if cand.is_fallback else 0,
                ),
            )

            for role, gid in cand.roles.items():
                self.conn.execute(
                    """
                    INSERT INTO PlanAssignmentItem(assignment_id, garment_id, outfit_role)
                    VALUES (?, ?, ?);
                    """,
                    (assign_id, gid, role),
                )

        return plan_id

    def retire_garment(self, garment_id: str, reason: str = "Retired by owner") -> None:
        """Scenario 23: Retires a garment and atomically invalidates all active proposed/reserved plans referencing it."""
        self.wear_service.retire_garment(garment_id, reason)
