"""
Vougenze Deterministic V0 Scorers (Frozen Spec Part 4, Scenarios 5, 28, 29).
Computes compatibility C(o), occasion fit O(o,s), preference P(o,s), repetition penalty R(o,s),
and session quality Q(o,s) = w_C * C + w_O * O + w_P * P - w_R * R.
Supports transient simulated within-plan wear history, deduplicates one-piece garments,
and evaluates feasible substitute counts.
"""
from __future__ import annotations
import json
import sqlite3
from dataclasses import dataclass
from datetime import date
from typing import Optional, List, Dict, Any, Set, Tuple

from core.models import OutfitCandidate, PriorityLevel


@dataclass
class ScoringWeights:
    w_compatibility: float = 0.30
    w_occasion: float = 0.35
    w_preference: float = 0.25
    w_repetition: float = 0.10


NEUTRALS: Set[str] = {"white", "black", "grey", "navy", "beige", "denim", "tan", "cream"}

SESSION_FORMALITY: Dict[str, Tuple[int, int]] = {
    "sleepwear": (1, 1),
    "home_casual": (1, 2),
    "casual": (1, 3),
    "university": (2, 3),
    "office": (3, 4),
    "presentation": (4, 5),
    "party": (2, 4),
    "formal": (4, 5),
    "traditional": (2, 5),
}


