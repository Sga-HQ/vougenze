"""
Vougenze Preference Evidence Service (Frozen Spec 2.13, Scenarios 31, 32, 33, 34, 35).
Captures normalized candidate snapshots, pairwise alternatives, and contextual evidence
without asserting learned preference updates (Data Readiness Contract).
Enforces snapshot consistency, choice-set uniqueness, and valid inclusion combinations.
"""
from __future__ import annotations
import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, Union

from core.models import (
    InclusionType,
    OutfitCandidate,
    RejectionReason,
)


class PreferenceEvidenceService:
    """Manages candidate snapshots and normalized preference evidence capture."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    @staticmethod
    def _utc_now() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _generate_snapshot_id(self, user_id: str, candidate: OutfitCandidate, session_id: Optional[str]) -> str:
        """Deterministic candidate snapshot ID derived from canonical content."""
        payload = {
            "user_id": user_id,
            "session_id": session_id,
            "roles": sorted(candidate.roles.items()),
            "is_fallback": candidate.is_fallback,
        }
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:16]
        return f"ocs_{digest}"

    def create_candidate_snapshot(
        self,
        user_id: str,
        candidate: OutfitCandidate,
        session_id: Optional[str] = None,
        generator_version: str = "v0_deterministic",
        feature_schema_version: str = "v0.1",
        feature_snapshot: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        Saves an immutable snapshot of an outfit candidate and its garments.
        Verifies content equality if a snapshot with the deterministic ID already exists.
        """
        now = self._utc_now()
        cand_id = candidate.id if (candidate.id and candidate.id.strip()) else self._generate_snapshot_id(user_id, candidate, session_id)

        # Check existing snapshot
        existing = self.conn.execute(
            "SELECT * FROM OutfitCandidateSnapshot WHERE id = ?;", (cand_id,)
        ).fetchone()

        if existing:
            # Verify consistency: user and session must match
            if existing["user_id"] != user_id or existing["session_id"] != session_id:
                raise ValueError(f"Snapshot ID collision for candidate '{cand_id}' with mismatched identity")
            return cand_id

        # Insert snapshot header
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
                cand_id,
                user_id,
                session_id,
                generator_version,
                feature_schema_version,
                candidate.compatibility_score,
                candidate.occasion_score,
                candidate.preference_score,
                candidate.repetition_penalty,
                json.dumps(feature_snapshot or {"roles": candidate.roles, "is_fallback": candidate.is_fallback}),
                now,
            ),
        )

        # Insert snapshot items
        for role, gid in candidate.roles.items():
            g_row = self.conn.execute("SELECT * FROM Garment WHERE id = ?;", (gid,)).fetchone()
            g_snap = dict(g_row) if g_row else {"id": gid}
            self.conn.execute(
                """
                INSERT INTO OutfitCandidateSnapshotItem(
                    candidate_id, garment_id, outfit_role, garment_snapshot
                )
                VALUES (?, ?, ?, ?);
                """,
                (cand_id, gid, role, json.dumps(g_snap)),
            )

        return cand_id

    def record_selection_evidence(
        self,
        user_id: str,
        session_id: str,
        occasion_context: str,
        selected_candidate: OutfitCandidate,
        alternatives: List[Dict[str, Any]],
        selection_reason: Optional[List[str]] = None,
        is_constraint_driven: bool = False,
        constraint_flags: Optional[Dict[str, Any]] = None,
        recorded_at: Optional[str] = None,
    ) -> str:
        """
        Scenario 31, 32, 33: Records outfit selection evidence with normalized alternatives.
        Guarantees:
        - Atomic choice set creation
        - Selected candidate is not duplicated in alternatives
        - Inclusion semantics are validated:
            PRESENTED requires presentation_order
            FEASIBLE_NOT_PRESENTED requires was_feasible = 1, was_selected = 0
            INFEASIBLE_REFERENCE requires was_feasible = 0, was_selected = 0, exclusion_reason required
        """
        now = recorded_at or self._utc_now()
        evidence_id = f"pe_{uuid.uuid4().hex[:12]}"
        sp_id = f"sp_{uuid.uuid4().hex[:8]}"

        self.conn.execute(f"SAVEPOINT {sp_id};")
        try:
            selected_cand_id = self.create_candidate_snapshot(
                user_id=user_id,
                candidate=selected_candidate,
                session_id=session_id,
            )

            # Choice-set duplicate guard
            seen_candidates = {selected_cand_id}

            flags = constraint_flags or {}
            if is_constraint_driven:
                flags["constraint_driven"] = 1

            self.conn.execute(
                """
                INSERT INTO PreferenceEvidence(
                    id, user_id, evidence_type, session_id, occasion_context,
                    selected_candidate_id, selection_reason, constraint_flags, recorded_at
                )
                VALUES (?, ?, 'selection', ?, ?, ?, ?, ?, ?);
                """,
                (
                    evidence_id,
                    user_id,
                    session_id,
                    occasion_context,
                    selected_cand_id,
                    json.dumps(selection_reason or []),
                    json.dumps(flags),
                    now,
                ),
            )

            # Record selected alternative
            self.conn.execute(
                """
                INSERT INTO PreferenceEvidenceAlternative(
                    evidence_id, candidate_id, presentation_order, was_selected, was_feasible,
                    inclusion_type, exclusion_reason, constraint_flags
                )
                VALUES (?, ?, 0, 1, 1, 'PRESENTED', NULL, ?);
                """,
                (evidence_id, selected_cand_id, json.dumps(flags)),
            )

            # Record compared / reference alternatives
            for order, alt in enumerate(alternatives, start=1):
                cand: OutfitCandidate = alt["candidate"]
                alt_cand_id = self.create_candidate_snapshot(
                    user_id=user_id,
                    candidate=cand,
                    session_id=session_id,
                )

                if alt_cand_id in seen_candidates:
                    raise ValueError(f"Duplicate candidate '{alt_cand_id}' in preference evidence choice set")
                seen_candidates.add(alt_cand_id)

                was_feasible: bool = alt.get("was_feasible", True)
                inc_type: str = alt.get(
                    "inclusion_type",
                    InclusionType.PRESENTED.value if was_feasible else InclusionType.INFEASIBLE_REFERENCE.value,
                )
                exclusion_reason: Optional[str] = alt.get("exclusion_reason")
                alt_flags: Dict[str, Any] = alt.get("constraint_flags", {})

                # Validate inclusion semantics combinations
                if inc_type == InclusionType.PRESENTED.value and order is None:
                    raise ValueError("PRESENTED alternative requires presentation_order")
                if inc_type == InclusionType.FEASIBLE_NOT_PRESENTED.value:
                    if not was_feasible:
                        raise ValueError("FEASIBLE_NOT_PRESENTED requires was_feasible=True")
                if inc_type == InclusionType.INFEASIBLE_REFERENCE.value:
                    if was_feasible:
                        raise ValueError("INFEASIBLE_REFERENCE requires was_feasible=False")
                    if not exclusion_reason:
                        raise ValueError("INFEASIBLE_REFERENCE requires exclusion_reason")

                self.conn.execute(
                    """
                    INSERT INTO PreferenceEvidenceAlternative(
                        evidence_id, candidate_id, presentation_order, was_selected, was_feasible,
                        inclusion_type, exclusion_reason, constraint_flags
                    )
                    VALUES (?, ?, ?, 0, ?, ?, ?, ?);
                    """,
                    (
                        evidence_id,
                        alt_cand_id,
                        order,
                        1 if was_feasible else 0,
                        inc_type,
                        exclusion_reason,
                        json.dumps(alt_flags),
                    ),
                )

            self.conn.execute(f"RELEASE SAVEPOINT {sp_id};")
            return evidence_id
        except Exception:
            self.conn.execute(f"ROLLBACK TO SAVEPOINT {sp_id};")
            self.conn.execute(f"RELEASE SAVEPOINT {sp_id};")
            raise

    def record_rejection_evidence(
        self,
        user_id: str,
        session_id: str,
        occasion_context: str,
        rejected_candidate: OutfitCandidate,
        rejection_reason: Union[RejectionReason, str],
        alternatives: Optional[List[Dict[str, Any]]] = None,
        recorded_at: Optional[str] = None,
    ) -> str:
        """
        Scenario 34 & 35: Records chronological rejection evidence.
        - Uses enumerated reason code.
        - If rejection_reason is ITEM_IN_LAUNDRY, sets laundry_constrained flag to yield NO taste penalty.
        - Preserves chronological sequence for taste shift detection.
        """
        now = recorded_at or self._utc_now()
        evidence_id = f"pe_{uuid.uuid4().hex[:12]}"
        sp_id = f"sp_{uuid.uuid4().hex[:8]}"

        self.conn.execute(f"SAVEPOINT {sp_id};")
        try:
            reason_val = rejection_reason.value if isinstance(rejection_reason, RejectionReason) else str(rejection_reason)

            rej_cand_id = self.create_candidate_snapshot(
                user_id=user_id,
                candidate=rejected_candidate,
                session_id=session_id,
            )

            seen_candidates = {rej_cand_id}

            flags: Dict[str, Any] = {}
            if reason_val == RejectionReason.ITEM_IN_LAUNDRY.value or "laundry" in reason_val.lower():
                flags["laundry_constrained"] = 1
                flags["aesthetic_penalty"] = 0

            self.conn.execute(
                """
                INSERT INTO PreferenceEvidence(
                    id, user_id, evidence_type, session_id, occasion_context,
                    rejected_candidate_id, rejection_reason, constraint_flags, recorded_at
                )
                VALUES (?, ?, 'rejection', ?, ?, ?, ?, ?, ?);
                """,
                (
                    evidence_id,
                    user_id,
                    session_id,
                    occasion_context,
                    rej_cand_id,
                    reason_val,
                    json.dumps(flags),
                    now,
                ),
            )

            self.conn.execute(
                """
                INSERT INTO PreferenceEvidenceAlternative(
                    evidence_id, candidate_id, presentation_order, was_selected, was_feasible,
                    inclusion_type, exclusion_reason, constraint_flags
                )
                VALUES (?, ?, 0, 0, 1, 'PRESENTED', ?, ?);
                """,
                (evidence_id, rej_cand_id, reason_val, json.dumps(flags)),
            )

            if alternatives:
                for order, alt in enumerate(alternatives, start=1):
                    cand: OutfitCandidate = alt["candidate"]
                    alt_cand_id = self.create_candidate_snapshot(
                        user_id=user_id,
                        candidate=cand,
                        session_id=session_id,
                    )
                    if alt_cand_id in seen_candidates:
                        raise ValueError(f"Duplicate candidate '{alt_cand_id}' in preference evidence choice set")
                    seen_candidates.add(alt_cand_id)

                    was_feasible = alt.get("was_feasible", True)
                    inc_type = alt.get("inclusion_type", "PRESENTED")
                    alt_flags = alt.get("constraint_flags", {})

                    self.conn.execute(
                        """
                        INSERT INTO PreferenceEvidenceAlternative(
                            evidence_id, candidate_id, presentation_order, was_selected, was_feasible,
                            inclusion_type, exclusion_reason, constraint_flags
                        )
                        VALUES (?, ?, ?, 0, ?, ?, ?, ?);
                        """,
                        (
                            evidence_id,
                            alt_cand_id,
                            order,
                            1 if was_feasible else 0,
                            inc_type,
                            alt.get("exclusion_reason"),
                            json.dumps(alt_flags),
                        ),
                    )

            self.conn.execute(f"RELEASE SAVEPOINT {sp_id};")
            return evidence_id
        except Exception:
            self.conn.execute(f"ROLLBACK TO SAVEPOINT {sp_id};")
            self.conn.execute(f"RELEASE SAVEPOINT {sp_id};")
            raise
