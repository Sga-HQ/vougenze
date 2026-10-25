"""
Vougenze Intake Staging & Duplicate Detection Service (Frozen Spec 2.3, Scenarios 37-40).
Stages incoming photos, computes byte-level SHA-256 fingerprints,
evaluates visual duplicate candidates across all matches, and enforces idempotent resolutions
for all four outcomes: SAME_GARMENT, DIFFERENT_GARMENT, LINKED_SIMILAR, and DISMISSED.
Requires active transaction boundaries and performs durable file cataloguing.
"""
from __future__ import annotations
import hashlib
import json
import shutil
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple, Union

from core.models import (
    DuplicateCandidateStatus,
)
from core.db import register_rollback_callback


def sha256_file(path: Union[str, Path]) -> str:
    """Computes SHA-256 hash by reading file contents in 1MB chunks."""
    hasher = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


class IntakeService:
    """Manages intake drafts, image fingerprints, and duplicate resolution."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    @staticmethod
    def _utc_now() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _require_transaction(self) -> None:
        if not self.conn.in_transaction:
            raise RuntimeError("This operation requires an active transaction")

    def stage_intake_draft(
        self,
        user_id: str,
        temporary_image_path: str,
        sha256_hash: Optional[str] = None,
        perceptual_hash: Optional[str] = None,
        detector_version: str = "v0_exact_hash",
    ) -> Tuple[str, Optional[str]]:
        """
        Scenario 37: Stages an image in GarmentIntakeDraft and checks for duplicate fingerprints
        BEFORE any new Garment record is created.
        Returns (draft_id, candidate_id or None).
        """
        self._require_transaction()
        now = self._utc_now()
        draft_id = f"gid_{uuid.uuid4().hex[:12]}"

        # Exact byte hashing: never silently downgrade to path text
        if sha256_hash:
            digest = sha256_hash
        elif Path(temporary_image_path).is_file():
            digest = sha256_file(temporary_image_path)
        else:
            raise FileNotFoundError(f"Intake image file not found: {temporary_image_path}")

        # Insert draft
        self.conn.execute(
            """
            INSERT INTO GarmentIntakeDraft(id, user_id, temporary_image_path, status, created_at)
            VALUES (?, ?, ?, 'PENDING', ?);
            """,
            (draft_id, user_id, temporary_image_path, now),
        )

        # Insert draft fingerprint
        fp_id = f"gif_{uuid.uuid4().hex[:12]}"
        self.conn.execute(
            """
            INSERT INTO GarmentIntakeFingerprint(id, intake_draft_id, sha256, perceptual_hash, created_at)
            VALUES (?, ?, ?, ?, ?);
            """,
            (fp_id, draft_id, digest, perceptual_hash, now),
        )

        # Multi-match detection: check existing catalogued images of this user
        cur = self.conn.execute(
            """
            SELECT DISTINCT gi.garment_id
            FROM GarmentImageFingerprint gif
            JOIN GarmentImage gi ON gif.garment_image_id = gi.id
            JOIN Garment g ON gi.garment_id = g.id
            WHERE g.user_id = ? AND gif.sha256 = ?;
            """,
            (user_id, digest),
        )
        matches = cur.fetchall()

        first_candidate_id = None
        for match in matches:
            candidate_id = f"gdc_{uuid.uuid4().hex[:12]}"
            if not first_candidate_id:
                first_candidate_id = candidate_id
            self.conn.execute(
                """
                INSERT INTO GarmentDuplicateCandidate(
                    id, user_id, intake_draft_id, existing_garment_id, exact_hash_match,
                    detector_version, status
                )
                VALUES (?, ?, ?, ?, 1, ?, 'PENDING');
                """,
                (candidate_id, user_id, draft_id, match["garment_id"], detector_version),
            )

        return draft_id, first_candidate_id

    def resolve_as_same_garment(
        self,
        candidate_id: str,
        image_type: str = "detail",
        is_primary: int = 0,
        resolution_note: Optional[str] = None,
    ) -> str:
        """
        Scenario 38: Attaches the draft image to the existing garment and updates draft status.
        Guarded against repeated resolutions; dismisses sibling candidates atomically.
        """
        self._require_transaction()
        now = self._utc_now()
        cur = self.conn.execute(
            """
            SELECT c.*, d.temporary_image_path, f.sha256, f.perceptual_hash
            FROM GarmentDuplicateCandidate c
            JOIN GarmentIntakeDraft d ON c.intake_draft_id = d.id
            JOIN GarmentIntakeFingerprint f ON f.intake_draft_id = d.id
            WHERE c.id = ? AND c.status = 'PENDING';
            """,
            (candidate_id,),
        )
        row = cur.fetchone()
        if not row:
            raise KeyError(f"Candidate {candidate_id} not found or already resolved")

        # 1. Prepare unique catalogued path and copy file if present on disk
        image_id = f"gi_{uuid.uuid4().hex[:12]}"
        raw_name = Path(row["temporary_image_path"]).name
        catalogued_path = f"catalogued/{row['existing_garment_id']}/{image_id}_{raw_name}"

        src_path = Path(row["temporary_image_path"])
        copied_dst: Optional[Path] = None
        if src_path.is_file():
            dst_path = Path(catalogued_path)
            dst_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_path, dst_path)
            copied_dst = dst_path

            def _cleanup_same():
                if dst_path.is_file():
                    try:
                        dst_path.unlink()
                    except Exception:
                        pass
            register_rollback_callback(self.conn, _cleanup_same)

        try:
            # 2. Insert GarmentImage on existing garment
            self.conn.execute(
                """
                INSERT INTO GarmentImage(id, garment_id, image_path, image_type, is_primary, created_at)
                VALUES (?, ?, ?, ?, ?, ?);
                """,
                (image_id, row["existing_garment_id"], catalogued_path, image_type, is_primary, now),
            )

            # 3. Create GarmentImageFingerprint
            gif_id = f"gif_{uuid.uuid4().hex[:12]}"
            self.conn.execute(
                """
                INSERT INTO GarmentImageFingerprint(id, garment_image_id, sha256, perceptual_hash, created_at)
                VALUES (?, ?, ?, ?, ?);
                """,
                (gif_id, image_id, row["sha256"], row["perceptual_hash"], now),
            )

            # 4. Finalize candidate status
            res = self.conn.execute(
                """
                UPDATE GarmentDuplicateCandidate
                SET status = 'SAME_GARMENT',
                    resolution_note = ?,
                    resolved_at = ?
                WHERE id = ? AND status = 'PENDING';
                """,
                (resolution_note, now, candidate_id),
            )
            if res.rowcount != 1:
                raise ValueError(f"Candidate {candidate_id} is not in PENDING status")

            # 5. Mark draft attached
            self.conn.execute(
                "UPDATE GarmentIntakeDraft SET status = 'ATTACHED_TO_EXISTING' WHERE id = ?;",
                (row["intake_draft_id"],),
            )

            # 6. Resolve sibling candidates
            self.conn.execute(
                """
                UPDATE GarmentDuplicateCandidate
                SET status = 'DISMISSED',
                    resolution_note = 'Resolved through sibling candidate',
                    resolved_at = ?
                WHERE intake_draft_id = ?
                  AND id != ?
                  AND status = 'PENDING';
                """,
                (now, row["intake_draft_id"], candidate_id),
            )

            return image_id
        except Exception:
            if copied_dst and copied_dst.is_file():
                try:
                    copied_dst.unlink()
                except Exception:
                    pass
            raise

    def resolve_as_different_garment(
        self,
        candidate_id: str,
        garment_data: Dict[str, Any],
        resolution_note: Optional[str] = None,
    ) -> str:
        """
        Scenario 39: Confirms distinction, creates new Garment, attaches image, and marks status.
        Guarded against repeated resolutions; dismisses sibling candidates atomically.
        """
        self._require_transaction()
        now = self._utc_now()
        cur = self.conn.execute(
            """
            SELECT c.*, d.temporary_image_path, f.sha256, f.perceptual_hash
            FROM GarmentDuplicateCandidate c
            JOIN GarmentIntakeDraft d ON c.intake_draft_id = d.id
            JOIN GarmentIntakeFingerprint f ON f.intake_draft_id = d.id
            WHERE c.id = ? AND c.status = 'PENDING';
            """,
            (candidate_id,),
        )
        row = cur.fetchone()
        if not row:
            raise KeyError(f"Candidate {candidate_id} not found or already resolved")

        garment_id = garment_data.get("id") or f"g_{uuid.uuid4().hex[:12]}"

        # 1. Insert new garment
        self.conn.execute(
            """
            INSERT INTO Garment(
                id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color,
                secondary_colors, pattern_type, formality, style_tags, layering_role,
                usage_contexts, cultural_context, weather_suitability, condition_state, availability_state,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'CLEAN', 'AVAILABLE', ?, ?);
            """,
            (
                garment_id,
                row["user_id"],
                garment_data["name"],
                garment_data["physical_category"],
                garment_data["subcategory"],
                garment_data.get("cut_and_fit", "regular"),
                garment_data["primary_color"],
                json.dumps(garment_data.get("secondary_colors", [])),
                garment_data.get("pattern_type", "solid"),
                garment_data.get("formality", 2),
                json.dumps(garment_data.get("style_tags", [])),
                garment_data.get("layering_role"),
                json.dumps(garment_data.get("usage_contexts", [])),
                garment_data.get("cultural_context"),
                json.dumps(garment_data.get("weather_suitability", [])),
                now,
                now,
            ),
        )

        # 2. Prepare permanent image storage
        image_id = f"gi_{uuid.uuid4().hex[:12]}"
        raw_name = Path(row["temporary_image_path"]).name
        catalogued_path = f"catalogued/{garment_id}/{image_id}_{raw_name}"

        src_path = Path(row["temporary_image_path"])
        copied_dst: Optional[Path] = None
        if src_path.is_file():
            dst_path = Path(catalogued_path)
            dst_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_path, dst_path)
            copied_dst = dst_path

            def _cleanup_diff():
                if dst_path.is_file():
                    try:
                        dst_path.unlink()
                    except Exception:
                        pass
            register_rollback_callback(self.conn, _cleanup_diff)

        try:
            # 3. Attach image
            self.conn.execute(
                """
                INSERT INTO GarmentImage(id, garment_id, image_path, image_type, is_primary, created_at)
                VALUES (?, ?, ?, 'full', 1, ?);
                """,
                (image_id, garment_id, catalogued_path, now),
            )

            # 4. Fingerprint
            gif_id = f"gif_{uuid.uuid4().hex[:12]}"
            self.conn.execute(
                """
                INSERT INTO GarmentImageFingerprint(id, garment_image_id, sha256, perceptual_hash, created_at)
                VALUES (?, ?, ?, ?, ?);
                """,
                (gif_id, image_id, row["sha256"], row["perceptual_hash"], now),
            )

            # 5. Finalize candidate status
            res = self.conn.execute(
                """
                UPDATE GarmentDuplicateCandidate
                SET status = 'DIFFERENT_GARMENT',
                    resolution_note = ?,
                    resolved_at = ?
                WHERE id = ? AND status = 'PENDING';
                """,
                (resolution_note, now, candidate_id),
            )
            if res.rowcount != 1:
                raise ValueError(f"Candidate {candidate_id} is not in PENDING status")

            # 6. Update draft status
            self.conn.execute(
                "UPDATE GarmentIntakeDraft SET status = 'CREATED_NEW_GARMENT' WHERE id = ?;",
                (row["intake_draft_id"],),
            )

            # 7. Dismiss sibling candidates
            self.conn.execute(
                """
                UPDATE GarmentDuplicateCandidate
                SET status = 'DISMISSED',
                    resolution_note = 'Resolved through sibling candidate',
                    resolved_at = ?
                WHERE intake_draft_id = ?
                  AND id != ?
                  AND status = 'PENDING';
                """,
                (now, row["intake_draft_id"], candidate_id),
            )

            return garment_id
        except Exception:
            if copied_dst and copied_dst.is_file():
                try:
                    copied_dst.unlink()
                except Exception:
                    pass
            raise

    def resolve_as_linked_similar(
        self,
        candidate_id: str,
        garment_data: Optional[Dict[str, Any]] = None,
        resolution_note: Optional[str] = None,
    ) -> str:
        """
        Scenario 39: Keep both and link as similar.
        Creates new Garment, attaches image, establishes durable similarity link in GarmentSimilarityLink,
        and marks draft/candidate resolved.
        Cleans up copied file from disk if database transaction fails.
        """
        self._require_transaction()
        now = self._utc_now()
        cur = self.conn.execute(
            """
            SELECT c.*, d.temporary_image_path, f.sha256, f.perceptual_hash
            FROM GarmentDuplicateCandidate c
            JOIN GarmentIntakeDraft d ON c.intake_draft_id = d.id
            JOIN GarmentIntakeFingerprint f ON f.intake_draft_id = d.id
            WHERE c.id = ? AND c.status = 'PENDING';
            """,
            (candidate_id,),
        )
        row = cur.fetchone()
        if not row:
            raise KeyError(f"Candidate {candidate_id} not found or already resolved")

        existing_id = row["existing_garment_id"]

        # Fetch existing garment metadata if garment_data not fully provided
        orig_row = self.conn.execute("SELECT * FROM Garment WHERE id = ?;", (existing_id,)).fetchone()
        gdata = garment_data or {}

        new_garment_id = gdata.get("id") or f"g_{uuid.uuid4().hex[:12]}"
        name = gdata.get("name") or f"{orig_row['name']} (Similar)"
        p_cat = gdata.get("physical_category") or orig_row["physical_category"]
        subcat = gdata.get("subcategory") or orig_row["subcategory"]
        cut = gdata.get("cut_and_fit") or orig_row["cut_and_fit"]
        color = gdata.get("primary_color") or orig_row["primary_color"]
        formality = gdata.get("formality") or orig_row["formality"]

        # 1. Insert new Garment
        self.conn.execute(
            """
            INSERT INTO Garment(
                id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color,
                formality, condition_state, availability_state, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'CLEAN', 'AVAILABLE', ?, ?);
            """,
            (new_garment_id, row["user_id"], name, p_cat, subcat, cut, color, formality, now, now),
        )

        # 2. Attach Image
        image_id = f"gi_{uuid.uuid4().hex[:12]}"
        raw_name = Path(row["temporary_image_path"]).name
        catalogued_path = f"catalogued/{new_garment_id}/{image_id}_{raw_name}"

        src_path = Path(row["temporary_image_path"])
        copied_dst: Optional[Path] = None
        if src_path.is_file():
            dst_path = Path(catalogued_path)
            dst_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_path, dst_path)
            copied_dst = dst_path

            def _cleanup_link():
                if dst_path.is_file():
                    try:
                        dst_path.unlink()
                    except Exception:
                        pass
            register_rollback_callback(self.conn, _cleanup_link)

        try:
            self.conn.execute(
                """
                INSERT INTO GarmentImage(id, garment_id, image_path, image_type, is_primary, created_at)
                VALUES (?, ?, ?, 'full', 1, ?);
                """,
                (image_id, new_garment_id, catalogued_path, now),
            )

            # 3. Fingerprint
            gif_id = f"gif_{uuid.uuid4().hex[:12]}"
            self.conn.execute(
                """
                INSERT INTO GarmentImageFingerprint(id, garment_image_id, sha256, perceptual_hash, created_at)
                VALUES (?, ?, ?, ?, ?);
                """,
                (gif_id, image_id, row["sha256"], row["perceptual_hash"], now),
            )

            # 4. Insert durable Similarity Link
            gid_a, gid_b = (min(existing_id, new_garment_id), max(existing_id, new_garment_id))
            self.conn.execute(
                """
                INSERT INTO GarmentSimilarityLink(garment_id_a, garment_id_b, link_type, created_at)
                VALUES (?, ?, 'OWNER_CONFIRMED_SIMILAR', ?);
                """,
                (gid_a, gid_b, now),
            )

            # 5. Finalize candidate status
            res = self.conn.execute(
                """
                UPDATE GarmentDuplicateCandidate
                SET status = 'LINKED_SIMILAR',
                    resolution_note = ?,
                    resolved_at = ?
                WHERE id = ? AND status = 'PENDING';
                """,
                (resolution_note, now, candidate_id),
            )
            if res.rowcount != 1:
                raise ValueError(f"Candidate {candidate_id} is not in PENDING status")

            # 6. Finalize draft status
            self.conn.execute(
                "UPDATE GarmentIntakeDraft SET status = 'CREATED_NEW_GARMENT' WHERE id = ?;",
                (row["intake_draft_id"],),
            )

            # 7. Dismiss sibling candidates
            self.conn.execute(
                """
                UPDATE GarmentDuplicateCandidate
                SET status = 'DISMISSED',
                    resolution_note = 'Resolved through sibling candidate',
                    resolved_at = ?
                WHERE intake_draft_id = ?
                  AND id != ?
                  AND status = 'PENDING';
                """,
                (now, row["intake_draft_id"], candidate_id),
            )

            return new_garment_id
        except Exception:
            if copied_dst and copied_dst.is_file():
                try:
                    copied_dst.unlink()
                except Exception:
                    pass
            raise

    def resolve_as_dismissed(
        self,
        candidate_id: str,
        resolution_note: Optional[str] = None,
    ) -> None:
        """Marks candidate as DISMISSED."""
        self._require_transaction()
        now = self._utc_now()
        res = self.conn.execute(
            """
            UPDATE GarmentDuplicateCandidate
            SET status = 'DISMISSED',
                resolution_note = ?,
                resolved_at = ?
            WHERE id = ? AND status = 'PENDING';
            """,
            (resolution_note, now, candidate_id),
        )
        if res.rowcount != 1:
            raise KeyError(f"Candidate {candidate_id} not found or already resolved")
