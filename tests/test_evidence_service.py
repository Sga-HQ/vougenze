"""
Unit tests for core.evidence_service.
Validates Scenarios 31, 32, 33, 34, and 35 (Data Readiness Contract).
"""
import json
import unittest
from core.db import Database
from core.models import OutfitCandidate, InclusionType
from core.evidence_service import PreferenceEvidenceService


class TestPreferenceEvidenceService(unittest.TestCase):

    def setUp(self):
        self.db = Database(":memory:")
        self.db.migrate()
        self.user_id = "u1"

        with self.db.transaction() as conn:
            conn.execute("INSERT INTO User(id, display_name) VALUES (?, 'Test User');", (self.user_id,))
            conn.execute(
                """
                INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color)
                VALUES ('g_top1', ?, 'White Shirt', 'top', 'shirt', 'regular', 'white'),
                       ('g_bot1', ?, 'Blue Jeans', 'bottom', 'jeans', 'regular', 'blue'),
                       ('g_shoe1', ?, 'Sneakers', 'footwear', 'sneakers', 'regular', 'white'),
                       ('g_top2', ?, 'Linen Shirt', 'top', 'shirt', 'regular', 'beige');
                """,
                (self.user_id, self.user_id, self.user_id, self.user_id),
            )
            conn.execute(
                """
                INSERT INTO WearSession(id, user_id, session_date, session_type)
                VALUES ('ws_univ', ?, '2026-03-01', 'university');
                """,
                (self.user_id,),
            )

    def test_scenario_31_only_feasible_item_marked_constraint_driven(self):
        """Scenario 31: Only feasible item marked constraint-driven, blocked alternative marked INFEASIBLE_REFERENCE."""
        with self.db.transaction() as conn:
            service = PreferenceEvidenceService(conn)

            feasible_cand = OutfitCandidate(
                id="cand_feas",
                garment_ids=["g_top1", "g_bot1", "g_shoe1"],
                roles={"top": "g_top1", "bottom": "g_bot1", "footwear": "g_shoe1"},
                compatibility_score=0.9,
                occasion_score=0.85,
                preference_score=0.8,
            )

            blocked_cand = OutfitCandidate(
                id="cand_blocked",
                garment_ids=["g_top2", "g_bot1", "g_shoe1"],
                roles={"top": "g_top2", "bottom": "g_bot1", "footwear": "g_shoe1"},
                compatibility_score=0.8,
            )

            alternatives = [
                {
                    "candidate": blocked_cand,
                    "was_feasible": False,
                    "inclusion_type": InclusionType.INFEASIBLE_REFERENCE.value,
                    "exclusion_reason": "top_in_laundry",
                }
            ]

            ev_id = service.record_selection_evidence(
                user_id=self.user_id,
                session_id="ws_univ",
                occasion_context="university",
                selected_candidate=feasible_cand,
                alternatives=alternatives,
                selection_reason=["only_clean_shirt_available"],
                is_constraint_driven=True,
            )

            # Check evidence header
            ev = conn.execute("SELECT * FROM PreferenceEvidence WHERE id = ?;", (ev_id,)).fetchone()
            flags = json.loads(ev["constraint_flags"])
            self.assertEqual(flags.get("constraint_driven"), 1)
            self.assertEqual(ev["occasion_context"], "university")

            # Check alternatives
            alts = conn.execute(
                "SELECT * FROM PreferenceEvidenceAlternative WHERE evidence_id = ? ORDER BY presentation_order ASC;",
                (ev_id,),
            ).fetchall()
            self.assertEqual(len(alts), 2)

            # Selected item
            self.assertEqual(alts[0]["was_selected"], 1)
            self.assertEqual(alts[0]["was_feasible"], 1)
            self.assertEqual(alts[0]["inclusion_type"], "PRESENTED")

            # Infeasible reference item
            self.assertEqual(alts[1]["was_selected"], 0)
            self.assertEqual(alts[1]["was_feasible"], 0)
            self.assertEqual(alts[1]["inclusion_type"], "INFEASIBLE_REFERENCE")
            self.assertEqual(alts[1]["exclusion_reason"], "top_in_laundry")

    def test_scenario_32_contextual_preference_evidence_scoped(self):
        """Scenario 32: Selecting an outfit for university scopes preference evidence strictly to university context."""
        with self.db.transaction() as conn:
            service = PreferenceEvidenceService(conn)

            cand = OutfitCandidate(
                id="cand_univ",
                garment_ids=["g_top1", "g_bot1", "g_shoe1"],
                roles={"top": "g_top1", "bottom": "g_bot1", "footwear": "g_shoe1"},
            )

            ev_id = service.record_selection_evidence(
                user_id=self.user_id,
                session_id="ws_univ",
                occasion_context="university",
                selected_candidate=cand,
                alternatives=[],
            )

            ev = conn.execute("SELECT * FROM PreferenceEvidence WHERE id = ?;", (ev_id,)).fetchone()
            self.assertEqual(ev["occasion_context"], "university")

    def test_scenario_33_pairwise_evidence_captures_normalized_alternatives(self):
        """Scenario 33: Normalized candidate snapshots and presentation order preserved."""
        with self.db.transaction() as conn:
            service = PreferenceEvidenceService(conn)

            cand_a = OutfitCandidate(
                id="cand_a",
                garment_ids=["g_top1", "g_bot1", "g_shoe1"],
                roles={"top": "g_top1", "bottom": "g_bot1", "footwear": "g_shoe1"},
                compatibility_score=0.9,
            )
            cand_b = OutfitCandidate(
                id="cand_b",
                garment_ids=["g_top2", "g_bot1", "g_shoe1"],
                roles={"top": "g_top2", "bottom": "g_bot1", "footwear": "g_shoe1"},
                compatibility_score=0.85,
            )

            ev_id = service.record_selection_evidence(
                user_id=self.user_id,
                session_id="ws_univ",
                occasion_context="university",
                selected_candidate=cand_a,
                alternatives=[{"candidate": cand_b, "was_feasible": True, "inclusion_type": "PRESENTED"}],
            )

            # Check snapshots created
            snaps = conn.execute("SELECT * FROM OutfitCandidateSnapshot WHERE session_id = 'ws_univ';").fetchall()
            self.assertEqual(len(snaps), 2)

            # Check snapshot items
            items_a = conn.execute("SELECT * FROM OutfitCandidateSnapshotItem WHERE candidate_id = 'cand_a';").fetchall()
            self.assertEqual(len(items_a), 3)

    def test_scenario_34_recent_taste_shift_data_readiness(self):
        """Scenario 34: Consecutive recent rejections stored chronologically with reasons and alternatives."""
        with self.db.transaction() as conn:
            service = PreferenceEvidenceService(conn)

            cand = OutfitCandidate(
                id="cand_rej",
                garment_ids=["g_top1", "g_bot1", "g_shoe1"],
                roles={"top": "g_top1", "bottom": "g_bot1", "footwear": "g_shoe1"},
            )

            rejections = [
                ("2026-03-01T10:00:00Z", "Too formal for today"),
                ("2026-03-02T10:00:00Z", "Feeling tired of white shirts"),
                ("2026-03-03T10:00:00Z", "Color feels too plain"),
            ]

            for ts, reason in rejections:
                service.record_rejection_evidence(
                    user_id=self.user_id,
                    session_id="ws_univ",
                    occasion_context="university",
                    rejected_candidate=cand,
                    rejection_reason=reason,
                    recorded_at=ts,
                )

            evs = conn.execute(
                "SELECT * FROM PreferenceEvidence WHERE evidence_type = 'rejection' ORDER BY recorded_at ASC;"
            ).fetchall()
            self.assertEqual(len(evs), 3)
            self.assertEqual(evs[0]["rejection_reason"], "Too formal for today")
            self.assertEqual(evs[1]["rejection_reason"], "Feeling tired of white shirts")
            self.assertEqual(evs[2]["rejection_reason"], "Color feels too plain")

    def test_scenario_35_laundry_rejection_yields_no_taste_penalty(self):
        """Scenario 35: Rejecting outfit because items are in laundry flags laundry_constrained and 0 aesthetic penalty."""
        with self.db.transaction() as conn:
            service = PreferenceEvidenceService(conn)

            cand = OutfitCandidate(
                id="cand_laundry_rej",
                garment_ids=["g_top1", "g_bot1", "g_shoe1"],
                roles={"top": "g_top1", "bottom": "g_bot1", "footwear": "g_shoe1"},
            )

            ev_id = service.record_rejection_evidence(
                user_id=self.user_id,
                session_id="ws_univ",
                occasion_context="university",
                rejected_candidate=cand,
                rejection_reason="Items are in laundry batch lb1",
            )

            ev = conn.execute("SELECT * FROM PreferenceEvidence WHERE id = ?;", (ev_id,)).fetchone()
            flags = json.loads(ev["constraint_flags"])
            self.assertEqual(flags.get("laundry_constrained"), 1)
            self.assertEqual(flags.get("aesthetic_penalty"), 0)


if __name__ == "__main__":
    unittest.main()
