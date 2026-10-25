"""
Vougenze Candidate Generation Service (Frozen Spec 2.1, 2.13, Scenarios 6, 8, 20).
Generates role-valid outfit candidates, supports multi-role one-pieces,
applies context-specific priority overrides, enforces hard wear limits,
preserves future-critical and signature-distinct items during pool truncation,
and generates deterministic candidate IDs with bounded Cartesian products.
"""
from __future__ import annotations
import hashlib
import itertools
import json
import sqlite3
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, Set, Tuple

from core.models import (
    OutfitCandidate,
    PriorityLevel,
    ConditionState,
    AvailabilityState,
    FallbackPolicy,
)

MAX_ROLE_ITEMS = 12
MAX_CANDIDATES = 200


class CandidateGenerator:
    """Generates valid candidate outfits for a wear session."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def get_session(self, session_id: str) -> sqlite3.Row:
        cur = self.conn.execute("SELECT * FROM WearSession WHERE id = ?;", (session_id,))
        row = cur.fetchone()
        if not row:
            raise KeyError(f"WearSession {session_id} not found")
        return row

    def generate_candidates(
        self,
        session_id: str,
        user_id: Optional[str] = None,
        fallback_policy: FallbackPolicy = FallbackPolicy.IF_REQUIRED,
        excluded_garment_ids: Optional[Set[str]] = None,
    ) -> List[OutfitCandidate]:
        """
        Generates role-valid outfit candidates for the given session.
        Enforces user ownership, physical availability, hygiene condition,
        and hard wear limits with authorization overrides.
        """
        session = self.get_session(session_id)
        uid = session["user_id"]
        if user_id is not None and user_id != uid:
            raise PermissionError("Requested user does not own this wear session")

        required_roles = json.loads(session["required_roles"])
        session_type = session["session_type"]

        # Fetch all garments belonging to authoritative session owner
        cur = self.conn.execute(
            "SELECT * FROM Garment WHERE user_id = ? ORDER BY id ASC;", (uid,)
        )
        all_garments = cur.fetchall()

        # Step 1: Attempt generation without fallback-only garments if policy is EXCLUDE or IF_REQUIRED
        allow_initial_fallback = (fallback_policy == FallbackPolicy.ALLOW)
        candidates = self._build_candidates(
            all_garments=all_garments,
            required_roles=required_roles,
            session_type=session_type,
            session_id=session_id,
            allow_fallback=allow_initial_fallback,
            excluded_garment_ids=excluded_garment_ids,
        )

        # Step 2: If no normal candidates found and fallback is IF_REQUIRED, retry with fallback (Scenario 6)
        if not candidates and fallback_policy == FallbackPolicy.IF_REQUIRED:
            candidates = self._build_candidates(
                all_garments=all_garments,
                required_roles=required_roles,
                session_type=session_type,
                session_id=session_id,
                allow_fallback=True,
                excluded_garment_ids=excluded_garment_ids,
            )

        # Sort candidates prioritizing minimum fallback garments
        candidates.sort(key=lambda c: (c.fallback_count, c.id))
        return candidates[:MAX_CANDIDATES]

    # Explicit alias representing two-stage filtering: stage 1 produces structurally possible candidates,
    # stage 2 (ContinuationPlanner beam search) enforces path-specific operational availability.
    structurally_possible_candidates = generate_candidates

    def _build_candidates(
        self,
        all_garments: List[sqlite3.Row],
        required_roles: List[str],
        session_type: str,
        session_id: str,
        allow_fallback: bool,
        excluded_garment_ids: Optional[Set[str]] = None,
    ) -> List[OutfitCandidate]:
        """Filters garments and combines them into role-complete outfits with protected quotas."""
        # Check if an OccasionProfile exists for this user and session_type
        user_id = all_garments[0]["user_id"] if all_garments else None
        occ_row = None
        if user_id:
            occ_row = self.conn.execute(
                "SELECT * FROM OccasionProfile WHERE user_id = ? AND name = ?;", (user_id, session_type)
            ).fetchone()

        min_formality = occ_row["formality_min"] if occ_row and occ_row["formality_min"] is not None else 1
        max_formality = occ_row["formality_max"] if occ_row and occ_row["formality_max"] is not None else 5
        prohibited = set(json.loads(occ_row["prohibited_items"] or "[]")) if occ_row else set()

        # 1. Filter eligible garments
        eligible: List[sqlite3.Row] = []
        for g in all_garments:
            # Occasion Profile admissibility check
            if occ_row:
                g_f = g["formality"] if g["formality"] is not None else 2
                if not (min_formality <= g_f <= max_formality):
                    continue
                if g["subcategory"] in prohibited or g["physical_category"] in prohibited or g["id"] in prohibited:
                    continue

            # Physical availability check
            if g["availability_state"] != AvailabilityState.AVAILABLE.value:
                continue

            # Condition eligibility check (CLEAN or WORN_REUSABLE)
            # Wear authorization NEVER overrides condition state!
            if g["condition_state"] not in (
                ConditionState.CLEAN.value,
                ConditionState.WORN_REUSABLE.value,
            ):
                continue

            # Reservation / exclusion check
            if excluded_garment_ids and g["id"] in excluded_garment_ids:
                continue

            # Hard wear limit check
            effective_limit = g["hard_limit_wears"]
            if (
                effective_limit is not None
                and g["wears_since_confirmed_wash"] >= effective_limit
            ):
                authorization = self.conn.execute(
                    """
                    SELECT id
                    FROM GarmentWearAuthorization
                    WHERE garment_id = ?
                      AND status = 'AVAILABLE'
                      AND consumed_by_wear_event_id IS NULL
                    ORDER BY authorized_at
                    LIMIT 1;
                    """,
                    (g["id"],),
                ).fetchone()

                if authorization is None:
                    continue

            # Scenario 8: Context Priority Overrides
            prio = PriorityLevel(g["priority_level"])
            overrides = json.loads(g["context_priority_overrides"] or "{}")
            if session_type in overrides:
                prio = PriorityLevel(overrides[session_type])

            if prio == PriorityLevel.DO_NOT_RECOMMEND:
                continue

            if prio == PriorityLevel.FALLBACK_ONLY and not allow_fallback:
                continue

            eligible.append(g)

        # 2. Partition eligible garments by the roles they can satisfy
        role_pools: Dict[str, List[sqlite3.Row]] = {role: [] for role in required_roles}
        one_pieces: List[sqlite3.Row] = []

        for g in eligible:
            cat = g["physical_category"]
            fills = json.loads(g["fills_roles"] or "[]")
            if not fills:
                fills = [cat]

            # Scenario 20: Check if one-piece fills both top and bottom
            if cat == "one_piece" or ("top" in fills and "bottom" in fills):
                if g not in one_pieces:
                    one_pieces.append(g)

            for r in required_roles:
                if r in fills or cat == r:
                    if g not in role_pools[r]:
                        role_pools[r].append(g)

        # Truncate role pools using bounded critical retention
        for r in required_roles:
            role_pools[r] = self._truncate_role_pool(role_pools[r], session_type, MAX_ROLE_ITEMS)

        one_pieces = self._truncate_role_pool(one_pieces, session_type, MAX_ROLE_ITEMS)

        one_piece_cands: List[OutfitCandidate] = []
        standard_cands: List[OutfitCandidate] = []

        # 3a. Scenario 20: Generate one-piece candidates with dedicated quota
        if "top" in required_roles and "bottom" in required_roles and one_pieces:
            remaining_roles = [r for r in required_roles if r not in ("top", "bottom")]
            if not remaining_roles:
                for op in one_pieces:
                    role_map = {"top": op["id"], "bottom": op["id"]}
                    cand_id = self._generate_candidate_id(session_id, role_map)
                    fb_count = 1 if self._is_garment_fallback(op, session_type) else 0
                    one_piece_cands.append(
                        OutfitCandidate(
                            id=cand_id,
                            garment_ids=[op["id"]],
                            roles=role_map,
                            is_fallback=(fb_count > 0),
                            fallback_count=fb_count,
                        )
                    )
            elif all(len(role_pools[r]) > 0 for r in remaining_roles):
                other_pools = [role_pools[r] for r in remaining_roles]
                for op in one_pieces:
                    if len(one_piece_cands) >= 50:
                        break
                    for others in itertools.product(*other_pools):
                        if len(one_piece_cands) >= 50:
                            break
                        other_ids = [g["id"] for g in others]
                        if len(other_ids) != len(set(other_ids)):
                            continue
                        if op["id"] in other_ids:
                            continue

                        role_map = {"top": op["id"], "bottom": op["id"]}
                        for r, g in zip(remaining_roles, others):
                            role_map[r] = g["id"]
                        cand_id = self._generate_candidate_id(session_id, role_map)

                        fb_count = (1 if self._is_garment_fallback(op, session_type) else 0) + sum(
                            1 for g in others if self._is_garment_fallback(g, session_type)
                        )
                        one_piece_cands.append(
                            OutfitCandidate(
                                id=cand_id,
                                garment_ids=[op["id"]] + other_ids,
                                roles=role_map,
                                is_fallback=(fb_count > 0),
                                fallback_count=fb_count,
                            )
                        )

        # 3b. Generate standard 2-piece/multi-piece candidates with dedicated quota
        standard_role_keys = [r for r in required_roles]
        if all(len(role_pools[r]) > 0 for r in standard_role_keys):
            pool_lists = [role_pools[r] for r in standard_role_keys]
            for combo in itertools.product(*pool_lists):
                if len(standard_cands) >= (MAX_CANDIDATES - len(one_piece_cands)):
                    break
                garment_ids = [g["id"] for g in combo]
                if len(garment_ids) == len(set(garment_ids)):
                    role_map = {r: g["id"] for r, g in zip(standard_role_keys, combo)}
                    cand_id = self._generate_candidate_id(session_id, role_map)
                    fb_count = sum(1 for g in combo if self._is_garment_fallback(g, session_type))
                    standard_cands.append(
                        OutfitCandidate(
                            id=cand_id,
                            garment_ids=garment_ids,
                            roles=role_map,
                            is_fallback=(fb_count > 0),
                            fallback_count=fb_count,
                        )
                    )

        combined = one_piece_cands + standard_cands
        combined.sort(key=lambda c: (c.fallback_count, c.id))
        return combined

    def get_feasible_substitute_counts(
        self,
        session_id: str,
        user_id: Optional[str] = None,
        excluded_garment_ids: Optional[Set[str]] = None,
    ) -> Dict[str, int]:
        """
        Counts the number of feasible substitute garments for each required role
        in the session, strictly filtering out dirty, airing, unknown, in-laundry,
        retired, or reserved/excluded garments.
        """
        session = self.get_session(session_id)
        uid = session["user_id"]
        if user_id is not None and user_id != uid:
            raise PermissionError("Requested user does not own this wear session")

        required_roles = json.loads(session["required_roles"] or '["top", "bottom", "footwear"]')
        candidates = self.generate_candidates(
            session_id=session_id,
            user_id=uid,
            excluded_garment_ids=excluded_garment_ids,
        )

        substitute_gids: Dict[str, Set[str]] = {r: set() for r in required_roles}
        for cand in candidates:
            for r, gid in cand.roles.items():
                if r in substitute_gids:
                    substitute_gids[r].add(gid)

        return {r: len(gids) for r, gids in substitute_gids.items()}

    @staticmethod
    def compute_candidate_substitutes(
        candidates: List[OutfitCandidate],
        selected: OutfitCandidate,
    ) -> Dict[str, int]:
        """
        Scenario 28: Computes complete valid outfit replacements for each component
        of the selected candidate among the candidates for this session.
        """
        substitutes = {}
        for role, selected_gid in selected.roles.items():
            if role in ("top", "bottom") and selected.roles.get("top") == selected.roles.get("bottom"):
                replacements = {
                    c.roles["top"]
                    for c in candidates
                    if c.roles.get("top") == c.roles.get("bottom") and c.roles.get("top") != selected_gid
                }
            else:
                replacements = {
                    c.roles[role]
                    for c in candidates
                    if role in c.roles and c.roles[role] != selected_gid
                }
            substitutes[role] = len(replacements) + 1
        return substitutes

    def _generate_candidate_id(self, session_id: str, role_map: Dict[str, str]) -> str:
        """Derives a deterministic candidate ID from session_id and canonical role mapping."""
        signature_payload = {
            "session_id": session_id,
            "roles": sorted(role_map.items()),
            "generator_version": "v0_deterministic",
        }
        digest = hashlib.sha256(
            json.dumps(signature_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:16]
        return f"cand_{digest}"

    def _is_garment_fallback(self, g: sqlite3.Row, session_type: str) -> bool:
        prio = PriorityLevel(g["priority_level"])
        overrides = json.loads(g["context_priority_overrides"] or "{}")
        if session_type in overrides:
            prio = PriorityLevel(overrides[session_type])
        return prio == PriorityLevel.FALLBACK_ONLY

    def snapshot_candidate(self, candidate: OutfitCandidate, session_id: str, user_id: str) -> str:
        """Helper to create and persist an OutfitCandidateSnapshot for this candidate."""
        from core.evidence_service import PreferenceEvidenceService
        return PreferenceEvidenceService(self.conn).create_candidate_snapshot(
            user_id=user_id,
            candidate=candidate,
            session_id=session_id,
        )

    @classmethod
    def _truncate_role_pool(
        cls,
        pool: List[sqlite3.Row],
        session_type: Optional[str] = None,
        max_items: int = MAX_ROLE_ITEMS,
    ) -> List[sqlite3.Row]:
        """
        Scenario 20 & Defect 5: Bounded critical retention policy.
        1. Retains all critical items (formality >= 4, formal/presentation/office contexts, favourite priority).
        2. Retains at least one item per distinct eligibility signature.
        3. Fills remaining capacity by like rating and current desirability.
        """
        if len(pool) <= max_items:
            return pool

        retained: List[sqlite3.Row] = []
        retained_ids: Set[str] = set()

        def is_critical(g: sqlite3.Row) -> bool:
            formality = g["formality"] or 2
            contexts = json.loads(g["usage_contexts"] or "[]")
            prio = g["priority_level"]
            if formality >= 4 or any(c in contexts for c in ("presentation", "formal", "office")):
                return True
            if prio == PriorityLevel.FAVOURITE.value:
                return True
            return False

        # 1. Retain critical items
        for g in pool:
            if is_critical(g):
                if g["id"] not in retained_ids and len(retained) < max_items:
                    retained.append(g)
                    retained_ids.add(g["id"])

        # 2. Retain distinct signatures
        seen_signatures: Set[Tuple[Any, ...]] = set()
        for g in pool:
            sig = (
                g["physical_category"],
                g["cut_and_fit"],
                g["formality"],
                tuple(sorted(json.loads(g["usage_contexts"] or "[]"))),
                g["hard_limit_wears"],
            )
            if sig not in seen_signatures:
                seen_signatures.add(sig)
                if g["id"] not in retained_ids and len(retained) < max_items:
                    retained.append(g)
                    retained_ids.add(g["id"])

        # 3. Fill remaining pool capacity by like rating and id
        remaining = [g for g in pool if g["id"] not in retained_ids]
        remaining.sort(key=lambda g: (-int(g["like_rating"] or 5), g["id"]))

        for g in remaining:
            if len(retained) >= max_items:
                break
            retained.append(g)
            retained_ids.add(g["id"])

        return retained
