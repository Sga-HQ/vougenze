"""
Vougenze P0 and P1 Invariant Tests (Tests 43 through 80).
Mechanically verifies all frozen semantics, edge cases, and consistency constraints:
- Candidate condition & authorization decoupling (43-45)
- Authorization consumption rules (46, 64, 65)
- Byte-level hashing invariants (47, 48)
- Laundry outcomes & batch membership (49-51)
- Owner & identity boundaries (52, 53, 74)
- Duplicate intake idempotency (54)
- Preference evidence choice-set consistency (55, 56, 60)
- Candidate generation bounds, one-piece deduplication & deterministic signatures (57, 58, 59, 67, 79)
- Review threshold vs physical capacity (61)
- Within-plan transient repetition & authorization planning (62, 63)
- Frozen lexicographic ranking (66)
- Audited historical lineage reconstruction (68-70)
- Post-wear assessment & airing validity (71-73)
- Terminal retirement invariant (75)
- Session materialization & invalidation (76, 77)
- Safe/Balanced/Exploratory diversity evaluation (78)
- Simulator separation of foreign keys vs domain invariants (80)
"""
import copy
import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from core.db import Database
from core.models import (
    ConditionState,
    AvailabilityState,
    PriorityLevel,
    FallbackPolicy,
    PostWearChoice,
    LaundryItemStatus,
    CleaningScope,
    StrategyMode,
    PlanStatus,
    OutfitCandidate,
    InclusionType,
    RejectionReason,
    ReusePolicy,
    InvalidationReason,
)
from core.candidate_generator import CandidateGenerator
from core.intake_service import IntakeService, sha256_file
from core.laundry_service import LaundryService
from core.state_engine import StateEngine, InvalidStateTransitionError
from core.wear_service import WearService
from core.scorers import DeterministicScorer
from core.planner import ContinuationPlanner, PartialPlan
from core.evidence_service import PreferenceEvidenceService
from core.session_service import SessionService
from core.simulator import LifecycleSmokeSimulator


