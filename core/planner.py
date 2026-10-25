"""
Vougenze Bounded Continuation Planner & Multi-Strategy Engine (Frozen Spec 2.14, Part 4).
Implements beam search across the planning horizon, strict ascending lexicographic ranking,
within-plan repetition propagation, (session_id, garment_id) hard-limit authorization reservations,
persisted plan reservations, candidate-specific substitute counting, and enforced strategy diversity.
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
    RecommendationPlan,
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
    reserved_authorizations: Dict[Tuple[str, str], str] = field(default_factory=dict)  # (sid, gid) -> auth_id
    diversity_progress: int = 0
    uncovered_high: int = 0
    uncovered_normal: int = 0
    uncovered_low: int = 0
    uncertain_dependencies: int = 0
    fallback_count: int = 0
    total_quality: float = 0.0
    total_repetition: float = 0.0
    min_high_quality: Optional[float] = None


class ContinuationPlanner:
    """Bounded beam search planner for multi-session horizon planning."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.candidate_generator = CandidateGenerator(conn)
        self.wear_service = WearService(conn)

    @staticmethod
    def _utc_now() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _require_transaction(self) -> None:
        if not self.conn.in_transaction:
            raise RuntimeError("Planner operations require an active transaction")

    @staticmethod
    def plan_ranking_key(p: PartialPlan, target_diversity: int = 0, attainable_target: Optional[int] = None) -> tuple:
        """
        Ascending lexicographic ranking key enforcing:
        1. Feasibility (uncovered high, normal, low, uncertain dependencies, fallback items)
        2. Attainable diversity target deficit (for Balanced / Exploratory relative to Safe)
        3. Quality and repetition (-min_high_quality, -total_quality, total_repetition)
        """
        target = attainable_target if attainable_target is not None else target_diversity
        diversity_deficit = max(0, target - p.diversity_progress)
        high_floor = p.min_high_quality if p.min_high_quality is not None else 1.0
        return (
            p.uncovered_high,
            p.uncovered_normal,
            p.uncovered_low,
            p.uncertain_dependencies,
            p.fallback_count,
            diversity_deficit,
            -high_floor,
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
        Persists authorization reservations and enforces attainable strategy diversity.
        """
        self._require_transaction()
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

        # Common base quality definition
        weights = ScoringWeights()
        weights.w_compatibility = 0.30
        weights.w_occasion = 0.35
        weights.w_preference = 0.25
        weights.w_repetition = 0.10
        scorer = DeterministicScorer(self.conn, weights)

        # 2. Determine attainable diversity target relative to Safe
        sessions_with_alternatives = 0
        eligible_low_normal_sessions = 0
        all_session_candidates: Dict[str, List[OutfitCandidate]] = {}

        for session in sessions:
            sid = session["id"]
            cands = self.candidate_generator.generate_candidates(sid, user_id=user_id)
            all_session_candidates[sid] = cands
            if len(cands) >= 2:
                sessions_with_alternatives += 1
            if session["importance"] in ("LOW", "NORMAL"):
                eligible_low_normal_sessions += 1

        if strategy_mode == StrategyMode.SAFE or not reference_safe_assignments:
            attainable_target = 0
        elif strategy_mode == StrategyMode.BALANCED:
            attainable_target = min(2, sessions_with_alternatives)
        else:  # EXPLORATORY
            attainable_target = min(3, eligible_low_normal_sessions)

        # 3. Beam Search Initialization
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
            candidates = all_session_candidates.get(sid, [])
            next_beam: List[PartialPlan] = []

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
                feasible_candidates: List[Tuple[OutfitCandidate, Dict[Tuple[str, str], str]]] = []
                for cand in candidates:
                    cand_feasible = True
                    needed_auths: Dict[Tuple[str, str], str] = {}
                    for gid in cand.garment_ids:
                        sim_wears = partial.simulated_wears.get(gid, 0)
                        hl = hard_limits.get(gid)
                        if hl is not None and sim_wears >= hl:
                            # Reserve distinct authorization per (sid, gid)
                            reserved_auth_ids = set(partial.reserved_authorizations.values())
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
                                ORDER BY gwa.authorized_at ASC;
                                """,
                                (gid,),
                            )
                            auth_rows = auth_cur.fetchall()
                            usable_auth = None
                            for a in auth_rows:
                                if a["id"] not in reserved_auth_ids and a["id"] not in needed_auths.values():
                                    usable_auth = a["id"]
                                    break
                            if usable_auth:
                                needed_auths[(sid, gid)] = usable_auth
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

                path_cands = [fc[0] for fc in feasible_candidates]

                for cand, needed_auths in feasible_candidates:
                    # Scenario 28: Selected-candidate-specific complete outfit substitutes
                    substitutes = CandidateGenerator.compute_candidate_substitutes(path_cands, cand)

                    # VZ-P1: Evaluate path-isolated copy so path-dependent scoring
                    # does not mutate the shared OutfitCandidate across different beam paths
                    evaluated_cand = copy.deepcopy(cand)

                    q = scorer.score_candidate(
                        candidate=evaluated_cand,
                        session=session,
                        recent_wear_history=history,
                        feasible_substitute_counts=substitutes,
                        simulated_wear_history=sim_history,
                    )

                    # Keep deterministic quality semantically stable. Exploration is
                    # represented only through the diversity coordinate, never by
                    # mutating or inflating persisted quality.
                    effective_q = q

                    p_next = copy.deepcopy(partial)
                    p_next.assignments[sid] = evaluated_cand
                    p_next.reserved_authorizations.update(needed_auths)
                    p_next.total_quality += effective_q
                    p_next.total_repetition += evaluated_cand.repetition_penalty
                    p_next.fallback_count += evaluated_cand.fallback_count

                    # Track slot-level diversity progress against reference Safe plan
                    if reference_safe_assignments and sid in reference_safe_assignments:
                        cand_sig = tuple(sorted(evaluated_cand.garment_ids))
                        if cand_sig != reference_safe_assignments[sid]:
                            if strategy_mode == StrategyMode.BALANCED:
                                p_next.diversity_progress += 1
                            elif strategy_mode == StrategyMode.EXPLORATORY and importance in ("LOW", "NORMAL"):
                                p_next.diversity_progress += 1

                    if importance == "HIGH":
                        p_next.min_high_quality = (
                            effective_q
                            if p_next.min_high_quality is None
                            else min(p_next.min_high_quality, effective_q)
                        )

                    # Advance simulated wear state
                    for gid in evaluated_cand.garment_ids:
                        p_next.simulated_wears[gid] = p_next.simulated_wears.get(gid, 0) + 1
                        p_next.simulated_last_worn[gid] = session["session_date"]

                    next_beam.append(p_next)

            next_beam.sort(key=lambda p: self.plan_ranking_key(p, attainable_target))
            beam = next_beam[:beam_width]

        best_partial = beam[0] if beam else PartialPlan()

        # 4. Strategy Diversity Calculation
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

        diversity_notes = "Normal multi-outfit rotation"
        if len(set(current_signatures.values())) <= 1:
            diversity_notes = "Limited wardrobe: exactly 1 valid outfit configuration available"

        # 5. Persist RecommendationPlan
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
                attainable_target,
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
                round(best_partial.min_high_quality, 4) if best_partial.min_high_quality is not None else None,
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

        # Persist PlanWearAuthorizationReservation
        for (res_sid, res_gid), auth_id in best_partial.reserved_authorizations.items():
            if res_sid not in best_partial.assignments or res_gid not in best_partial.assignments[res_sid].garment_ids:
                continue
            auth_check = self.conn.execute(
                "SELECT id FROM GarmentWearAuthorization WHERE id = ? AND garment_id = ? AND status = 'AVAILABLE';",
                (auth_id, res_gid),
            ).fetchone()
            if not auth_check:
                raise ValueError(f"Authorization {auth_id} does not belong to garment {res_gid} or is not AVAILABLE")
            self.conn.execute(
                """
                INSERT INTO PlanWearAuthorizationReservation(
                    plan_id, session_id, garment_id, authorization_id, created_at
                )
                VALUES (?, ?, ?, ?, ?);
                """,
                (plan_id, res_sid, res_gid, auth_id, now),
            )

        return plan_id

    def supersede_plan(self, plan_id: str, reason: str = "SUPERSEDED") -> None:
        """Explicitly supersedes a plan and releases its authorization reservations."""
        self._require_transaction()
        self.conn.execute(
            "UPDATE RecommendationPlan SET status = 'SUPERSEDED', invalidation_reason = ? WHERE id = ?;",
            (reason, plan_id),
        )
        self.conn.execute(
            "DELETE FROM PlanWearAuthorizationReservation WHERE plan_id = ?;",
            (plan_id,),
        )

    def retire_garment(self, garment_id: str, reason: str = "Retired by owner") -> None:
        """Scenario 23: Retires a garment and atomically invalidates all active proposed/reserved plans referencing it."""
        self.wear_service.retire_garment(garment_id, reason)

    def get_plan(self, plan_id: str) -> RecommendationPlan:
        """Loads RecommendationPlan and its assignments from the database."""
        p_row = self.conn.execute("SELECT * FROM RecommendationPlan WHERE id = ?;", (plan_id,)).fetchone()
        if not p_row:
            raise KeyError(f"RecommendationPlan '{plan_id}' not found")

        assign_cur = self.conn.execute(
            """
            SELECT psa.*, pai.garment_id, pai.outfit_role
            FROM PlanSessionAssignment psa
            JOIN PlanAssignmentItem pai ON psa.id = pai.assignment_id
            WHERE psa.plan_id = ?
            ORDER BY psa.session_id, pai.outfit_role;
            """,
            (plan_id,),
        )
        assignments_by_sid: Dict[str, Dict[str, Any]] = {}
        for r in assign_cur.fetchall():
            sid = r["session_id"]
            if sid not in assignments_by_sid:
                assignments_by_sid[sid] = {
                    "id": r["id"],
                    "roles": {},
                    "garment_ids": [],
                    "quality_score": r["quality_score"],
                    "compatibility_score": r["compatibility_score"],
                    "preference_score": r["preference_score"],
                    "repetition_penalty": r["repetition_penalty"],
                    "is_fallback": bool(r["is_fallback"]),
                }
            assignments_by_sid[sid]["roles"][r["outfit_role"]] = r["garment_id"]
            if r["garment_id"] not in assignments_by_sid[sid]["garment_ids"]:
                assignments_by_sid[sid]["garment_ids"].append(r["garment_id"])

        cands = {
            sid: OutfitCandidate(
                id=d["id"],
                garment_ids=d["garment_ids"],
                roles=d["roles"],
                quality_score=d["quality_score"],
                compatibility_score=d["compatibility_score"],
                preference_score=d["preference_score"],
                repetition_penalty=d["repetition_penalty"],
                is_fallback=d["is_fallback"],
            )
            for sid, d in assignments_by_sid.items()
        }

        return RecommendationPlan(
            id=p_row["id"],
            user_id=p_row["user_id"],
            strategy_mode=StrategyMode(p_row["strategy_mode"]),
            horizon_start_date=p_row["horizon_start_date"],
            horizon_end_date=p_row["horizon_end_date"],
            status=PlanStatus(p_row["status"]),
            invalidation_reason=InvalidationReason(p_row["invalidation_reason"]) if p_row["invalidation_reason"] else None,
            superseded_by_id=p_row["superseded_by_id"],
            plan_input_fingerprint=p_row["plan_input_fingerprint"],
            diversity_target=p_row["diversity_target"],
            diversity_achieved=p_row["diversity_achieved"],
            diversity_notes=p_row["diversity_notes"],
            assignments=cands,
            created_at=p_row["created_at"],
        )
