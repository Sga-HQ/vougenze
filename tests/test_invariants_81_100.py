"""
Targeted mechanical verification test suite for Invariants 81–100.
Tests the remaining transaction boundaries, candidate truncation, snapshot immutability,
exact intake lifecycle, durable similarity, strategy diversity enforcement,
authorization reservations and consumption, and migration hash integrity.
"""
import copy
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.candidate_generator import CandidateGenerator
from core.db import Database
from core.evidence_service import PreferenceEvidenceService, SnapshotConflictError
from core.intake_service import IntakeService
from core.laundry_service import LaundryService
from core.models import (
    AvailabilityState,
    ConditionState,
    InclusionType,
    InvalidationReason,
    LaundryBatchStatus,
    LaundryItemStatus,
    OutfitCandidate,
    PlanStatus,
    PriorityLevel,
    RejectionReason,
    StrategyMode,
)
from core.planner import ContinuationPlanner
from core.session_service import SessionService
from core.wear_service import WearService


class TestInvariants81to100(unittest.TestCase):

    def setUp(self):
        self.db = Database(":memory:")
        self.db.migrate()
        self.user_id = "u_inv_test"
        self.temp_dir = tempfile.mkdtemp()

        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO User(id, display_name) VALUES (?, 'Invariant Tester');",
                (self.user_id,),
            )

    def tearDown(self):
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)
        # Clean up any local catalogued test dir
        if os.path.exists("catalogued"):
            shutil.rmtree("catalogued", ignore_errors=True)

    # -------------------------------------------------------------------------
    # Invariant 81: Candidate pool truncation retains unique HIGH-session item
    # -------------------------------------------------------------------------
    def test_81_candidate_pool_truncation_retains_unique_high_session_item(self):
        """Invariant 81: Pool truncation preserves critical items regardless of like_rating."""
        with self.db.transaction() as conn:
            # Insert 20 casual shirts with like_rating = 10, formality = 1
            for i in range(20):
                conn.execute(
                    """
                    INSERT INTO Garment(
                        id, user_id, name, physical_category, subcategory, cut_and_fit,
                        primary_color, formality, like_rating, usage_contexts
                    )
                    VALUES (?, ?, ?, 'top', 'shirt', 'regular', 'white', 1, 10, '["casual"]');
                    """,
                    (f"g_casual_{i}", self.user_id, f"Casual Shirt {i}"),
                )

            # Insert 1 presentation-only shirt with like_rating = 1, formality = 5
            conn.execute(
                """
                INSERT INTO Garment(
                    id, user_id, name, physical_category, subcategory, cut_and_fit,
                    primary_color, formality, like_rating, usage_contexts
                )
                VALUES ('g_critical_pres', ?, 'Formal Presentation Shirt', 'top', 'shirt', 'tailored', 'navy', 5, 1, '["presentation"]');
                """,
                (self.user_id,),
            )

            cur = conn.execute("SELECT * FROM Garment WHERE user_id = ?;", (self.user_id,))
            pool = cur.fetchall()
            self.assertEqual(len(pool), 21)

            # Truncate pool to max 10 items
            truncated = CandidateGenerator._truncate_role_pool(pool, max_items=10)
            self.assertEqual(len(truncated), 10)
            truncated_ids = {g["id"] for g in truncated}

            # The critical presentation shirt with like_rating 1 must survive truncation!
            self.assertIn("g_critical_pres", truncated_ids)
            print("PASS: Invariant 81: Candidate pool truncation retained unique HIGH-session item despite low like rating")

    # -------------------------------------------------------------------------
    # Invariant 82: Cartesian truncation preserves critical & one-piece quotas
    # -------------------------------------------------------------------------
    def test_82_cartesian_truncation_preserves_critical_and_one_piece_quotas(self):
        """Invariant 82: Dedicated candidate quotas protect one-pieces even if standard Cartesian combinations exceed budget."""
        with self.db.transaction() as conn:
            # Create session needing top, bottom, footwear
            sid = "ws_inv_82"
            conn.execute(
                "INSERT INTO WearSession(id, user_id, session_date, session_type, importance, required_roles) "
                "VALUES (?, ?, '2026-03-10', 'casual', 'NORMAL', '[\"top\", \"bottom\", \"footwear\"]');",
                (sid, self.user_id),
            )

            # 10 tops, 10 bottoms, 5 shoes -> 500 combinations (> 200 MAX_CANDIDATES)
            for i in range(10):
                conn.execute(
                    "INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color) "
                    "VALUES (?, ?, ?, 'top', 'shirt', 'regular', 'white');",
                    (f"g_top_{i}", self.user_id, f"Top {i}"),
                )
                conn.execute(
                    "INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color) "
                    "VALUES (?, ?, ?, 'bottom', 'jeans', 'regular', 'blue');",
                    (f"g_bot_{i}", self.user_id, f"Bottom {i}"),
                )
            for i in range(5):
                conn.execute(
                    "INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color) "
                    "VALUES (?, ?, ?, 'footwear', 'sneakers', 'regular', 'black');",
                    (f"g_shoe_{i}", self.user_id, f"Shoe {i}"),
                )

            # Insert 1 one-piece jumpsuit that fills both top and bottom
            conn.execute(
                """
                INSERT INTO Garment(
                    id, user_id, name, physical_category, subcategory, cut_and_fit,
                    primary_color, fills_roles
                )
                VALUES ('g_jumpsuit_82', ?, 'Silk Jumpsuit', 'one_piece', 'jumpsuit', 'regular', 'black', '["top", "bottom"]');
                """,
                (self.user_id,),
            )

            gen = CandidateGenerator(conn)
            candidates = gen.generate_candidates(sid)

            # Assert candidates were generated and one-piece candidates exist
            self.assertTrue(len(candidates) > 0)
            one_piece_cands = [c for c in candidates if "g_jumpsuit_82" in c.garment_ids]
            self.assertTrue(
                len(one_piece_cands) >= 1,
                "Dedicated candidate quota must ensure one-piece candidates are not starved by 200 standard Cartesian combinations",
            )
            print(f"PASS: Invariant 82: Dedicated candidate quota preserved {len(one_piece_cands)} one-piece candidates under heavy Cartesian pressure")

    # -------------------------------------------------------------------------
    # Invariant 83: Reused snapshot ID with different role membership is rejected
    # -------------------------------------------------------------------------
    def test_83_reused_snapshot_id_with_different_roles_rejected(self):
        """Invariant 83: Immutable snapshot validation detects collision/incompatible content and raises SnapshotConflictError."""
        with self.db.transaction() as conn:
            ev_svc = PreferenceEvidenceService(conn)
            sid = "ws_inv_83"
            conn.execute(
                "INSERT INTO WearSession(id, user_id, session_date, session_type) VALUES (?, ?, '2026-03-10', 'casual');",
                (sid, self.user_id),
            )
            conn.execute(
                "INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color) "
                "VALUES ('g_83_a', ?, 'Shirt A', 'top', 'shirt', 'regular', 'white'), "
                "       ('g_83_b', ?, 'Pants B', 'bottom', 'jeans', 'regular', 'blue'), "
                "       ('g_83_c', ?, 'Pants C', 'bottom', 'chinos', 'regular', 'khaki'), "
                "       ('g_83_s', ?, 'Shoes S', 'footwear', 'sneakers', 'regular', 'black');",
                (self.user_id, self.user_id, self.user_id, self.user_id),
            )

            cand1 = OutfitCandidate(
                id="cand_fixed_id",
                garment_ids=["g_83_a", "g_83_b", "g_83_s"],
                roles={"top": "g_83_a", "bottom": "g_83_b", "footwear": "g_83_s"},
            )
            snap1_id = ev_svc.create_candidate_snapshot(
                user_id=self.user_id,
                candidate=cand1,
                session_id=sid,
            )
            self.assertEqual(snap1_id, "cand_fixed_id")

            # Identical candidate re-snapshot returns same ID cleanly
            snap1_repeat = ev_svc.create_candidate_snapshot(
                user_id=self.user_id,
                candidate=cand1,
                session_id=sid,
            )
            self.assertEqual(snap1_repeat, snap1_id)

            # Different candidate reusing the same ID must raise SnapshotConflictError
            cand2 = OutfitCandidate(
                id="cand_fixed_id",
                garment_ids=["g_83_a", "g_83_c", "g_83_s"],
                roles={"top": "g_83_a", "bottom": "g_83_c", "footwear": "g_83_s"},
            )
            with self.assertRaises(SnapshotConflictError):
                ev_svc.create_candidate_snapshot(
                    user_id=self.user_id,
                    candidate=cand2,
                    session_id=sid,
                )
            print("PASS: Invariant 83: Reused snapshot ID with different role membership raised SnapshotConflictError")

    # -------------------------------------------------------------------------
    # Invariant 84: Rejection evidence validates inclusion-type consistency
    # -------------------------------------------------------------------------
    def test_84_rejection_evidence_validates_inclusion_consistency(self):
        """Invariant 84: Shared inclusion validator enforces semantic consistency in rejection alternatives."""
        with self.db.transaction() as conn:
            ev_svc = PreferenceEvidenceService(conn)
            sid = "ws_inv_84"
            conn.execute(
                "INSERT INTO WearSession(id, user_id, session_date, session_type) VALUES (?, ?, '2026-03-10', 'casual');",
                (sid, self.user_id),
            )
            conn.execute(
                "INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color) "
                "VALUES ('g_84_t', ?, 'Top 1', 'top', 'shirt', 'regular', 'white'), "
                "       ('g_84_t2', ?, 'Top 2', 'top', 'shirt', 'regular', 'blue'), "
                "       ('g_84_b', ?, 'Bottom', 'bottom', 'jeans', 'regular', 'blue'), "
                "       ('g_84_s', ?, 'Shoes', 'footwear', 'sneakers', 'regular', 'black');",
                (self.user_id, self.user_id, self.user_id, self.user_id),
            )

            rej_cand = OutfitCandidate(
                id="cand_rej_84",
                garment_ids=["g_84_t", "g_84_b", "g_84_s"],
                roles={"top": "g_84_t", "bottom": "g_84_b", "footwear": "g_84_s"},
            )
            alt_cand = OutfitCandidate(
                id="cand_alt_84",
                garment_ids=["g_84_t2", "g_84_b", "g_84_s"],
                roles={"top": "g_84_t2", "bottom": "g_84_b", "footwear": "g_84_s"},
            )

            # Inconsistent: INFEASIBLE_REFERENCE with was_feasible=True must be rejected
            invalid_alt_1 = {
                "candidate": alt_cand,
                "inclusion_type": InclusionType.INFEASIBLE_REFERENCE.value,
                "was_feasible": True,
                "exclusion_reason": "In laundry",
            }
            with self.assertRaises(ValueError):
                ev_svc.record_rejection_evidence(
                    user_id=self.user_id,
                    session_id=sid,
                    occasion_context="casual",
                    rejected_candidate=rej_cand,
                    rejection_reason=RejectionReason.OTHER,
                    alternatives=[invalid_alt_1],
                )

            # Inconsistent: PRESENTED alternative with presentation_order = None must be rejected
            invalid_alt_2 = {
                "candidate": alt_cand,
                "inclusion_type": InclusionType.PRESENTED.value,
                "was_feasible": True,
                "presentation_order": None,
            }
            with self.assertRaises(ValueError):
                ev_svc.record_rejection_evidence(
                    user_id=self.user_id,
                    session_id=sid,
                    occasion_context="casual",
                    rejected_candidate=rej_cand,
                    rejection_reason=RejectionReason.OTHER,
                    alternatives=[invalid_alt_2],
                )

            print("PASS: Invariant 84: Shared inclusion validator rejected inconsistent alternative configurations")

    # -------------------------------------------------------------------------
    # Invariant 85: Missing intake file without explicit test hash is rejected
    # -------------------------------------------------------------------------
    def test_85_missing_intake_file_without_explicit_test_hash_rejected(self):
        """Invariant 85: IntakeService raises FileNotFoundError when no file exists and no synthetic hash is provided."""
        with self.db.transaction() as conn:
            intake_svc = IntakeService(conn)
            missing_path = os.path.join(self.temp_dir, "nonexistent_image_123.jpg")

            with self.assertRaises(FileNotFoundError):
                intake_svc.stage_intake_draft(
                    user_id=self.user_id,
                    temporary_image_path=missing_path,
                    sha256_hash=None,  # No explicit synthetic test hash
                )
            print("PASS: Invariant 85: Missing intake file without explicit synthetic hash raised FileNotFoundError")

    # -------------------------------------------------------------------------
    # Invariant 86: Resolving one duplicate candidate closes sibling candidates
    # -------------------------------------------------------------------------
    def test_86_resolving_one_duplicate_closes_sibling_candidates(self):
        """Invariant 86: Resolving candidate as SAME_GARMENT closes sibling pending candidates as DISMISSED."""
        with self.db.transaction() as conn:
            intake_svc = IntakeService(conn)

            # Create existing garments A and B
            conn.execute(
                "INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color) "
                "VALUES ('g_exist_a', ?, 'Existing A', 'top', 'shirt', 'regular', 'white'), "
                "       ('g_exist_b', ?, 'Existing B', 'top', 'shirt', 'regular', 'white');",
                (self.user_id, self.user_id),
            )

            # Create real temporary image file
            img_file = Path(self.temp_dir) / "test_sibling.jpg"
            img_file.write_bytes(b"sibling_test_bytes_123")

            draft_id, _ = intake_svc.stage_intake_draft(
                user_id=self.user_id,
                temporary_image_path=str(img_file),
            )

            # Insert two duplicate candidate matches for this single intake draft
            cand_a = "cand_dup_a"
            cand_b = "cand_dup_b"
            conn.execute(
                """
                INSERT INTO GarmentDuplicateCandidate(id, intake_draft_id, user_id, existing_garment_id, exact_hash_match, detector_version, status)
                VALUES (?, ?, ?, 'g_exist_a', 1, 'v0_heuristic', 'PENDING'),
                       (?, ?, ?, 'g_exist_b', 0, 'v0_heuristic', 'PENDING');
                """,
                (cand_a, draft_id, self.user_id, cand_b, draft_id, self.user_id),
            )

            # Resolve cand_a as SAME_GARMENT
            intake_svc.resolve_as_same_garment(cand_a)

            # Verify cand_a is SAME_GARMENT
            row_a = conn.execute("SELECT status FROM GarmentDuplicateCandidate WHERE id = ?;", (cand_a,)).fetchone()
            self.assertEqual(row_a["status"], "SAME_GARMENT")

            # Verify sibling candidate cand_b was atomically closed as DISMISSED
            row_b = conn.execute("SELECT status, resolution_note FROM GarmentDuplicateCandidate WHERE id = ?;", (cand_b,)).fetchone()
            self.assertEqual(row_b["status"], "DISMISSED")
            self.assertEqual(row_b["resolution_note"], "Resolved through sibling candidate")
            print("PASS: Invariant 86: Resolving duplicate candidate atomically marked sibling candidates as DISMISSED")

    # -------------------------------------------------------------------------
    # Invariant 87: LINKED_SIMILAR creates a durable similarity relationship
    # -------------------------------------------------------------------------
    def test_87_linked_similar_creates_durable_similarity_link(self):
        """Invariant 87: resolve_as_linked_similar creates new garment and inserts into GarmentSimilarityLink."""
        with self.db.transaction() as conn:
            intake_svc = IntakeService(conn)

            conn.execute(
                "INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color) "
                "VALUES ('g_orig_87', ?, 'Original Shirt', 'top', 'shirt', 'regular', 'white');",
                (self.user_id,),
            )

            img_file = Path(self.temp_dir) / "test_link.jpg"
            img_file.write_bytes(b"link_similar_image_bytes")

            draft_id, _ = intake_svc.stage_intake_draft(
                user_id=self.user_id,
                temporary_image_path=str(img_file),
            )

            cand_id = "cand_link_87"
            conn.execute(
                """
                INSERT INTO GarmentDuplicateCandidate(id, intake_draft_id, user_id, existing_garment_id, exact_hash_match, detector_version, status)
                VALUES (?, ?, ?, 'g_orig_87', 0, 'v0_heuristic', 'PENDING');
                """,
                (cand_id, draft_id, self.user_id),
            )

            new_gid = intake_svc.resolve_as_linked_similar(
                candidate_id=cand_id,
                garment_data={"name": "Similar Linen Shirt", "physical_category": "top", "subcategory": "shirt", "cut_and_fit": "regular", "primary_color": "white", "formality": 2},
                resolution_note="Keep both and link as similar",
            )

            # Verify candidate status and draft status
            c_row = conn.execute("SELECT status FROM GarmentDuplicateCandidate WHERE id = ?;", (cand_id,)).fetchone()
            self.assertEqual(c_row["status"], "LINKED_SIMILAR")

            # Verify durable similarity link exists in GarmentSimilarityLink
            g_a, g_b = min("g_orig_87", new_gid), max("g_orig_87", new_gid)
            link_row = conn.execute(
                "SELECT * FROM GarmentSimilarityLink WHERE garment_id_a = ? AND garment_id_b = ?;",
                (g_a, g_b),
            ).fetchone()
            self.assertIsNotNone(link_row)
            self.assertEqual(link_row["link_type"], "OWNER_CONFIRMED_SIMILAR")
            print("PASS: Invariant 87: LINKED_SIMILAR created durable link row in GarmentSimilarityLink")

    # -------------------------------------------------------------------------
    # Invariant 88: Duplicate resolution failure rolls back status and image rows
    # -------------------------------------------------------------------------
    def test_88_duplicate_resolution_failure_rolls_back_status_and_images(self):
        """Invariant 88: Transaction boundary ensures complete rollback if duplicate resolution fails midway."""
        img_file = Path(self.temp_dir) / "test_rollback.jpg"
        img_file.write_bytes(b"bytes_for_rollback_test")

        with self.db.transaction() as conn:
            intake_svc = IntakeService(conn)
            conn.execute(
                "INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color) "
                "VALUES ('g_orig_88', ?, 'Base Garment', 'top', 'shirt', 'regular', 'white');",
                (self.user_id,),
            )
            draft_id, _ = intake_svc.stage_intake_draft(user_id=self.user_id, temporary_image_path=str(img_file))
            cand_id = "cand_88_roll"
            conn.execute(
                """
                INSERT INTO GarmentDuplicateCandidate(id, intake_draft_id, user_id, existing_garment_id, exact_hash_match, detector_version, status)
                VALUES (?, ?, ?, 'g_orig_88', 1, 'v0_heuristic', 'PENDING');
                """,
                (cand_id, draft_id, self.user_id),
            )

        # Inject failure inside transaction
        try:
            with self.db.transaction() as conn:
                intake_svc = IntakeService(conn)
                intake_svc.resolve_as_same_garment(cand_id)
                # Injected mid-transaction failure
                raise RuntimeError("Simulated crash before transaction commit")
        except RuntimeError:
            pass

        # Verify candidate is still PENDING and no image was committed
        with self.db.connection() as conn:
            c_row = conn.execute("SELECT status FROM GarmentDuplicateCandidate WHERE id = ?;", (cand_id,)).fetchone()
            self.assertEqual(c_row["status"], "PENDING")
            img_count = conn.execute("SELECT count(*) FROM GarmentImage WHERE garment_id = 'g_orig_88';").fetchone()[0]
            self.assertEqual(img_count, 0)
            print("PASS: Invariant 88: Duplicate resolution failure rolled back candidate status and image rows cleanly")

    # -------------------------------------------------------------------------
    # Invariant 89: Laundry dispatch requires active transaction
    # -------------------------------------------------------------------------
    def test_89_laundry_dispatch_requires_active_transaction(self):
        """Invariant 89: Calling send_laundry_batch without active transaction raises RuntimeError."""
        with self.db.connection() as conn:
            self.assertFalse(conn.in_transaction)
            laundry_svc = LaundryService(conn)
            with self.assertRaises(RuntimeError) as cm:
                laundry_svc.send_laundry_batch(
                    user_id=self.user_id,
                    garment_ids=["g_nonexistent"],
                    sent_date="2026-03-10",
                    expected_return_date="2026-03-12",
                )
            self.assertIn("require an active transaction", str(cm.exception))
            print("PASS: Invariant 89: Laundry dispatch outside transaction raised RuntimeError")

    # -------------------------------------------------------------------------
    # Invariant 90: Item return failure rolls back batch item, wash, garment and batch
    # -------------------------------------------------------------------------
    def test_90_item_return_failure_rolls_back_all_mutations(self):
        """Invariant 90: Injected return failure rolls back all batch, wash, item, and garment state mutations."""
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color, condition_state, availability_state) "
                "VALUES ('g_laundry_90', ?, 'Shirt 90', 'top', 'shirt', 'regular', 'white', 'NEEDS_WASH', 'AVAILABLE');",
                (self.user_id,),
            )
            laundry_svc = LaundryService(conn)
            batch_id = laundry_svc.send_laundry_batch(
                user_id=self.user_id,
                garment_ids=["g_laundry_90"],
                sent_date="2026-03-10",
                expected_return_date="2026-03-12",
            )

        # Injected failure during return
        try:
            with self.db.transaction() as conn:
                laundry_svc = LaundryService(conn)
                laundry_svc.record_item_return(
                    batch_id=batch_id,
                    garment_id="g_laundry_90",
                    item_status=LaundryItemStatus.RETURNED_CLEAN,
                    wash_confirmed=True,
                    return_date="2026-03-12",
                )
                raise RuntimeError("Simulated failure right before return commit")
        except RuntimeError:
            pass

        # Verify state is untouched (still IN_LAUNDRY, no WashEvent)
        with self.db.connection() as conn:
            g_row = conn.execute("SELECT availability_state, current_laundry_batch_id FROM Garment WHERE id = 'g_laundry_90';").fetchone()
            self.assertEqual(g_row["availability_state"], "IN_LAUNDRY")
            self.assertEqual(g_row["current_laundry_batch_id"], batch_id)

            item_row = conn.execute("SELECT item_status FROM LaundryBatchItem WHERE batch_id = ?;", (batch_id,)).fetchone()
            self.assertEqual(item_row["item_status"], "IN_LAUNDRY")

            wash_count = conn.execute("SELECT count(*) FROM WashEvent WHERE garment_id = 'g_laundry_90';").fetchone()[0]
            self.assertEqual(wash_count, 0)
            print("PASS: Invariant 90: Item return failure rolled back all mutations completely")

    # -------------------------------------------------------------------------
    # Invariant 91: Policy replacement rejects cross-owner policy
    # -------------------------------------------------------------------------
    def test_91_policy_replacement_rejects_cross_owner_policy(self):
        """Invariant 91: update_policy rejects cross-owner policy replacement with PermissionError."""
        other_user = "u_other_user"
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO User(id, display_name) VALUES (?, 'Other User');", (other_user,))
            pol_id = "lp_other_user"
            conn.execute(
                """
                INSERT INTO LaundryPolicy(id, user_id, policy_type, handover_weekday, return_weekday, interval_days, dirty_threshold, effective_from)
                VALUES (?, ?, 'commercial', 1, 4, 3, 5, '2026-01-01');
                """,
                (pol_id, other_user),
            )

            laundry_svc = LaundryService(conn)
            # User self.user_id attempts to update other_user's policy
            with self.assertRaises(PermissionError):
                laundry_svc.update_policy(
                    user_id=self.user_id,
                    old_policy_id=pol_id,
                    new_policy_data={"policy_type": "commercial", "interval_days": 2},
                    effective_from="2026-03-10",
                )

            # Owner updates policy -> closes old policy with effective_until
            new_pol_id = laundry_svc.update_policy(
                user_id=other_user,
                old_policy_id=pol_id,
                new_policy_data={"policy_type": "commercial", "interval_days": 2},
                effective_from="2026-03-10",
            )
            old_row = conn.execute("SELECT effective_until FROM LaundryPolicy WHERE id = ?;", (pol_id,)).fetchone()
            self.assertEqual(old_row["effective_until"], "2026-03-10")

            # Updating already-closed policy raises ValueError
            with self.assertRaises(ValueError) as cm:
                laundry_svc.update_policy(
                    user_id=other_user,
                    old_policy_id=pol_id,
                    new_policy_data={"policy_type": "commercial", "interval_days": 1},
                    effective_from="2026-03-15",
                )
            self.assertIn("already closed", str(cm.exception))
            print("PASS: Invariant 91: Policy replacement rejected cross-owner and already-closed policy")

    # -------------------------------------------------------------------------
    # Invariant 92: Closed batch cannot be marked delayed
    # -------------------------------------------------------------------------
    def test_92_closed_batch_cannot_be_marked_delayed(self):
        """Invariant 92: mark_batch_delayed rejects completed batches (RETURNED_CONFIRMED)."""
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color, condition_state, availability_state) "
                "VALUES ('g_closed_92', ?, 'Shirt 92', 'top', 'shirt', 'regular', 'white', 'NEEDS_WASH', 'AVAILABLE');",
                (self.user_id,),
            )
            laundry_svc = LaundryService(conn)
            batch_id = laundry_svc.send_laundry_batch(
                user_id=self.user_id,
                garment_ids=["g_closed_92"],
                sent_date="2026-03-10",
                expected_return_date="2026-03-12",
            )
            laundry_svc.record_item_return(
                batch_id=batch_id,
                garment_id="g_closed_92",
                item_status=LaundryItemStatus.RETURNED_CLEAN,
                wash_confirmed=True,
                return_date="2026-03-12",
            )
            # Batch is now RETURNED_CONFIRMED
            b_row = conn.execute("SELECT status FROM LaundryBatch WHERE id = ?;", (batch_id,)).fetchone()
            self.assertEqual(b_row["status"], "RETURNED_CONFIRMED")

            # Marking delayed must be rejected
            with self.assertRaises(ValueError) as cm:
                laundry_svc.mark_batch_delayed(
                    batch_id=batch_id,
                    new_expected_return_date="2026-03-15",
                    delay_reason="Traffic",
                )
            self.assertIn("terminal status RETURNED_CONFIRMED", str(cm.exception))
            print("PASS: Invariant 92: Closed laundry batch was rejected from being marked delayed")

    # -------------------------------------------------------------------------
    # Invariant 93: Supplied template respects effective dates
    # -------------------------------------------------------------------------
    def test_93_supplied_template_respects_effective_dates(self):
        """Invariant 93: SessionService checks template effective_from and effective_until per civil date."""
        with self.db.transaction() as conn:
            tpl_id = "tpl_effective_test"
            # Template effective only for 2026-03-01 to 2026-03-02
            conn.execute(
                """
                INSERT INTO WearSessionTemplate(id, user_id, template_name, effective_from, effective_until)
                VALUES (?, ?, 'Strict Date Template', '2026-03-01', '2026-03-02');
                """,
                (tpl_id, self.user_id),
            )
            # Day 0 to 6 all defined
            for dow in range(7):
                conn.execute(
                    """
                    INSERT INTO WearSessionTemplateDay(id, template_id, day_of_week, session_type, importance, required_roles)
                    VALUES (?, ?, ?, 'work', 'NORMAL', '["top", "bottom"]');
                    """,
                    (f"td_{dow}", tpl_id, dow),
                )

            session_svc = SessionService(conn)
            # Request materialization for 5-day horizon: 2026-03-01 through 2026-03-05
            session_ids = session_svc.materialize_sessions_for_horizon(
                user_id=self.user_id,
                start_date="2026-03-01",
                end_date="2026-03-05",
                template_id=tpl_id,
            )
            cur = conn.execute(
                "SELECT session_date FROM WearSession WHERE id IN ({});".format(
                    ",".join("?" for _ in session_ids)
                ),
                session_ids,
            )
            session_dates = [r["session_date"] for r in cur.fetchall()]
            # Must ONLY contain 2026-03-01 and 2026-03-02
            self.assertEqual(sorted(session_dates), ["2026-03-01", "2026-03-02"])
            print("PASS: Invariant 93: Supplied template respected civil date effective window bounds strictly")

    # -------------------------------------------------------------------------
    # Invariant 94: Plan invalidation uses actual created session dates
    # -------------------------------------------------------------------------
    def test_94_plan_invalidation_uses_actual_created_session_dates(self):
        """Invariant 94: Plan invalidation only supersedes plans overlapping actual newly created dates."""
        with self.db.transaction() as conn:
            session_svc = SessionService(conn)

            # 1. Pre-create sessions for 2026-03-01 and 2026-03-02
            conn.execute(
                "INSERT INTO WearSession(id, user_id, session_date, session_type) VALUES "
                "('ws_94_1', ?, '2026-03-01', 'casual'), ('ws_94_2', ?, '2026-03-02', 'casual');",
                (self.user_id, self.user_id),
            )

            # 2. Propose a plan covering 2026-03-01 to 2026-03-02
            plan_id = "rp_early_horizon"
            conn.execute(
                """
                INSERT INTO RecommendationPlan(id, user_id, strategy_mode, horizon_start_date, horizon_end_date, status)
                VALUES (?, ?, 'SAFE', '2026-03-01', '2026-03-02', 'PROPOSED');
                """,
                (plan_id, self.user_id),
            )

            # 3. Create template valid only for 2026-03-05
            tpl_id = "tpl_94_late"
            conn.execute(
                "INSERT INTO WearSessionTemplate(id, user_id, template_name, effective_from, effective_until) "
                "VALUES (?, ?, 'Late Template', '2026-03-05', '2026-03-05');",
                (tpl_id, self.user_id),
            )
            conn.execute(
                "INSERT INTO WearSessionTemplateDay(id, template_id, day_of_week, session_type, importance, required_roles) "
                "VALUES ('td_94', ?, 4, 'work', 'NORMAL', '[\"top\", \"bottom\"]');",
                (tpl_id,),
            )

            # 4. Request materialization over horizon 2026-03-01 to 2026-03-05
            # Since 2026-03-01 and 2026-03-02 already have sessions, and template only creates on 2026-03-05,
            # newly created dates is only {'2026-03-05'}.
            session_svc.materialize_sessions_for_horizon(
                user_id=self.user_id,
                start_date="2026-03-01",
                end_date="2026-03-05",
                template_id=tpl_id,
            )

            # Plan for 2026-03-01 to 2026-03-02 must NOT be invalidated!
            p_row = conn.execute("SELECT status FROM RecommendationPlan WHERE id = ?;", (plan_id,)).fetchone()
            self.assertEqual(p_row["status"], "PROPOSED")
            print("PASS: Invariant 94: Plan invalidation correctly preserved proposed plan not overlapping created dates")

    # -------------------------------------------------------------------------
    # Invariant 95: Applied migration with changed SHA-256 fails closed
    # -------------------------------------------------------------------------
    def test_95_applied_migration_with_changed_sha256_fails_closed(self):
        """Invariant 95: Modifying migration ledger or file so SHA-256 differs raises RuntimeError."""
        with self.db.transaction() as conn:
            # Tamper with recorded migration hash
            conn.execute(
                "UPDATE SchemaMigration SET sha256 = 'tampered_sha256_hash_value' WHERE version = '0001_v0_schema';"
            )

        # Running db.migrate() must fail closed with RuntimeError
        with self.assertRaises(RuntimeError) as cm:
            self.db.migrate()
        self.assertIn("differs from current file", str(cm.exception))
        print("PASS: Invariant 95: Applied migration hash tampering failed closed with RuntimeError")

    # -------------------------------------------------------------------------
    # Invariant 96: Balanced reaches attainable slot-level difference from Safe
    # -------------------------------------------------------------------------
    def test_96_balanced_reaches_attainable_slot_level_difference(self):
        """Invariant 96: Balanced search reaches min(2, attainable_diffs) slot-level difference from Safe."""
        with self.db.transaction() as conn:
            planner = ContinuationPlanner(conn)
            # Create 3 sessions
            sids = ["ws_96_1", "ws_96_2", "ws_96_3"]
            for i, sid in enumerate(sids):
                conn.execute(
                    "INSERT INTO WearSession(id, user_id, session_date, session_type, importance, required_roles) "
                    "VALUES (?, ?, ?, 'casual', 'NORMAL', '[\"top\", \"bottom\", \"footwear\"]');",
                    (sid, self.user_id, f"2026-03-1{i}"),
                )

            # Insert diverse wardrobe
            for i in range(4):
                conn.execute(
                    "INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color, formality, like_rating) "
                    "VALUES (?, ?, ?, 'top', 'shirt', 'regular', ?, 2, ?);",
                    (f"g_96_top_{i}", self.user_id, f"Top {i}", f"color_{i}", 8 - i),
                )
                conn.execute(
                    "INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color, formality, like_rating) "
                    "VALUES (?, ?, ?, 'bottom', 'jeans', 'regular', ?, 2, ?);",
                    (f"g_96_bot_{i}", self.user_id, f"Bottom {i}", f"color_{i}", 8 - i),
                )
            conn.execute(
                "INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color) "
                "VALUES ('g_96_shoe', ?, 'Sneakers', 'footwear', 'sneakers', 'regular', 'white');",
                (self.user_id,),
            )

            # 1. Generate Safe plan
            safe_plan_id = planner.generate_plan(
                user_id=self.user_id,
                horizon_start_date="2026-03-10",
                horizon_end_date="2026-03-12",
                strategy_mode=StrategyMode.SAFE,
            )
            safe_plan = planner.get_plan(safe_plan_id)
            safe_signatures = {
                sid: tuple(sorted(cand.garment_ids))
                for sid, cand in safe_plan.assignments.items()
            }

            # 2. Generate Balanced plan referencing Safe
            balanced_plan_id = planner.generate_plan(
                user_id=self.user_id,
                horizon_start_date="2026-03-10",
                horizon_end_date="2026-03-12",
                strategy_mode=StrategyMode.BALANCED,
                reference_safe_assignments=safe_signatures,
            )
            balanced_plan = planner.get_plan(balanced_plan_id)

            # Verify diversity target and achieved
            self.assertGreaterEqual(balanced_plan.diversity_target, 1)
            self.assertGreaterEqual(balanced_plan.diversity_achieved, balanced_plan.diversity_target)
            print(f"PASS: Invariant 96: Balanced strategy achieved {balanced_plan.diversity_achieved} diffs (target {balanced_plan.diversity_target})")

    # -------------------------------------------------------------------------
    # Invariant 97: Exploratory reaches attainable low/normal novelty target
    # -------------------------------------------------------------------------
    def test_97_exploratory_reaches_attainable_novelty_target(self):
        """Invariant 97: Exploratory search reaches attainable low/normal novelty target."""
        with self.db.transaction() as conn:
            planner = ContinuationPlanner(conn)
            # Create 3 sessions (2 normal, 1 high)
            sids = [("ws_97_1", "NORMAL", "2026-03-10"), ("ws_97_2", "NORMAL", "2026-03-11"), ("ws_97_3", "HIGH", "2026-03-12")]
            for sid, imp, dt in sids:
                conn.execute(
                    "INSERT INTO WearSession(id, user_id, session_date, session_type, importance, required_roles) "
                    "VALUES (?, ?, ?, 'casual', ?, '[\"top\", \"bottom\", \"footwear\"]');",
                    (sid, self.user_id, dt, imp),
                )

            # Insert garments
            for i in range(4):
                conn.execute(
                    "INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color, formality, like_rating) "
                    "VALUES (?, ?, ?, 'top', 'shirt', 'regular', ?, 2, ?), (?, ?, ?, 'bottom', 'jeans', 'regular', ?, 2, ?);",
                    (f"g_97_top_{i}", self.user_id, f"Top {i}", f"c_{i}", 8 - i, f"g_97_bot_{i}", self.user_id, f"Bottom {i}", f"c_{i}", 8 - i),
                )
            conn.execute(
                "INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color) "
                "VALUES ('g_97_shoe', ?, 'Shoes', 'footwear', 'sneakers', 'regular', 'white');",
                (self.user_id,),
            )

            # 1. Generate Safe plan
            safe_plan_id = planner.generate_plan(
                user_id=self.user_id,
                horizon_start_date="2026-03-10",
                horizon_end_date="2026-03-12",
                strategy_mode=StrategyMode.SAFE,
            )
            safe_plan = planner.get_plan(safe_plan_id)
            safe_signatures = {
                sid: tuple(sorted(cand.garment_ids))
                for sid, cand in safe_plan.assignments.items()
            }

            # 2. Generate Exploratory plan
            exp_plan_id = planner.generate_plan(
                user_id=self.user_id,
                horizon_start_date="2026-03-10",
                horizon_end_date="2026-03-12",
                strategy_mode=StrategyMode.EXPLORATORY,
                reference_safe_assignments=safe_signatures,
            )
            exp_plan = planner.get_plan(exp_plan_id)

            self.assertGreaterEqual(exp_plan.diversity_target, 1)
            self.assertGreaterEqual(exp_plan.diversity_achieved, exp_plan.diversity_target)
            print(f"PASS: Invariant 97: Exploratory strategy achieved {exp_plan.diversity_achieved} novel differences (target {exp_plan.diversity_target})")

    # -------------------------------------------------------------------------
    # Invariant 98: Safe has no artificial diversity target
    # -------------------------------------------------------------------------
    def test_98_safe_has_no_artificial_diversity_target(self):
        """Invariant 98: Safe strategy sets diversity_target = 0."""
        with self.db.transaction() as conn:
            planner = ContinuationPlanner(conn)
            conn.execute(
                "INSERT INTO WearSession(id, user_id, session_date, session_type, importance, required_roles) "
                "VALUES ('ws_98', ?, '2026-03-10', 'casual', 'NORMAL', '[\"top\", \"bottom\", \"footwear\"]');",
                (self.user_id,),
            )
            conn.execute(
                "INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color) "
                "VALUES ('g_98_t', ?, 'Top', 'top', 'shirt', 'regular', 'white'), "
                "       ('g_98_b', ?, 'Bottom', 'bottom', 'jeans', 'regular', 'blue'), "
                "       ('g_98_s', ?, 'Shoes', 'footwear', 'sneakers', 'regular', 'white');",
                (self.user_id, self.user_id, self.user_id),
            )

            plan_id = planner.generate_plan(
                user_id=self.user_id,
                horizon_start_date="2026-03-10",
                horizon_end_date="2026-03-10",
                strategy_mode=StrategyMode.SAFE,
            )
            plan = planner.get_plan(plan_id)
            self.assertEqual(plan.diversity_target, 0)
            print("PASS: Invariant 98: Safe strategy diversity_target is 0 (no artificial diversity constraint)")

    # -------------------------------------------------------------------------
    # Invariant 99: Multiple beyond-limit wears reserve separate authorizations
    # -------------------------------------------------------------------------
    def test_99_multiple_beyond_limit_wears_reserve_separate_authorizations(self):
        """Invariant 99: Each beyond-limit wear in a horizon reserves a distinct authorization keyed by (session_id, garment_id)."""
        with self.db.transaction() as conn:
            # 2 sessions
            s1, s2 = "ws_99_1", "ws_99_2"
            conn.execute(
                "INSERT INTO WearSession(id, user_id, session_date, session_type, importance, required_roles) "
                "VALUES (?, ?, '2026-03-10', 'casual', 'NORMAL', '[\"top\", \"bottom\", \"footwear\"]'), "
                "       (?, ?, '2026-03-11', 'casual', 'NORMAL', '[\"top\", \"bottom\", \"footwear\"]');",
                (s1, self.user_id, s2, self.user_id),
            )

            # Only 1 top in the wardrobe, with hard_limit = 1, current wears = 1
            # (so every wear in this horizon is beyond the limit and requires an authorization)
            gid = "g_limit_99"
            conn.execute(
                """
                INSERT INTO Garment(
                    id, user_id, name, physical_category, subcategory, cut_and_fit,
                    primary_color, hard_limit_wears, wears_since_confirmed_wash
                )
                VALUES (?, ?, 'Sole Top', 'top', 'shirt', 'regular', 'white', 1, 1);
                """,
                (gid, self.user_id),
            )
            conn.execute(
                "INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color) "
                "VALUES ('g_99_b', ?, 'Pants', 'bottom', 'jeans', 'regular', 'blue'), "
                "       ('g_99_s', ?, 'Shoes', 'footwear', 'sneakers', 'regular', 'white');",
                (self.user_id, self.user_id),
            )

            # Issue TWO distinct authorizations for this garment
            auth_1, auth_2 = "gwa_99_1", "gwa_99_2"
            conn.execute(
                """
                INSERT INTO GarmentWearAuthorization(id, garment_id, authorized_by, reason, status, authorized_at)
                VALUES (?, ?, 'owner', 'Wear 1 authorization', 'AVAILABLE', '2026-03-10T08:00:00Z'),
                       (?, ?, 'owner', 'Wear 2 authorization', 'AVAILABLE', '2026-03-10T08:01:00Z');
                """,
                (auth_1, gid, auth_2, gid),
            )

            planner = ContinuationPlanner(conn)
            plan_id = planner.generate_plan(
                user_id=self.user_id,
                horizon_start_date="2026-03-10",
                horizon_end_date="2026-03-11",
                strategy_mode=StrategyMode.SAFE,
            )

            # Check PlanWearAuthorizationReservation
            reservations = conn.execute(
                "SELECT * FROM PlanWearAuthorizationReservation WHERE plan_id = ? ORDER BY session_id;",
                (plan_id,),
            ).fetchall()

            # Must have 2 distinct reservations
            self.assertEqual(len(reservations), 2)
            auth_ids = {r["authorization_id"] for r in reservations}
            self.assertEqual(auth_ids, {auth_1, auth_2})
            self.assertEqual(reservations[0]["session_id"], s1)
            self.assertEqual(reservations[1]["session_id"], s2)
            print("PASS: Invariant 99: Multiple beyond-limit wears reserved distinct authorizations across sessions")

    # -------------------------------------------------------------------------
    # Invariant 100: Plan-persisted authorization is consumed by wear logging
    # -------------------------------------------------------------------------
    def test_100_plan_persisted_authorization_consumed_by_wear_logging(self):
        """Invariant 100: WearService consumes the exact authorization persistently reserved by the plan."""
        with self.db.transaction() as conn:
            s1 = "ws_100_1"
            conn.execute(
                "INSERT INTO WearSession(id, user_id, session_date, session_type, importance, required_roles) "
                "VALUES (?, ?, '2026-03-10', 'casual', 'NORMAL', '[\"top\", \"bottom\", \"footwear\"]');",
                (s1, self.user_id),
            )
            gid = "g_limit_100"
            conn.execute(
                """
                INSERT INTO Garment(
                    id, user_id, name, physical_category, subcategory, cut_and_fit,
                    primary_color, hard_limit_wears, wears_since_confirmed_wash
                )
                VALUES (?, ?, 'Special Shirt', 'top', 'shirt', 'regular', 'white', 1, 1);
                """,
                (gid, self.user_id),
            )
            conn.execute(
                "INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color) "
                "VALUES ('g_100_b', ?, 'Pants', 'bottom', 'jeans', 'regular', 'blue'), "
                "       ('g_100_s', ?, 'Shoes', 'footwear', 'sneakers', 'regular', 'white');",
                (self.user_id, self.user_id),
            )

            auth_id = "gwa_100_exact"
            conn.execute(
                """
                INSERT INTO GarmentWearAuthorization(id, garment_id, authorized_by, reason, status, authorized_at)
                VALUES (?, ?, 'owner', 'Exact test auth', 'AVAILABLE', '2026-03-10T08:00:00Z');
                """,
                (auth_id, gid),
            )

            planner = ContinuationPlanner(conn)
            plan_id = planner.generate_plan(
                user_id=self.user_id,
                horizon_start_date="2026-03-10",
                horizon_end_date="2026-03-10",
                strategy_mode=StrategyMode.SAFE,
            )

            # Retrieve assignment ID
            psa_row = conn.execute(
                "SELECT id FROM PlanSessionAssignment WHERE plan_id = ? AND session_id = ?;",
                (plan_id, s1),
            ).fetchone()
            psa_id = psa_row["id"]

            wear_svc = WearService(conn)
            log_id = wear_svc.log_outfit_wear(
                user_id=self.user_id,
                session_id=s1,
                garment_roles={"top": gid, "bottom": "g_100_b", "footwear": "g_100_s"},
                local_wear_date="2026-03-10",
                recommendation_plan_id=plan_id,
                plan_assignment_id=psa_id,
            )

            # Verify the exact authorization was CONSUMED
            auth_row = conn.execute("SELECT * FROM GarmentWearAuthorization WHERE id = ?;", (auth_id,)).fetchone()
            self.assertEqual(auth_row["status"], "CONSUMED")
            self.assertIsNotNone(auth_row["consumed_by_wear_event_id"])

            # Verify wear event points to this garment
            we_row = conn.execute(
                "SELECT * FROM WearEvent WHERE id = ?;",
                (auth_row["consumed_by_wear_event_id"],),
            ).fetchone()
            self.assertEqual(we_row["garment_id"], gid)
            self.assertEqual(we_row["outfit_wear_log_id"], log_id)
            print("PASS: Invariant 100: Exact plan-persisted authorization was consumed during wear logging")


if __name__ == "__main__":
    unittest.main()