class DeterministicScorer:
    """Computes transparent, deterministic V0 scores for outfit candidates."""

    def __init__(self, conn: sqlite3.Connection, weights: Optional[ScoringWeights] = None):
        self.conn = conn
        self.weights = weights or ScoringWeights()

    def score_candidate(
        self,
        candidate: OutfitCandidate,
        session: sqlite3.Row,
        recent_wear_history: List[sqlite3.Row],
        feasible_substitute_counts: Optional[Dict[str, int]] = None,
        user_garment_counts_by_role: Optional[Dict[str, int]] = None,
        simulated_wear_history: Optional[List[Dict[str, Any]]] = None,
    ) -> float:
        """Scores candidate and stores individual component scores."""
        counts = feasible_substitute_counts if feasible_substitute_counts is not None else (user_garment_counts_by_role or {})
        garments_by_role: Dict[str, sqlite3.Row] = {}
        for role, gid in candidate.roles.items():
            cur = self.conn.execute("SELECT * FROM Garment WHERE id = ?;", (gid,))
            garments_by_role[role] = cur.fetchone()

        c_score = self.compute_compatibility(garments_by_role)
        o_score = self.compute_occasion_fit(garments_by_role, session)
        p_score = self.compute_preference(garments_by_role, session)
        r_score = self.compute_repetition_penalty(
            garments_by_role=garments_by_role,
            session=session,
            recent_wear_history=recent_wear_history,
            feasible_substitute_counts=counts,
            simulated_wear_history=simulated_wear_history or [],
        )

        q_score = (
            self.weights.w_compatibility * c_score
            + self.weights.w_occasion * o_score
            + self.weights.w_preference * p_score
            - self.weights.w_repetition * r_score
        )
        q_score = max(0.0, min(1.0, q_score))

        candidate.compatibility_score = round(c_score, 4)
        candidate.occasion_score = round(o_score, 4)
        candidate.preference_score = round(p_score, 4)
        candidate.repetition_penalty = round(r_score, 4)
        candidate.quality_score = round(q_score, 4)

        return q_score

    def compute_compatibility(self, garments_by_role: Dict[str, sqlite3.Row]) -> float:
        """Pairwise harmony, formality coherence, and cut balance."""
        if len(garments_by_role) <= 1:
            return 1.0

        top = garments_by_role.get("top")
        bottom = garments_by_role.get("bottom")

        # One-piece filling both top and bottom is perfectly self-compatible
        if top and bottom and top["id"] == bottom["id"]:
            return 1.0

        score = 0.8  # baseline

        if top and bottom:
            c_top = (top["primary_color"] or "").lower()
            c_bot = (bottom["primary_color"] or "").lower()

            if c_top in NEUTRALS or c_bot in NEUTRALS:
                score += 0.1
            elif c_top == c_bot:
                score += 0.05
            else:
                score -= 0.05

            f_diff = abs(int(top["formality"]) - int(bottom["formality"]))
            score -= 0.15 * f_diff

        return max(0.1, min(1.0, score))

    def compute_occasion_fit(
        self,
        garments_by_role: Dict[str, sqlite3.Row],
        session: sqlite3.Row,
    ) -> float:
        """Evaluates whether garments match session formality and usage context."""
        session_type = session["session_type"]
        min_f, max_f = SESSION_FORMALITY.get(session_type, (1, 5))

        # Deduplicate physical garments (one-piece counted once)
        unique_garments = {g["id"]: g for g in garments_by_role.values()}

        fit_scores = []
        for g in unique_garments.values():
            g_f = int(g["formality"])
            if min_f <= g_f <= max_f:
                f_score = 1.0
            else:
                dist = min(abs(g_f - min_f), abs(g_f - max_f))
                f_score = max(0.1, 1.0 - 0.3 * dist)

            contexts = json.loads(g["usage_contexts"] or "[]")
            if not contexts:
                c_score = 1.0  # unconstrained/neutral
            elif session_type in contexts:
                c_score = 1.0  # explicitly suitable
            elif session_type in ("presentation", "formal") and any(ctx in ("sleepwear", "home_casual") for ctx in contexts):
                c_score = 0.2  # strong penalty for casual-only in formal session
            else:
                c_score = 0.5  # moderate mismatch

            fit_scores.append(0.6 * f_score + 0.4 * c_score)

        return sum(fit_scores) / len(fit_scores) if fit_scores else 0.5

    def compute_preference(
        self,
        garments_by_role: Dict[str, sqlite3.Row],
        session: sqlite3.Row,
    ) -> float:
        """Personal desirability based on like_rating and priority_level (deduplicated)."""
        session_type = session["session_type"]
        unique_garments = {g["id"]: g for g in garments_by_role.values()}

        scores = []
        for g in unique_garments.values():
            rating = int(g["like_rating"] or 5) / 10.0  # [0.1, 1.0]

            prio = PriorityLevel(g["priority_level"])
            overrides = json.loads(g["context_priority_overrides"] or "{}")
            if session_type in overrides:
                prio = PriorityLevel(overrides[session_type])

            if prio == PriorityLevel.FAVOURITE:
                p_bonus = 0.2
            elif prio == PriorityLevel.FALLBACK_ONLY:
                p_bonus = -0.3
            else:
                p_bonus = 0.0

            scores.append(max(0.1, min(1.0, rating + p_bonus)))

        return sum(scores) / len(scores) if scores else 0.5

    def compute_repetition_penalty(
        self,
        garments_by_role: Dict[str, sqlite3.Row],
        session: sqlite3.Row,
        recent_wear_history: List[sqlite3.Row],
        feasible_substitute_counts: Dict[str, int],
        simulated_wear_history: List[Dict[str, Any]],
    ) -> float:
        """
        Computes repetition penalty taking into account:
        - Sleepwear context has zero repetition penalty (Scenario 5).
        - Single feasible substitute has zero repetition penalty (Scenario 28).
        - Cross-context isolation (home_casual to party factor = 0.0) (Scenario 29).
        - Within-plan simulated wear recency.
        """
        session_type = session["session_type"]
        session_date = session["session_date"]

        if session_type == "sleepwear":
            return 0.0

        target_dt = date.fromisoformat(session_date)
        role_penalties = []

        # Deduplicate physical garments (one-piece filling multiple roles counted once)
        unique_garments = {}
        for role, g in garments_by_role.items():
            gid = g["id"]
            if gid not in unique_garments:
                unique_garments[gid] = (role, g)

        for gid, (role, g) in unique_garments.items():
            # Feasible-substitute constraint: if <= 1 feasible item exists for this role, zero penalty!
            substitute_cnt = feasible_substitute_counts.get(role, 99)
            if substitute_cnt <= 1:
                role_penalties.append(0.0)
                continue

            highest_penalty = 0.0

            # 1. Check within-plan simulated wears (Monday wear affects Wednesday assignment)
            for sim in simulated_wear_history:
                if gid in sim.get("garment_ids", []):
                    sim_dt = date.fromisoformat(sim["session_date"])
                    delta_days = (target_dt - sim_dt).days
                    if 0 <= delta_days <= 14:
                        if delta_days <= 1:
                            recency = 1.0
                        elif delta_days <= 3:
                            recency = 0.6
                        elif delta_days <= 7:
                            recency = 0.3
                        else:
                            recency = 0.1

                        prev_type = sim["session_type"]
                        if prev_type == session_type:
                            cf = 1.0
                        elif prev_type in ("home_casual", "sleepwear") and session_type in ("party", "presentation", "office"):
                            cf = 0.0
                        else:
                            cf = 0.5

                        pen = recency * cf
                        if pen > highest_penalty:
                            highest_penalty = pen

            # 2. Check historical confirmed wears
            for log in recent_wear_history:
                items_cur = self.conn.execute(
                    "SELECT 1 FROM OutfitWearLogItem WHERE outfit_wear_log_id = ? AND garment_id = ?;",
                    (log["id"], gid),
                )
                if not items_cur.fetchone():
                    continue

                wear_dt = date.fromisoformat(log["local_wear_date"])
                delta_days = (target_dt - wear_dt).days
                if delta_days < 0:
                    continue

                if delta_days <= 1:
                    base_recency = 1.0
                elif delta_days <= 3:
                    base_recency = 0.6
                elif delta_days <= 7:
                    base_recency = 0.3
                elif delta_days <= 14:
                    base_recency = 0.1
                else:
                    base_recency = 0.0

                prev_type = log["session_type"]
                if prev_type == session_type:
                    context_factor = 1.0
                elif prev_type == "home_casual" and session_type == "party":
                    context_factor = 0.0
                elif prev_type == "sleepwear":
                    context_factor = 0.0
                else:
                    context_factor = 0.5

                penalty = base_recency * context_factor
                if penalty > highest_penalty:
                    highest_penalty = penalty

            role_penalties.append(highest_penalty)

        return sum(role_penalties) / len(role_penalties) if role_penalties else 0.0
