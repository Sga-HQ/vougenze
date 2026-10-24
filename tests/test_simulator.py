"""
Unit and integration tests for the Headless Wardrobe Simulator.
Validates multi-week lifecycle execution, state transitions, laundry cycles,
and storage economics measurement (Spec Part 1.4 & Part 5).
"""
import tempfile
import unittest
from pathlib import Path

from core.db import Database
from core.models import StrategyMode
from core.simulator import WardrobeSimulator


class TestWardrobeSimulator(unittest.TestCase):

    def test_multi_week_headless_simulation(self):
        """Simulates 14 days of wardrobe lifecycle including wear, laundry, and continuation planning."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            temp_db_path = f.name

        try:
            db = Database(temp_db_path)
            db.migrate()

            with db.transaction() as conn:
                sim = WardrobeSimulator(conn, user_id="u_sim_test", db_file_path=temp_db_path)
                result = sim.run_simulation(
                    start_date="2026-03-01",
                    days=14,
                    strategy_mode=StrategyMode.BALANCED,
                )

                self.assertTrue(result.invariants_passed, "All SQLite foreign key invariants must pass")
                self.assertEqual(result.total_days, 14)
                self.assertGreater(result.sessions_materialized, 0, "Sessions should be materialized from routine templates")
                self.assertGreater(result.wears_logged, 0, "Outfits should be logged")
                self.assertGreater(result.preference_evidence_events, 0, "Preference evidence should be recorded")
                self.assertGreater(result.db_size_bytes, 0, "Database size must be measured")

                # Verify unique garment wear count matches active wear events
                unique_wears = conn.execute(
                    "SELECT count(DISTINCT outfit_wear_log_id || '_' || garment_id) FROM OutfitWearLogItem;"
                ).fetchone()[0]
                events_cnt = conn.execute("SELECT count(*) FROM WearEvent WHERE is_voided = 0;").fetchone()[0]
                self.assertEqual(unique_wears, events_cnt, "Each unique garment wear must correspond to an active WearEvent")

                # Verify GarmentStateTransition audit trail was maintained
                trans_cnt = conn.execute("SELECT count(*) FROM GarmentStateTransition;").fetchone()[0]
                self.assertGreater(trans_cnt, 0, "Audit trail transitions must be recorded")

        finally:
            db.close()
            for p in Path(temp_db_path).parent.glob(Path(temp_db_path).name + "*"):
                try:
                    p.unlink()
                except Exception:
                    pass


if __name__ == "__main__":
    unittest.main()