class TestP0P1Invariants(unittest.TestCase):

    def setUp(self):
        self.db = Database(":memory:")
        self.db.migrate()
        self.user_id = "u_test_1"
        self.user_id_2 = "u_test_2"

        with self.db.transaction() as conn:
            conn.execute("INSERT INTO User(id, display_name) VALUES (?, 'Alice');", (self.user_id,))
            conn.execute("INSERT INTO User(id, display_name) VALUES (?, 'Bob');", (self.user_id_2,))

    # 43. Authorization never makes NEEDS_WASH eligible
    def test_43_authorization_never_makes_needs_wash_eligible(self):
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO WearSession(id, user_id, session_date, session_type, required_roles)
                VALUES ('ws_43', ?, '2026-03-01', 'casual', '["top", "bottom"]');
                """,
                (self.user_id,),
            )
            conn.execute(
                """
                INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color, condition_state, availability_state)
                VALUES ('g_dirty_top', ?, 'Dirty Shirt', 'top', 'shirt', 'regular', 'blue', 'NEEDS_WASH', 'AVAILABLE'),
                       ('g_clean_bot', ?, 'Clean Pants', 'bottom', 'jeans', 'regular', 'blue', 'CLEAN', 'AVAILABLE');
                """,
                (self.user_id, self.user_id),
            )
            # Add an active authorization for the dirty top
            conn.execute(
                """
                INSERT INTO GarmentWearAuthorization(id, garment_id, status)
                VALUES ('gwa_43', 'g_dirty_top', 'AVAILABLE');
                """,
            )

            gen = CandidateGenerator(conn)
            candidates = gen.generate_candidates("ws_43")
            self.assertEqual(len(candidates), 0, "Dirty garment (NEEDS_WASH) must NEVER be eligible even with authorization")

    # 44. Authorization never makes AIRING or UNKNOWN_CONDITION eligible
    def test_44_authorization_never_makes_airing_or_unknown_eligible(self):
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO WearSession(id, user_id, session_date, session_type, required_roles)
                VALUES ('ws_44', ?, '2026-03-01', 'casual', '["top", "bottom"]');
                """,
                (self.user_id,),
            )
            conn.execute(
                """
                INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color, condition_state, availability_state)
                VALUES ('g_airing_top', ?, 'Airing Shirt', 'top', 'shirt', 'regular', 'blue', 'AIRING', 'AVAILABLE'),
                       ('g_unknown_top', ?, 'Unknown Shirt', 'top', 'shirt', 'regular', 'white', 'UNKNOWN_CONDITION', 'AVAILABLE'),
                       ('g_clean_bot', ?, 'Clean Pants', 'bottom', 'jeans', 'regular', 'blue', 'CLEAN', 'AVAILABLE');
                """,
                (self.user_id, self.user_id, self.user_id),
            )
            conn.execute("INSERT INTO GarmentWearAuthorization(id, garment_id, status) VALUES ('gwa_44_1', 'g_airing_top', 'AVAILABLE');")
            conn.execute("INSERT INTO GarmentWearAuthorization(id, garment_id, status) VALUES ('gwa_44_2', 'g_unknown_top', 'AVAILABLE');")

            gen = CandidateGenerator(conn)
            candidates = gen.generate_candidates("ws_44")
            self.assertEqual(len(candidates), 0, "AIRING and UNKNOWN_CONDITION garments must NEVER be eligible even with authorization")

    # 45. Hard-limit garment is excluded without authorization, included with authorization
    def test_45_hard_limit_garment_excluded_without_authorization(self):
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO WearSession(id, user_id, session_date, session_type, required_roles)
                VALUES ('ws_45', ?, '2026-03-01', 'casual', '["top", "bottom"]');
                """,
                (self.user_id,),
            )
            conn.execute(
                """
                INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color, condition_state, availability_state, hard_limit_wears, wears_since_confirmed_wash)
                VALUES ('g_hl_top', ?, 'Limit Shirt', 'top', 'shirt', 'regular', 'blue', 'WORN_REUSABLE', 'AVAILABLE', 2, 2),
                       ('g_clean_bot', ?, 'Clean Pants', 'bottom', 'jeans', 'regular', 'blue', 'CLEAN', 'AVAILABLE', NULL, 0);
                """,
                (self.user_id, self.user_id),
            )

            gen = CandidateGenerator(conn)
            cands_before = gen.generate_candidates("ws_45")
            self.assertEqual(len(cands_before), 0, "Garment at hard limit must be excluded when no authorization exists")

            # Add authorization
            conn.execute("INSERT INTO GarmentWearAuthorization(id, garment_id, status) VALUES ('gwa_45', 'g_hl_top', 'AVAILABLE');")
            cands_after = gen.generate_candidates("ws_45")
            self.assertEqual(len(cands_after), 1, "Garment at hard limit with available authorization must be eligible")
            self.assertIn("g_hl_top", cands_after[0].garment_ids)

    # 46. Exactly one authorization is consumed by authorized wear
    def test_46_exactly_one_authorization_consumed_by_authorized_wear(self):
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO WearSession(id, user_id, session_date, session_type)
                VALUES ('ws_46', ?, '2026-03-01', 'casual');
                """,
                (self.user_id,),
            )
            conn.execute(
                """
                INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color, condition_state, hard_limit_wears, wears_since_confirmed_wash)
                VALUES ('g_top_46', ?, 'Linen Shirt', 'top', 'shirt', 'regular', 'blue', 'CLEAN', 1, 1),
                       ('g_bot_46', ?, 'Jeans', 'bottom', 'jeans', 'regular', 'blue', 'CLEAN', NULL, 0);
                """,
                (self.user_id, self.user_id),
            )
            # Insert two available authorizations
            conn.execute("INSERT INTO GarmentWearAuthorization(id, garment_id, status) VALUES ('auth_1', 'g_top_46', 'AVAILABLE');")
            conn.execute("INSERT INTO GarmentWearAuthorization(id, garment_id, status) VALUES ('auth_2', 'g_top_46', 'AVAILABLE');")

            wear_svc = WearService(conn)
            wear_svc.log_outfit_wear(
                user_id=self.user_id,
                session_id="ws_46",
                garment_roles={"top": "g_top_46", "bottom": "g_bot_46"},
                local_wear_date="2026-03-01",
            )

            consumed_count = conn.execute(
                "SELECT count(*) FROM GarmentWearAuthorization WHERE garment_id = 'g_top_46' AND status = 'CONSUMED';"
            ).fetchone()[0]
            available_count = conn.execute(
                "SELECT count(*) FROM GarmentWearAuthorization WHERE garment_id = 'g_top_46' AND status = 'AVAILABLE';"
            ).fetchone()[0]

            self.assertEqual(consumed_count, 1, "Exactly one authorization must be consumed")
            self.assertEqual(available_count, 1, "Remaining authorization must stay AVAILABLE")

    # 47. Same image bytes at different paths have equal SHA-256 (sha256_file)
    def test_47_same_image_bytes_different_paths_equal_sha256(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            p1 = Path(tmpdir) / "image_a.jpg"
            p2 = Path(tmpdir) / "subfolder" / "image_b.jpg"
            p2.parent.mkdir(parents=True, exist_ok=True)

            data = b"Vougenze frozen image content bytes \x00\xff\xfe"
            p1.write_bytes(data)
            p2.write_bytes(data)

            hash1 = sha256_file(str(p1))
            hash2 = sha256_file(str(p2))

            self.assertEqual(hash1, hash2, "Identical bytes across different file paths must produce identical SHA-256")

    # 48. Different image bytes at same path have different SHA-256
    def test_48_different_image_bytes_same_path_different_sha256(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            p = Path(tmpdir) / "mutable.jpg"
            p.write_bytes(b"first version of image")
            hash1 = sha256_file(str(p))

            p.write_bytes(b"second version of image with different content")
            hash2 = sha256_file(str(p))

            self.assertNotEqual(hash1, hash2, "Different bytes at same path must produce distinct SHA-256 digests")

    # 49. RETURNED_CLEAN with wash_confirmed=0 is rejected
    def test_49_returned_clean_with_wash_confirmed_false_rejected(self):
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color, availability_state)
                VALUES ('g_laundry_49', ?, 'Shirt', 'top', 'shirt', 'regular', 'blue', 'AVAILABLE');
                """,
                (self.user_id,),
            )
            laundry_svc = LaundryService(conn)
            batch_id = laundry_svc.send_laundry_batch(
                user_id=self.user_id,
                garment_ids=["g_laundry_49"],
                sent_date="2026-03-01",
                expected_return_date="2026-03-05",
            )

            with self.assertRaises(ValueError):
                laundry_svc.record_item_return(
                    batch_id=batch_id,
                    garment_id="g_laundry_49",
                    item_status=LaundryItemStatus.RETURNED_CLEAN,
                    wash_confirmed=False,
                )

    # 50. RETURNED_UNWASHED becomes NEEDS_WASH + AVAILABLE
    def test_50_returned_unwashed_becomes_needs_wash_available_no_wash_event(self):
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color, condition_state, availability_state)
                VALUES ('g_laundry_50', ?, 'Shirt', 'top', 'shirt', 'regular', 'blue', 'NEEDS_WASH', 'AVAILABLE');
                """,
                (self.user_id,),
            )
            laundry_svc = LaundryService(conn)
            batch_id = laundry_svc.send_laundry_batch(
                user_id=self.user_id,
                garment_ids=["g_laundry_50"],
                sent_date="2026-03-01",
                expected_return_date="2026-03-05",
            )

            laundry_svc.record_item_return(
                batch_id=batch_id,
                garment_id="g_laundry_50",
                item_status=LaundryItemStatus.RETURNED_UNWASHED,
                wash_confirmed=False,
            )

            g = conn.execute("SELECT condition_state, availability_state, current_laundry_batch_id FROM Garment WHERE id = 'g_laundry_50';").fetchone()
            self.assertEqual(g["condition_state"], ConditionState.NEEDS_WASH.value)
            self.assertEqual(g["availability_state"], AvailabilityState.AVAILABLE.value)
            self.assertIsNone(g["current_laundry_batch_id"])

            wash_cnt = conn.execute("SELECT count(*) FROM WashEvent WHERE garment_id = 'g_laundry_50';").fetchone()[0]
            self.assertEqual(wash_cnt, 0, "No WashEvent should be created for RETURNED_UNWASHED")

    # 51. Item return not belonging to batch is rejected atomically
    def test_51_item_return_not_belonging_to_batch_rejected_atomically(self):
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color, availability_state)
                VALUES ('g_in_b1', ?, 'Shirt 1', 'top', 'shirt', 'regular', 'blue', 'AVAILABLE'),
                       ('g_not_in_b1', ?, 'Shirt 2', 'top', 'shirt', 'regular', 'red', 'AVAILABLE');
                """,
                (self.user_id, self.user_id),
            )
            laundry_svc = LaundryService(conn)
            batch_id = laundry_svc.send_laundry_batch(
                user_id=self.user_id,
                garment_ids=["g_in_b1"],
                sent_date="2026-03-01",
                expected_return_date="2026-03-05",
            )

            with self.assertRaises(KeyError):
                laundry_svc.record_item_return(
                    batch_id=batch_id,
                    garment_id="g_not_in_b1",
                    item_status=LaundryItemStatus.RETURNED_CLEAN,
                    wash_confirmed=True,
                )

            item_status = conn.execute(
                "SELECT item_status FROM LaundryBatchItem WHERE batch_id = ? AND garment_id = 'g_in_b1';",
                (batch_id,)
            ).fetchone()[0]
            self.assertEqual(item_status, "IN_LAUNDRY", "Batch item status must remain unchanged")

    # 52. Cross-owner session candidate generation is rejected
    def test_52_cross_owner_session_candidate_generation_rejected(self):
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO WearSession(id, user_id, session_date, session_type)
                VALUES ('ws_u1_only', ?, '2026-03-01', 'casual');
                """,
                (self.user_id,),
            )
            gen = CandidateGenerator(conn)
            with self.assertRaises(PermissionError):
                gen.generate_candidates(session_id="ws_u1_only", user_id=self.user_id_2)

    # 53. Cross-owner laundry dispatch is rejected
    def test_53_cross_owner_laundry_dispatch_rejected(self):
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color)
                VALUES ('g_u2_item', ?, 'Bob Shirt', 'top', 'shirt', 'regular', 'white');
                """,
                (self.user_id_2,),
            )
            laundry_svc = LaundryService(conn)
            with self.assertRaises(PermissionError):
                laundry_svc.send_laundry_batch(
                    user_id=self.user_id,  # User 1 attempts to dispatch User 2's garment
                    garment_ids=["g_u2_item"],
                    sent_date="2026-03-01",
                    expected_return_date="2026-03-05",
                )

    # 54. Duplicate resolution retry creates no second image
    def test_54_duplicate_resolution_retry_creates_no_second_image(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            img_path = Path(tmpdir) / "test_dup.jpg"
            img_path.write_bytes(b"duplicate_intake_bytes_xyz")
            h = sha256_file(str(img_path))

            with self.db.transaction() as conn:
                # Insert existing garment and image with matching hash
                conn.execute(
                    """
                    INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color)
                    VALUES ('g_dup_54', ?, 'Existing Shirt', 'top', 'shirt', 'regular', 'blue');
                    """,
                    (self.user_id,),
                )
                conn.execute(
                    """
                    INSERT INTO GarmentImage(id, garment_id, image_path, image_type, is_primary)
                    VALUES ('gi_orig', 'g_dup_54', 'catalogued/test_dup.jpg', 'front', 1);
                    """
                )
                conn.execute(
                    """
                    INSERT INTO GarmentImageFingerprint(id, garment_image_id, sha256)
                    VALUES ('gif_orig', 'gi_orig', ?);
                    """,
                    (h,),
                )

                intake = IntakeService(conn)
                draft_id, cand_id = intake.stage_intake_draft(
                    user_id=self.user_id,
                    temporary_image_path=str(img_path),
                )
                self.assertIsNotNone(cand_id, "Duplicate candidate must be created for exact hash match")

                # First resolution as SAME_GARMENT
                intake.resolve_as_same_garment(candidate_id=cand_id)

                img_count_1 = conn.execute(
                    "SELECT count(*) FROM GarmentImage WHERE garment_id = 'g_dup_54';"
                ).fetchone()[0]
                self.assertEqual(img_count_1, 2)

                # Second resolution attempt must raise and NOT insert another image
                with self.assertRaises((KeyError, ValueError)):
                    intake.resolve_as_same_garment(candidate_id=cand_id)

                img_count_2 = conn.execute(
                    "SELECT count(*) FROM GarmentImage WHERE garment_id = 'g_dup_54';"
                ).fetchone()[0]
                self.assertEqual(img_count_2, 2, "Second duplicate resolution attempt must not create additional images")

    # 55. Evidence event with duplicate candidate is rejected atomically
    def test_55_evidence_event_with_duplicate_candidate_rejected_atomically(self):
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color)
                VALUES ('g_top_55', ?, 'T', 'top', 'shirt', 'regular', 'white'),
                       ('g_bot_55', ?, 'B', 'bottom', 'pants', 'regular', 'blue');
                """,
                (self.user_id, self.user_id),
            )
            conn.execute(
                """
                INSERT INTO WearSession(id, user_id, session_date, session_type)
                VALUES ('ws_55', ?, '2026-03-01', 'casual');
                """,
                (self.user_id,),
            )
            evidence_svc = PreferenceEvidenceService(conn)

            selected = OutfitCandidate(
                id="cand_sel",
                garment_ids=["g_top_55", "g_bot_55"],
                roles={"top": "g_top_55", "bottom": "g_bot_55"},
            )
            # Alternatives list contains duplicate candidate
            alt_dup = OutfitCandidate(
                id="cand_alt",
                garment_ids=["g_top_55", "g_bot_55"],
                roles={"top": "g_top_55", "bottom": "g_bot_55"},
            )
            alternatives = [
                {"candidate": alt_dup, "was_feasible": True, "inclusion_type": InclusionType.PRESENTED.value},
                {"candidate": alt_dup, "was_feasible": True, "inclusion_type": InclusionType.PRESENTED.value},  # Duplicate candidate in choice set
            ]

            with self.assertRaises(ValueError):
                evidence_svc.record_selection_evidence(
                    user_id=self.user_id,
                    session_id="ws_55",
                    occasion_context="casual",
                    selected_candidate=selected,
                    alternatives=alternatives,
                )

            event_cnt = conn.execute("SELECT count(*) FROM PreferenceEvidence;").fetchone()[0]
            self.assertEqual(event_cnt, 0, "Rejected evidence capture must roll back atomically")

    # 56. Contradictory inclusion_type/feasibility is rejected
    def test_56_contradictory_inclusion_type_feasibility_rejected_by_check(self):
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color)
                VALUES ('g_top_56', ?, 'T', 'top', 'shirt', 'regular', 'white');
                """,
                (self.user_id,),
            )
            conn.execute(
                """
                INSERT INTO WearSession(id, user_id, session_date, session_type)
                VALUES ('ws_56', ?, '2026-03-01', 'casual');
                """,
                (self.user_id,),
            )
            conn.execute(
                """
                INSERT INTO OutfitCandidateSnapshot(id, user_id, session_id, generator_version, feature_schema_version)
                VALUES ('ocs_56', ?, 'ws_56', '1', '1');
                """,
                (self.user_id,),
            )
            conn.execute(
                """
                INSERT INTO PreferenceEvidence(id, user_id, session_id, evidence_type, occasion_context, selected_candidate_id)
                VALUES ('pe_56', ?, 'ws_56', 'selection', 'casual', 'ocs_56');
                """,
                (self.user_id,),
            )

            # INFEASIBLE_REFERENCE with was_feasible = 1 violates table CHECK constraint
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    """
                    INSERT INTO PreferenceEvidenceAlternative(
                        evidence_id, candidate_id, inclusion_type, presentation_order, was_feasible, was_selected
                    ) VALUES ('pe_56', 'ocs_56', 'INFEASIBLE_REFERENCE', NULL, 1, 0);
                    """
                )

    # 57. One-piece cannot duplicate itself in a secondary role accidentally
    def test_57_one_piece_cannot_duplicate_itself_in_secondary_role(self):
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO WearSession(id, user_id, session_date, session_type, required_roles)
                VALUES ('ws_57', ?, '2026-03-01', 'casual', '["top", "bottom", "footwear"]');
                """,
                (self.user_id,),
            )
            conn.execute(
                """
                INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color)
                VALUES ('g_dress_57', ?, 'Sundress', 'one_piece', 'dress', 'regular', 'floral'),
                       ('g_shoes_57', ?, 'Sandals', 'footwear', 'sandal', 'regular', 'brown');
                """,
                (self.user_id, self.user_id),
            )

            gen = CandidateGenerator(conn)
            candidates = gen.generate_candidates("ws_57")
            self.assertGreater(len(candidates), 0)

            for cand in candidates:
                self.assertEqual(len(set(cand.garment_ids)), 2, "Candidate must only contain unique physical garments")
                self.assertEqual(cand.roles["top"], "g_dress_57")
                self.assertEqual(cand.roles["bottom"], "g_dress_57")
                self.assertEqual(cand.roles["footwear"], "g_shoes_57")

    # 58. Fallback search minimizes fallback item count
    def test_58_fallback_search_minimizes_fallback_item_count(self):
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO WearSession(id, user_id, session_date, session_type, required_roles)
                VALUES ('ws_58', ?, '2026-03-01', 'casual', '["top", "bottom"]');
                """,
                (self.user_id,),
            )
            conn.execute(
                """
                INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color, priority_level)
                VALUES ('g_top_norm', ?, 'Top Norm', 'top', 'shirt', 'regular', 'white', 'NORMAL'),
                       ('g_top_fall', ?, 'Top Fallback', 'top', 'shirt', 'regular', 'grey', 'FALLBACK_ONLY'),
                       ('g_bot_norm', ?, 'Bot Norm', 'bottom', 'pants', 'regular', 'blue', 'NORMAL'),
                       ('g_bot_fall', ?, 'Bot Fallback', 'bottom', 'pants', 'regular', 'grey', 'FALLBACK_ONLY');
                """,
                (self.user_id, self.user_id, self.user_id, self.user_id),
            )

            gen = CandidateGenerator(conn)
            cands = gen.generate_candidates("ws_58", fallback_policy=FallbackPolicy.ALLOW)

            # Assert candidates are sorted by fallback_count ascending
            self.assertGreater(len(cands), 0)
            fallback_counts = [c.fallback_count for c in cands]
            self.assertEqual(fallback_counts, sorted(fallback_counts), "Candidates must be ordered by fallback_count ascending")
            self.assertEqual(fallback_counts[0], 0, "First candidate must have 0 fallback items")
            self.assertEqual(fallback_counts[-1], 2, "Last candidate may use up to 2 fallback items")

    # 59. Same candidate state produces stable candidate identity
    def test_59_same_candidate_state_produces_stable_identity(self):
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO WearSession(id, user_id, session_date, session_type, required_roles)
                VALUES ('ws_59', ?, '2026-03-01', 'casual', '["top", "bottom"]');
                """,
                (self.user_id,),
            )
            conn.execute(
                """
                INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color)
                VALUES ('g_t_59', ?, 'T', 'top', 'shirt', 'regular', 'white'),
                       ('g_b_59', ?, 'B', 'bottom', 'pants', 'regular', 'blue');
                """,
                (self.user_id, self.user_id),
            )

            gen = CandidateGenerator(conn)
            run1 = gen.generate_candidates("ws_59")
            run2 = gen.generate_candidates("ws_59")

            self.assertEqual(len(run1), 1)
            self.assertEqual(len(run2), 1)
            self.assertEqual(run1[0].id, run2[0].id, "Deterministic candidate hashing must produce identical IDs across runs")

    # 60. Failed evidence capture leaves no partial header or alternatives
    def test_60_failed_evidence_capture_leaves_no_partial_records(self):
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color)
                VALUES ('g_t_60', ?, 'T', 'top', 'shirt', 'regular', 'white'),
                       ('g_b_60', ?, 'B', 'bottom', 'pants', 'regular', 'blue');
                """,
                (self.user_id, self.user_id),
            )
            conn.execute(
                """
                INSERT INTO WearSession(id, user_id, session_date, session_type)
                VALUES ('ws_60', ?, '2026-03-01', 'casual');
                """,
                (self.user_id,),
            )
            evidence_svc = PreferenceEvidenceService(conn)

            selected = OutfitCandidate(id="cand_sel_60", garment_ids=["g_t_60", "g_b_60"], roles={"top": "g_t_60", "bottom": "g_b_60"})
            alt1 = OutfitCandidate(id="cand_alt1_60", garment_ids=["g_t_60", "g_b_60"], roles={"top": "g_t_60", "bottom": "g_b_60"})
            # alt2 has duplicate candidate of selected, which triggers preflight check and rolls back
            alternatives = [
                {"candidate": alt1, "was_feasible": True, "inclusion_type": InclusionType.PRESENTED.value},
                {"candidate": selected, "was_feasible": True, "inclusion_type": InclusionType.PRESENTED.value},
            ]

            with self.assertRaises(ValueError):
                evidence_svc.record_selection_evidence(
                    user_id=self.user_id,
                    session_id="ws_60",
                    occasion_context="casual",
                    selected_candidate=selected,
                    alternatives=alternatives,
                )

            headers = conn.execute("SELECT count(*) FROM PreferenceEvidence;").fetchone()[0]
            alts = conn.execute("SELECT count(*) FROM PreferenceEvidenceAlternative;").fetchone()[0]
            snapshots = conn.execute("SELECT count(*) FROM OutfitCandidateSnapshot;").fetchone()[0]

            self.assertEqual(headers, 0)
            self.assertEqual(alts, 0)
            self.assertEqual(snapshots, 0)

    # 61. Review threshold is never used as physical capacity
    def test_61_review_threshold_never_used_as_physical_capacity(self):
        policy = ReusePolicy(
            research_review_after_wears=2,
            owner_review_after_wears=3,
            hard_limit_wears=10,
        )
        self.assertTrue(policy.review_due(4), "Review prompt must be due after 4 wears")
        self.assertFalse(policy.hard_limit_reached(4), "Hard limit must NOT be reached after 4 wears (limit is 10)")

        # Candidate generator should still allow this garment
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO WearSession(id, user_id, session_date, session_type, required_roles)
                VALUES ('ws_61', ?, '2026-03-01', 'casual', '["top", "bottom"]');
                """,
                (self.user_id,),
            )
            conn.execute(
                """
                INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color, condition_state, availability_state, owner_review_after_wears, hard_limit_wears, wears_since_confirmed_wash)
                VALUES ('g_61', ?, 'Shirt', 'top', 'shirt', 'regular', 'blue', 'WORN_REUSABLE', 'AVAILABLE', 2, 10, 4),
                       ('g_pants_61', ?, 'Pants', 'bottom', 'jeans', 'regular', 'blue', 'CLEAN', 'AVAILABLE', NULL, NULL, 0);
                """,
                (self.user_id, self.user_id),
            )
            gen = CandidateGenerator(conn)
            cands = gen.generate_candidates("ws_61")
            self.assertEqual(len(cands), 1, "Garment over review threshold but under hard limit must be eligible without authorization")

    # 62. Within-plan repetition affects later assignments
    def test_62_within_plan_repetition_affects_later_assignments(self):
        with self.db.transaction() as conn:
            # 2 sessions: day 1 and day 2
            conn.execute(
                """
                INSERT INTO WearSession(id, user_id, session_date, session_type, required_roles)
                VALUES ('ws_d1', ?, '2026-03-01', 'casual', '["top", "bottom"]'),
                       ('ws_d2', ?, '2026-03-02', 'casual', '["top", "bottom"]');
                """,
                (self.user_id, self.user_id),
            )
            # Top A slightly higher like rating (9) vs Top B (8)
            conn.execute(
                """
                INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color, like_rating)
                VALUES ('g_top_a', ?, 'Top A', 'top', 'shirt', 'regular', 'white', 9),
                       ('g_top_b', ?, 'Top B', 'top', 'shirt', 'regular', 'blue', 8),
                       ('g_bot_a', ?, 'Bot A', 'bottom', 'pants', 'regular', 'black', 9),
                       ('g_bot_b', ?, 'Bot B', 'bottom', 'pants', 'regular', 'navy', 8);
                """,
                (self.user_id, self.user_id, self.user_id, self.user_id),
            )

            planner = ContinuationPlanner(conn)
            plan_id = planner.generate_plan(
                user_id=self.user_id,
                horizon_start_date="2026-03-01",
                horizon_end_date="2026-03-02",
                strategy_mode=StrategyMode.BALANCED,
            )

            assignments = conn.execute(
                """
                SELECT psa.session_id, pai.garment_id
                FROM PlanSessionAssignment psa
                JOIN PlanAssignmentItem pai ON psa.id = pai.assignment_id
                WHERE psa.plan_id = ? AND pai.outfit_role = 'top'
                ORDER BY psa.session_id ASC;
                """,
                (plan_id,)
            ).fetchall()

            top_d1 = assignments[0]["garment_id"]
            top_d2 = assignments[1]["garment_id"]

            self.assertNotEqual(top_d1, top_d2, "Within-plan repetition penalty must cause day 2 to rotate to a different top")

    # 63. Planner recognizes a valid hard-limit authorization
    def test_63_planner_recognizes_valid_hard_limit_authorization(self):
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO WearSession(id, user_id, session_date, session_type, required_roles)
                VALUES ('ws_63', ?, '2026-03-01', 'casual', '["top", "bottom"]');
                """,
                (self.user_id,),
            )
            # Only 1 top available, but at hard limit
            conn.execute(
                """
                INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color, condition_state, hard_limit_wears, wears_since_confirmed_wash)
                VALUES ('g_only_top', ?, 'Shirt', 'top', 'shirt', 'regular', 'blue', 'WORN_REUSABLE', 2, 2),
                       ('g_only_bot', ?, 'Pants', 'bottom', 'pants', 'regular', 'blue', 'CLEAN', NULL, 0);
                """,
                (self.user_id, self.user_id),
            )
            # Add authorization
            conn.execute("INSERT INTO GarmentWearAuthorization(id, garment_id, status) VALUES ('auth_63', 'g_only_top', 'AVAILABLE');")

            planner = ContinuationPlanner(conn)
            plan_id = planner.generate_plan(
                user_id=self.user_id,
                horizon_start_date="2026-03-01",
                horizon_end_date="2026-03-01",
                strategy_mode=StrategyMode.SAFE,
            )

            assigned = conn.execute(
                "SELECT count(*) FROM PlanSessionAssignment WHERE plan_id = ? AND session_id = 'ws_63';", (plan_id,)
            ).fetchone()[0]
            self.assertEqual(assigned, 1, "Planner must successfully assign garment that has an available authorization")

    # 64. Below-limit wear does not consume authorization
    def test_64_below_limit_wear_does_not_consume_authorization(self):
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO WearSession(id, user_id, session_date, session_type)
                VALUES ('ws_64', ?, '2026-03-01', 'casual');
                """,
                (self.user_id,),
            )
            # Garment has limit 5, current wears 1
            conn.execute(
                """
                INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color, hard_limit_wears, wears_since_confirmed_wash)
                VALUES ('g_top_64', ?, 'T', 'top', 'shirt', 'regular', 'white', 5, 1),
                       ('g_bot_64', ?, 'B', 'bottom', 'pants', 'regular', 'blue', NULL, 0);
                """,
                (self.user_id, self.user_id),
            )
            conn.execute("INSERT INTO GarmentWearAuthorization(id, garment_id, status) VALUES ('auth_64', 'g_top_64', 'AVAILABLE');")

            wear_svc = WearService(conn)
            wear_svc.log_outfit_wear(
                user_id=self.user_id,
                session_id="ws_64",
                garment_roles={"top": "g_top_64", "bottom": "g_bot_64"},
                local_wear_date="2026-03-01",
            )

            status = conn.execute("SELECT status FROM GarmentWearAuthorization WHERE id = 'auth_64';").fetchone()[0]
            self.assertEqual(status, "AVAILABLE", "Below-limit wear must NEVER consume authorization")

    # 65. Authorized wear consumes exactly the reserved authorization
    def test_65_authorized_wear_consumes_reserved_authorization(self):
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO WearSession(id, user_id, session_date, session_type)
                VALUES ('ws_65', ?, '2026-03-01', 'casual');
                """,
                (self.user_id,),
            )
            conn.execute(
                """
                INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color, hard_limit_wears, wears_since_confirmed_wash)
                VALUES ('g_top_65', ?, 'T', 'top', 'shirt', 'regular', 'white', 2, 2),
                       ('g_bot_65', ?, 'B', 'bottom', 'pants', 'regular', 'blue', NULL, 0);
                """,
                (self.user_id, self.user_id),
            )
            conn.execute("INSERT INTO GarmentWearAuthorization(id, garment_id, status, authorized_at) VALUES ('auth_A', 'g_top_65', 'AVAILABLE', '2026-01-01');")
            conn.execute("INSERT INTO GarmentWearAuthorization(id, garment_id, status, authorized_at) VALUES ('auth_B', 'g_top_65', 'AVAILABLE', '2026-01-02');")

            wear_svc = WearService(conn)
            wear_svc.log_outfit_wear(
                user_id=self.user_id,
                session_id="ws_65",
                garment_roles={"top": "g_top_65", "bottom": "g_bot_65"},
                local_wear_date="2026-03-01",
                reserved_authorizations={"g_top_65": "auth_B"},
            )

            auth_a_status = conn.execute("SELECT status FROM GarmentWearAuthorization WHERE id = 'auth_A';").fetchone()[0]
            auth_b_status = conn.execute("SELECT status FROM GarmentWearAuthorization WHERE id = 'auth_B';").fetchone()[0]

            self.assertEqual(auth_a_status, "AVAILABLE")
            self.assertEqual(auth_b_status, "CONSUMED", "Explicitly reserved authorization auth_B must be consumed")

    # 66. Lexicographic ranking prioritizes uncovered LOW before fallback use
    def test_66_lexicographic_ranking_prioritizes_uncovered_low_before_fallback(self):
        # p1: 0 uncovered low, 1 fallback item
        p1 = PartialPlan(
            uncovered_high=0,
            uncovered_normal=0,
            uncovered_low=0,
            uncertain_dependencies=0,
            fallback_count=1,
            min_high_quality=0.8,
            total_quality=0.8,
            total_repetition=0.1,
        )
        # p2: 1 uncovered low, 0 fallback items
        p2 = PartialPlan(
            uncovered_high=0,
            uncovered_normal=0,
            uncovered_low=1,
            uncertain_dependencies=0,
            fallback_count=0,
            min_high_quality=0.8,
            total_quality=0.8,
            total_repetition=0.1,
        )

        key1 = ContinuationPlanner.plan_ranking_key(p1)
        key2 = ContinuationPlanner.plan_ranking_key(p2)

        self.assertLess(key1, key2, "Plan with 0 uncovered low and 1 fallback must rank higher (smaller key) than plan with 1 uncovered low")

    # 67. One-piece garment is counted once for preference and repetition
    def test_67_one_piece_counted_once_for_preference_and_repetition(self):
        with self.db.connection() as conn:
            scorer = DeterministicScorer(conn)
        garment_dict = {
            "id": "g_dress",
            "physical_category": "one_piece",
            "like_rating": 8,
            "priority_level": "NORMAL",
            "formality": 3,
            "context_priority_overrides": "{}",
        }
        session_dict = {"session_type": "casual", "session_date": "2026-03-01"}
        # compute_preference
        pref_score = scorer.compute_preference({"top": garment_dict, "bottom": garment_dict}, session_dict)
        self.assertAlmostEqual(pref_score, 0.8, places=3, msg="One-piece like rating must be evaluated once without duplication")

        # Repetition penalty calculation
        recent_history = [
            {"session_date": "2026-02-28", "session_type": "casual", "garment_ids": ["g_dress"]}
        ]
        rep_pen = scorer.compute_repetition_penalty(
            garments_by_role={"top": garment_dict, "bottom": garment_dict},
            session=session_dict,
            recent_wear_history=[],
            feasible_substitute_counts={"top": 5, "bottom": 5},
            simulated_wear_history=recent_history,
        )
        self.assertGreater(rep_pen, 0.0)
        self.assertLessEqual(rep_pen, 1.0)

    # 68. Active correction replays post-wear assessments correctly
    def test_68_active_correction_replays_post_wear_assessments(self):
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color, condition_state, wears_since_confirmed_wash)
                VALUES ('g_shirt_68', ?, 'Shirt', 'top', 'shirt', 'regular', 'blue', 'CLEAN', 0),
                       ('g_pants_68', ?, 'Pants', 'bottom', 'pants', 'regular', 'blue', 'CLEAN', 0);
                """,
                (self.user_id, self.user_id),
            )
            conn.execute("INSERT INTO WearSession(id, user_id, session_date, session_type) VALUES ('ws_1', ?, '2026-03-01', 'casual');", (self.user_id,))
            conn.execute("INSERT INTO WearSession(id, user_id, session_date, session_type) VALUES ('ws_2', ?, '2026-03-02', 'casual');", (self.user_id,))

            wear_svc = WearService(conn)
            state_eng = StateEngine(conn)

            # Wear 1
            log_id_1 = wear_svc.log_outfit_wear(
                user_id=self.user_id, session_id="ws_1",
                garment_roles={"top": "g_shirt_68", "bottom": "g_pants_68"},
                local_wear_date="2026-03-01",
            )
            we_1 = conn.execute("SELECT id FROM WearEvent WHERE outfit_wear_log_id = ? AND garment_id = 'g_shirt_68';", (log_id_1,)).fetchone()[0]
            state_eng.record_post_wear_assessment(
                wear_event_id=we_1, garment_id="g_shirt_68", choice=PostWearChoice.READY_TO_REUSE,
                assessed_at="2026-03-01T20:00:00Z"
            )
            # Reset pants to clean so it is eligible for wear 2
            conn.execute("UPDATE Garment SET condition_state = 'CLEAN' WHERE id = 'g_pants_68';")

            # Wear 2
            log_id_2 = wear_svc.log_outfit_wear(
                user_id=self.user_id, session_id="ws_2",
                garment_roles={"top": "g_shirt_68", "bottom": "g_pants_68"},
                local_wear_date="2026-03-02",
            )
            we_2 = conn.execute("SELECT id FROM WearEvent WHERE outfit_wear_log_id = ? AND garment_id = 'g_shirt_68';", (log_id_2,)).fetchone()[0]
            state_eng.record_post_wear_assessment(
                wear_event_id=we_2, garment_id="g_shirt_68", choice=PostWearChoice.NEEDS_WASH,
                assessed_at="2026-03-02T20:00:00Z"
            )

            cond_after_w2 = conn.execute("SELECT condition_state FROM Garment WHERE id = 'g_shirt_68';").fetchone()[0]
            self.assertEqual(cond_after_w2, "NEEDS_WASH")

            # Void Wear 2
            wear_svc.void_outfit_wear(
                outfit_wear_log_id=log_id_2,
                void_reason="User changed their mind, never wore outfit on day 2",
            )

            cond_after_void = conn.execute("SELECT condition_state, wears_since_confirmed_wash FROM Garment WHERE id = 'g_shirt_68';").fetchone()
            self.assertEqual(cond_after_void["condition_state"], "WORN_REUSABLE", "Condition must revert to operative assessment of Wear 1")
            self.assertEqual(cond_after_void["wears_since_confirmed_wash"], 1)

    # 69. Correction recomputes last_worn_at
    def test_69_correction_recomputes_last_worn_at(self):
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color)
                VALUES ('g_shirt_69', ?, 'Shirt', 'top', 'shirt', 'regular', 'blue'),
                       ('g_pants_69', ?, 'Pants', 'bottom', 'pants', 'regular', 'blue');
                """,
                (self.user_id, self.user_id),
            )
            conn.execute("INSERT INTO WearSession(id, user_id, session_date, session_type) VALUES ('ws_69_1', ?, '2026-03-01', 'casual');", (self.user_id,))
            conn.execute("INSERT INTO WearSession(id, user_id, session_date, session_type) VALUES ('ws_69_2', ?, '2026-03-05', 'casual');", (self.user_id,))

            wear_svc = WearService(conn)
            state_eng = StateEngine(conn)

            log_1 = wear_svc.log_outfit_wear(self.user_id, "ws_69_1", {"top": "g_shirt_69", "bottom": "g_pants_69"}, local_wear_date="2026-03-01")
            we_1 = conn.execute("SELECT id FROM WearEvent WHERE outfit_wear_log_id = ? AND garment_id = 'g_shirt_69';", (log_1,)).fetchone()[0]
            state_eng.record_post_wear_assessment(wear_event_id=we_1, garment_id="g_shirt_69", choice=PostWearChoice.READY_TO_REUSE, assessed_at="2026-03-01T20:00:00Z")
            conn.execute("UPDATE Garment SET condition_state = 'CLEAN' WHERE id = 'g_pants_69';")

            log_2 = wear_svc.log_outfit_wear(self.user_id, "ws_69_2", {"top": "g_shirt_69", "bottom": "g_pants_69"}, local_wear_date="2026-03-05")
            we_2 = conn.execute("SELECT id FROM WearEvent WHERE outfit_wear_log_id = ? AND garment_id = 'g_shirt_69';", (log_2,)).fetchone()[0]
            we_1_worn_at = conn.execute("SELECT worn_at FROM WearEvent WHERE id = ?;", (we_1,)).fetchone()[0]
            we_2_worn_at = conn.execute("SELECT worn_at FROM WearEvent WHERE id = ?;", (we_2,)).fetchone()[0]

            last_worn_before = conn.execute("SELECT last_worn_at FROM Garment WHERE id = 'g_shirt_69';").fetchone()[0]
            self.assertEqual(last_worn_before, we_2_worn_at)

            wear_svc.void_outfit_wear(
                outfit_wear_log_id=log_2,
                void_reason="Void wear 2",
            )

            last_worn_after = conn.execute("SELECT last_worn_at FROM Garment WHERE id = 'g_shirt_69';").fetchone()[0]
            self.assertEqual(last_worn_after, we_1_worn_at, "last_worn_at must be recomputed to worn_at of Wear 1")

    # 70. Voided wear-event assessment no longer affects current condition
    def test_70_voided_wear_event_assessment_no_longer_affects_condition(self):
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color)
                VALUES ('g_shirt_70', ?, 'Shirt', 'top', 'shirt', 'regular', 'blue'),
                       ('g_pants_70', ?, 'Pants', 'bottom', 'pants', 'regular', 'blue');
                """,
                (self.user_id, self.user_id),
            )
            conn.execute("INSERT INTO WearSession(id, user_id, session_date, session_type) VALUES ('ws_70', ?, '2026-03-01', 'casual');", (self.user_id,))

            wear_svc = WearService(conn)
            state_eng = StateEngine(conn)

            log_id = wear_svc.log_outfit_wear(self.user_id, "ws_70", {"top": "g_shirt_70", "bottom": "g_pants_70"}, local_wear_date="2026-03-01")
            we_id = conn.execute("SELECT id FROM WearEvent WHERE outfit_wear_log_id = ? AND garment_id = 'g_shirt_70';", (log_id,)).fetchone()[0]
            state_eng.record_post_wear_assessment(wear_event_id=we_id, garment_id="g_shirt_70", choice=PostWearChoice.NEEDS_WASH, assessed_at="2026-03-01T20:00:00Z")

            wear_svc.void_outfit_wear(
                outfit_wear_log_id=log_id,
                void_reason="Void single wear",
            )

            row = conn.execute("SELECT condition_state, wears_since_confirmed_wash FROM Garment WHERE id = 'g_shirt_70';").fetchone()
            self.assertEqual(row["condition_state"], "CLEAN", "With no active wear events since last wash, condition must revert to clean")
            self.assertEqual(row["wears_since_confirmed_wash"], 0)

    # 71. Post-wear assessment for the wrong garment is rejected
    def test_71_post_wear_assessment_wrong_garment_rejected(self):
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color)
                VALUES ('g_shirt_71', ?, 'Shirt', 'top', 'shirt', 'regular', 'blue'),
                       ('g_pants_71', ?, 'Pants', 'bottom', 'pants', 'regular', 'blue');
                """,
                (self.user_id, self.user_id),
            )
            conn.execute("INSERT INTO WearSession(id, user_id, session_date, session_type) VALUES ('ws_71', ?, '2026-03-01', 'casual');", (self.user_id,))

            wear_svc = WearService(conn)
            state_eng = StateEngine(conn)

            log_id = wear_svc.log_outfit_wear(self.user_id, "ws_71", {"top": "g_shirt_71", "bottom": "g_pants_71"}, local_wear_date="2026-03-01")
            we_shirt = conn.execute("SELECT id FROM WearEvent WHERE outfit_wear_log_id = ? AND garment_id = 'g_shirt_71';", (log_id,)).fetchone()[0]

            with self.assertRaises(ValueError):
                state_eng.record_post_wear_assessment(
                    wear_event_id=we_shirt,
                    garment_id="g_pants_71",  # Mismatched garment!
                    choice=PostWearChoice.READY_TO_REUSE,
                )

    # 72. Assessment for voided wear event is rejected
    def test_72_assessment_for_voided_wear_event_rejected(self):
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color)
                VALUES ('g_shirt_72', ?, 'Shirt', 'top', 'shirt', 'regular', 'blue'),
                       ('g_pants_72', ?, 'Pants', 'bottom', 'pants', 'regular', 'blue');
                """,
                (self.user_id, self.user_id),
            )
            conn.execute("INSERT INTO WearSession(id, user_id, session_date, session_type) VALUES ('ws_72', ?, '2026-03-01', 'casual');", (self.user_id,))

            wear_svc = WearService(conn)
            state_eng = StateEngine(conn)

            log_id = wear_svc.log_outfit_wear(self.user_id, "ws_72", {"top": "g_shirt_72", "bottom": "g_pants_72"}, local_wear_date="2026-03-01")
            we_shirt = conn.execute("SELECT id FROM WearEvent WHERE outfit_wear_log_id = ? AND garment_id = 'g_shirt_72';", (log_id,)).fetchone()[0]

            # Mark wear event voided directly
            conn.execute("UPDATE WearEvent SET is_voided = 1 WHERE id = ?;", (we_shirt,))

            with self.assertRaises(ValueError):
                state_eng.record_post_wear_assessment(
                    wear_event_id=we_shirt,
                    garment_id="g_shirt_72",
                    choice=PostWearChoice.READY_TO_REUSE,
                )

    # 73. Airing due time derives from assessed_at
    def test_73_airing_due_time_derives_from_assessed_at(self):
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color)
                VALUES ('g_shirt_73', ?, 'Shirt', 'top', 'shirt', 'regular', 'blue'),
                       ('g_pants_73', ?, 'Pants', 'bottom', 'pants', 'regular', 'blue');
                """,
                (self.user_id, self.user_id),
            )
            conn.execute("INSERT INTO WearSession(id, user_id, session_date, session_type) VALUES ('ws_73', ?, '2026-03-01', 'casual');", (self.user_id,))

            wear_svc = WearService(conn)
            state_eng = StateEngine(conn)

            log_id = wear_svc.log_outfit_wear(self.user_id, "ws_73", {"top": "g_shirt_73", "bottom": "g_pants_73"}, local_wear_date="2026-03-01")
            we_shirt = conn.execute("SELECT id FROM WearEvent WHERE outfit_wear_log_id = ? AND garment_id = 'g_shirt_73';", (log_id,)).fetchone()[0]

            assessed_at = "2026-01-01T10:00:00Z"
            state_eng.record_post_wear_assessment(
                wear_event_id=we_shirt,
                garment_id="g_shirt_73",
                choice=PostWearChoice.NEEDS_AIRING,
                assessed_at=assessed_at,
                airing_hours=48,
            )

            due_at = conn.execute(
                "SELECT airing_review_due_at FROM Garment WHERE id = 'g_shirt_73';"
            ).fetchone()[0]

            self.assertEqual(due_at, "2026-01-03T10:00:00Z", "airing_due_at must be exactly assessed_at + 48 hours")

    # 74. Wash event cannot use another owner's user_id
    def test_74_wash_event_cannot_use_another_owner_user_id(self):
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color)
                VALUES ('g_shirt_74', ?, 'Shirt', 'top', 'shirt', 'regular', 'blue');
                """,
                (self.user_id,),
            )
            state_eng = StateEngine(conn)
            with self.assertRaises(PermissionError):
                state_eng.record_wash_event(
                    garment_id="g_shirt_74",
                    user_id=self.user_id_2,  # User 2 tries to record wash on User 1's garment
                    cleaning_scope=CleaningScope.FULL_CLEAN,
                    method="home_machine",
                )

    # 75. RETIRED -> AVAILABLE is rejected
    def test_75_retired_to_available_transition_rejected(self):
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color, availability_state)
                VALUES ('g_retired_75', ?, 'Shirt', 'top', 'shirt', 'regular', 'blue', 'RETIRED');
                """,
                (self.user_id,),
            )
            state_eng = StateEngine(conn)
            with self.assertRaises(InvalidStateTransitionError):
                state_eng.transition_availability(
                    garment_id="g_retired_75",
                    to_state=AvailabilityState.AVAILABLE,
                    trigger_event="manual_unretire",
                )

    # 76. Session materialization invalidates overlapping plans
    def test_76_session_materialization_invalidates_overlapping_plans(self):
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO RecommendationPlan(id, user_id, strategy_mode, status, horizon_start_date, horizon_end_date)
                VALUES ('plan_76', ?, 'SAFE', 'PROPOSED', '2026-03-01', '2026-03-07');
                """,
                (self.user_id,),
            )
            conn.execute(
                """
                INSERT INTO WearSessionTemplate(id, user_id, template_name, effective_from)
                VALUES ('tmpl_76', ?, 'Routine', '2026-01-01');
                """,
                (self.user_id,),
            )
            conn.execute(
                """
                INSERT INTO WearSessionTemplateDay(id, template_id, day_of_week, session_type)
                VALUES ('td_76', 'tmpl_76', 0, 'casual');
                """
            )
            session_svc = SessionService(conn)
            session_svc.materialize_sessions_for_horizon(
                user_id=self.user_id,
                start_date="2026-03-01",
                end_date="2026-03-07",
            )

            plan = conn.execute("SELECT status, invalidation_reason FROM RecommendationPlan WHERE id = 'plan_76';").fetchone()
            self.assertEqual(plan["status"], "SUPERSEDED", "Materializing new sessions must invalidate overlapping plans")
            self.assertEqual(plan["invalidation_reason"], InvalidationReason.EVENT_ADDED_OR_MODIFIED.value)

    # 77. Template validity is evaluated per local date
    def test_77_template_validity_evaluated_per_local_date(self):
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO WearSessionTemplate(id, user_id, template_name, effective_from, effective_until)
                VALUES ('tmpl_a', ?, 'Routine A', '2026-03-01', '2026-03-03'),
                       ('tmpl_b', ?, 'Routine B', '2026-03-04', '2026-03-10');
                """,
                (self.user_id, self.user_id),
            )
            # Monday is day_of_week = 0
            conn.execute(
                """
                INSERT INTO WearSessionTemplateDay(id, template_id, day_of_week, session_type)
                VALUES ('td_a', 'tmpl_a', 0, 'casual'),
                       ('td_b', 'tmpl_b', 0, 'work');
                """
            )
            session_svc = SessionService(conn)
            # Materialize for 2026-03-02 (Monday) and 2026-03-09 (Monday)
            session_svc.materialize_sessions_for_horizon(
                user_id=self.user_id,
                start_date="2026-03-01",
                end_date="2026-03-10",
            )

            s_mar2 = conn.execute("SELECT generated_from_template_id, session_type FROM WearSession WHERE user_id = ? AND session_date = '2026-03-02';", (self.user_id,)).fetchone()
            s_mar9 = conn.execute("SELECT generated_from_template_id, session_type FROM WearSession WHERE user_id = ? AND session_date = '2026-03-09';", (self.user_id,)).fetchone()

            self.assertEqual(s_mar2["generated_from_template_id"], "tmpl_a")
            self.assertEqual(s_mar2["session_type"], "casual")
            self.assertEqual(s_mar9["generated_from_template_id"], "tmpl_b")
            self.assertEqual(s_mar9["session_type"], "work")

    # 78. Safe/Balanced/Exploratory diversity is measured between plans
    def test_78_strategy_diversity_measured_relative_to_safe(self):
        with self.db.transaction() as conn:
            for i in range(1, 4):
                conn.execute(
                    f"INSERT INTO WearSession(id, user_id, session_date, session_type) VALUES ('ws_div_{i}', ?, '2026-03-0{i}', 'casual');",
                    (self.user_id,),
                )
            for i in range(1, 4):
                conn.execute(
                    f"INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color, formality, like_rating) VALUES ('g_t_div_{i}', ?, 'T{i}', 'top', 'shirt', 'regular', 'c{i}', 2, 8);",
                    (self.user_id,),
                )
                conn.execute(
                    f"INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color, formality, like_rating) VALUES ('g_b_div_{i}', ?, 'B{i}', 'bottom', 'pants', 'regular', 'c{i}', 2, 8);",
                    (self.user_id,),
                )

            planner = ContinuationPlanner(conn)
            safe_plan_id = planner.generate_plan(self.user_id, "2026-03-01", "2026-03-03", StrategyMode.SAFE)
            safe_assignments = {
                r["session_id"]: tuple(sorted(r["garment_ids"].split(",")))
                for r in conn.execute(
                    """
                    SELECT psa.session_id, group_concat(pai.garment_id) as garment_ids
                    FROM PlanSessionAssignment psa
                    JOIN PlanAssignmentItem pai ON psa.id = pai.assignment_id
                    WHERE psa.plan_id = ?
                    GROUP BY psa.session_id;
                    """,
                    (safe_plan_id,)
                ).fetchall()
            }

            bal_plan_id = planner.generate_plan(
                self.user_id, "2026-03-01", "2026-03-03", StrategyMode.BALANCED,
                reference_safe_assignments=safe_assignments
            )

            bal_plan = conn.execute("SELECT diversity_achieved FROM RecommendationPlan WHERE id = ?;", (bal_plan_id,)).fetchone()
            self.assertIsNotNone(bal_plan["diversity_achieved"])
            self.assertGreaterEqual(bal_plan["diversity_achieved"], 0.0)

    # 79. Feasible-substitute count excludes dirty, reserved and incompatible garments
    def test_79_feasible_substitute_count_excludes_ineligible_garments(self):
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO WearSession(id, user_id, session_date, session_type, required_roles)
                VALUES ('ws_79', ?, '2026-03-01', 'casual', '["top", "bottom"]');
                """,
                (self.user_id,),
            )
            # 4 tops: 2 clean/available, 1 dirty, 1 in laundry
            conn.execute(
                """
                INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color, condition_state, availability_state)
                VALUES ('g_top_avail1', ?, 'T1', 'top', 'shirt', 'regular', 'white', 'CLEAN', 'AVAILABLE'),
                       ('g_top_avail2', ?, 'T2', 'top', 'shirt', 'regular', 'blue', 'CLEAN', 'AVAILABLE'),
                       ('g_top_dirty', ?, 'T3', 'top', 'shirt', 'regular', 'grey', 'NEEDS_WASH', 'AVAILABLE'),
                       ('g_top_laundry', ?, 'T4', 'top', 'shirt', 'regular', 'black', 'CLEAN', 'IN_LAUNDRY'),
                       ('g_bot_avail', ?, 'B1', 'bottom', 'pants', 'regular', 'navy', 'CLEAN', 'AVAILABLE');
                """,
                (self.user_id, self.user_id, self.user_id, self.user_id, self.user_id),
            )

            gen = CandidateGenerator(conn)
            # Exclude g_top_avail2 as reserved
            substitutes = gen.get_feasible_substitute_counts(
                session_id="ws_79",
                user_id=self.user_id,
                excluded_garment_ids={"g_top_avail2"},
            )

            # Of the 4 tops, 1 is dirty, 1 is in laundry, 1 is reserved -> exactly 1 feasible top remains
            self.assertEqual(substitutes.get("top"), 1, "Feasible substitute count for top must exclude dirty, laundry, and reserved garments")
            self.assertEqual(substitutes.get("bottom"), 1)

    # 80. Simulator reports foreign-key validity separately from domain invariants
    def test_80_simulator_reports_foreign_keys_separately_from_domain_invariants(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            temp_db_path = f.name

        try:
            db = Database(temp_db_path)
            db.migrate()

            with db.transaction() as conn:
                sim = LifecycleSmokeSimulator(conn, user_id="u_sim_80", db_file_path=temp_db_path)
                result = sim.run_simulation(start_date="2026-03-01", days=3, strategy_mode=StrategyMode.SAFE)

                self.assertIsInstance(result.foreign_keys_valid, bool)
                self.assertIsInstance(result.domain_invariants_valid, bool)
                self.assertTrue(result.foreign_keys_valid, "PRAGMA foreign_key_check must pass")
                self.assertTrue(result.domain_invariants_valid, "Domain reconciliation invariants must pass")
        finally:
            db.close()
            for p in Path(temp_db_path).parent.glob(Path(temp_db_path).name + "*"):
                try:
                    p.unlink()
                except Exception:
                    pass


if __name__ == "__main__":
    unittest.main()
