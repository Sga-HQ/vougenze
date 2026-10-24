"""
Vougenze Candidate Generation Service (Frozen Spec 2.1, 2.13, Scenarios 6, 8, 20).
Generates role-valid outfit candidates, supports multi-role one-pieces,
applies context-specific priority overrides, enforces hard wear limits,
and generates deterministic candidate IDs with bounded Cartesian products.
"""
from __future__ import annotations
import hashlib
import itertools
import json
import sqlite3
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, Set

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
        cur = self.conn.execute("SELECT * FROM Garment WHERE user_id = ?;", (uid,))
        all_garments = cur.fetchall()

        if fallback_policy == FallbackPolicy.ALLOW:
            return self._build_candidates(
                all_garments=all_garments,
                required_roles=required_roles,
                session_type=session_type,
                session_id=session_id,
                allow_fallback=True,
                excluded_garment_ids=excluded_garment_ids,
            )

        # Step 1: Attempt generation without fallback-only garments
        candidates = self._build_candidates(
            all_garments=all_garments,
            required_roles=required_roles,
            session_type=session_type,
            session_id=session_id,
            allow_fallback=False,
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

    def _build_candidates(
        self,
        all_garments: List[sqlite3.Row],
        required_roles: List[str],
        session_type: str,
        session_id: str,
        allow_fallback: bool,
        excluded_garment_ids: Optional[Set[str]] = None,
    ) -> List[OutfitCandidate]:
        """Filters garments and combines them into role-complete outfits."""
        # 1. Filter eligible garments
        eligible: List[sqlite3.Row] = []
        for g in all_garments:
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

        # Truncate role pools deterministically to MAX_ROLE_ITEMS
        for r in required_roles:
            role_pools[r].sort(key=lambda g: (-int(g["like_rating"] or 5), g["id"]))
            role_pools[r] = role_pools[r][:MAX_ROLE_ITEMS]

        one_pieces.sort(key=lambda g: (-int(g["like_rating"] or 5), g["id"]))
        one_pieces = one_pieces[:MAX_ROLE_ITEMS]

        candidates: List[OutfitCandidate] = []

        # 3a. Generate standard 2-piece/multi-piece candidates
        standard_role_keys = [r for r in required_roles]
        if all(len(role_pools[r]) > 0 for r in standard_role_keys):
            pool_lists = [role_pools[r] for r in standard_role_keys]
            for combo in itertools.product(*pool_lists):
                if len(candidates) >= MAX_CANDIDATES:
                    break
                # Ensure each garment is used at most once in distinct roles
                garment_ids = [g["id"] for g in combo]
                if len(garment_ids) == len(set(garment_ids)):
                    role_map = {r: g["id"] for r, g in zip(standard_role_keys, combo)}
                    cand_id = self._generate_candidate_id(session_id, role_map)
                    fb_count = sum(1 for g in combo if self._is_garment_fallback(g, session_type))
                    candidates.append(
                        OutfitCandidate(
                            id=cand_id,
                            garment_ids=garment_ids,
                            roles=role_map,
                            is_fallback=(fb_count > 0),
                            fallback_count=fb_count,
                        )
                    )

        # 3b. Scenario 20: Generate one-piece candidates filling both top and bottom
        if "top" in required_roles and "bottom" in required_roles and one_pieces:
            remaining_roles = [r for r in required_roles if r not in ("top", "bottom")]
            if all(len(role_pools[r]) > 0 for r in remaining_roles):
                other_pools = [role_pools[r] for r in remaining_roles] if remaining_roles else [[]]
                for op in one_pieces:
                    if len(candidates) >= MAX_CANDIDATES:
                        break
                    if remaining_roles:
                        for others in itertools.product(*other_pools):
                            if len(candidates) >= MAX_CANDIDATES:
                                break
                            other_ids = [g["id"] for g in others]
                            # Invariant: others must be distinct from each other and from the one-piece
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
                            candidates.append(
                                OutfitCandidate(
                                    id=cand_id,
                                    garment_ids=[op["id"]] + other_ids,
                                    roles=role_map,
                                    is_fallback=(fb_count > 0),
                                    fallback_count=fb_count,
                                )
                            )
                    else:
                        role_map = {"top": op["id"], "bottom": op["id"]}
                        cand_id = self._generate_candidate_id(session_id, role_map)
                        fb_count = 1 if self._is_garment_fallback(op, session_type) else 0
                        candidates.append(
                            OutfitCandidate(
                                id=cand_id,
                                garment_ids=[op["id"]],
                                roles=role_map,
                                is_fallback=(fb_count > 0),
                                fallback_count=fb_count,
                            )
                        )

        # Sort candidates deterministically, prioritizing minimum fallback count first
        candidates.sort(key=lambda c: (c.fallback_count, c.id))
        return candidates

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
        session_cur = self.conn.execute(
            "SELECT * FROM WearSession WHERE id = ?;", (session_id,)
        )
        session = session_cur.fetchone()
        if not session:
            raise KeyError(f"Session '{session_id}' not found")

        uid = session["user_id"]
        if user_id is not None and user_id != uid:
            raise PermissionError("Requested user does not own this wear session")

        required_roles = json.loads(session["required_roles"] or '["top", "bottom", "footwear"]')
        excluded = excluded_garment_ids or set()

        garments = self.conn.execute(
            """
            SELECT * FROM Garment
            WHERE user_id = ?
              AND availability_state = 'AVAILABLE'
              AND condition_state IN ('CLEAN', 'WORN_REUSABLE');
            """,
            (uid,),
        ).fetchall()

        counts = {r: 0 for r in required_roles}
        for g in garments:
            if g["id"] in excluded:
                continue
            hl = g["hard_limit_wears"]
            if hl is not None and (g["wears_since_confirmed_wash"] or 0) >= hl:
                # Check for available authorization
                auth = self.conn.execute(
                    """
                    SELECT id FROM GarmentWearAuthorization
                    WHERE garment_id = ? AND status = 'AVAILABLE' AND consumed_by_wear_event_id IS NULL
                    LIMIT 1;
                    """,
                    (g["id"],),
                ).fetchone()
                if not auth:
                    continue

            cat = g["physical_category"]
            fills = json.loads(g["fills_roles"] or "[]")
            for r in required_roles:
                if r in fills or cat == r:
                    counts[r] += 1
                elif cat == "one_piece" and r in ("top", "bottom"):
                    counts[r] += 1

        return counts

    def _generate_candidate_id(self, session_id: str, role_map: Dict[str, str]) -> str:
        """Deterministic, canonical candidate signature ID."""
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

    def snapshot_candidate(
        self,
        candidate: OutfitCandidate,
        session_id: str,
        user_id: str,
        generator_version: str = "v0_deterministic",
        feature_schema_version: str = "v0_heuristic",
    ) -> str:
        """Persists candidate into OutfitCandidateSnapshot and OutfitCandidateSnapshotItem."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.conn.execute(
            """
            INSERT INTO OutfitCandidateSnapshot(
                id, user_id, session_id, generator_version, feature_schema_version,
                compatibility_score, occasion_score, preference_score, repetition_penalty,
                feature_snapshot, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                candidate.id,
                user_id,
                session_id,
                generator_version,
                feature_schema_version,
                candidate.compatibility_score,
                candidate.occasion_score,
                candidate.preference_score,
                candidate.repetition_penalty,
                json.dumps({
                    "roles": candidate.roles,
                    "is_fallback": candidate.is_fallback,
                    "fallback_count": candidate.fallback_count,
                }),
                now,
            ),
        )

        for role, gid in candidate.roles.items():
            g_cur = self.conn.execute("SELECT * FROM Garment WHERE id = ?;", (gid,))
            g = g_cur.fetchone()
            snap = {
                "name": g["name"],
                "physical_category": g["physical_category"],
                "subcategory": g["subcategory"],
                "primary_color": g["primary_color"],
                "formality": g["formality"],
            }
            self.conn.execute(
                """
                INSERT INTO OutfitCandidateSnapshotItem(
                    candidate_id, garment_id, outfit_role, garment_snapshot
                )
                VALUES (?, ?, ?, ?);
                """,
                (candidate.id, gid, role, json.dumps(snap)),
            )

        return candidate.id
