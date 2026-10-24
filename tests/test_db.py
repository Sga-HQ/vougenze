"""
Unit tests for core.db connection factory and transaction boundaries.
Uses built-in unittest so no external dependencies are required.
"""
import unittest
import sqlite3
import tempfile
from pathlib import Path
from core.db import Database


class TestDatabase(unittest.TestCase):

    def test_migration_and_foreign_keys_on_memory(self):
        db = Database(":memory:")
        db.migrate()

        with db.connection() as conn:
            # Verify FK is enabled
            fk = conn.execute("PRAGMA foreign_keys;").fetchone()[0]
            self.assertEqual(fk, 1, "PRAGMA foreign_keys must be ON")

            # Verify table count (34 domain tables + 1 SchemaMigration ledger = 35)
            domain_tables = conn.execute(
                "SELECT count(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name != 'SchemaMigration';"
            ).fetchone()[0]
            self.assertEqual(domain_tables, 34, f"Expected 34 domain tables, got {domain_tables}")

            # Verify Garment foreign keys include LaundryBatch and GarmentCondition
            fks = conn.execute("PRAGMA foreign_key_list('Garment');").fetchall()
            fk_targets = {r["from"]: r["table"] for r in fks}
            self.assertEqual(fk_targets.get("current_laundry_batch_id"), "LaundryBatch")
            self.assertEqual(fk_targets.get("current_condition_assessment_id"), "GarmentCondition")

            # Verify foreign key violation raises IntegrityError
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO Garment(id, user_id, name, physical_category, subcategory, cut_and_fit, primary_color) "
                    "VALUES ('g1', 'nonexistent_user', 'Shirt', 'top', 'shirt', 'regular', 'white');"
                )

    def test_transaction_rollback_on_exception(self):
        db = Database(":memory:")
        db.migrate()

        with db.transaction() as conn:
            conn.execute("INSERT INTO User(id, display_name) VALUES ('u1', 'Alice');")

        # Attempt a transaction that succeeds halfway then raises
        with self.assertRaises(RuntimeError):
            with db.transaction() as conn:
                conn.execute("INSERT INTO User(id, display_name) VALUES ('u2', 'Bob');")
                raise RuntimeError("Simulated crash")

        with db.connection() as conn:
            users = [r[0] for r in conn.execute("SELECT id FROM User;").fetchall()]
            self.assertIn("u1", users)
            self.assertNotIn("u2", users, "u2 should have been rolled back")

    def test_file_database_and_wal_mode(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            temp_path = f.name

        try:
            db = Database(temp_path)
            db.migrate()

            with db.connection() as conn:
                journal_mode = conn.execute("PRAGMA journal_mode;").fetchone()[0].upper()
                self.assertEqual(journal_mode, "WAL", f"Expected WAL mode, got {journal_mode}")

                fk = conn.execute("PRAGMA foreign_keys;").fetchone()[0]
                self.assertEqual(fk, 1)
        finally:
            db.close()
            for p in Path(temp_path).parent.glob(Path(temp_path).name + "*"):
                try:
                    p.unlink()
                except Exception:
                    pass

    def test_schema_invariants_script(self):
        """Executes test_schema_invariants.sql and verifies all assertions pass."""
        sql_path = Path(__file__).parent / "test_schema_invariants.sql"
        sql_content = sql_path.read_text(encoding="utf-8")

        db = Database(":memory:")
        db.migrate()

        with db.connection() as conn:
            # Execute the invariant test script
            conn.executescript(sql_content)

            # Check that foreign keys are still valid
            fk_violations = conn.execute("PRAGMA foreign_key_check;").fetchall()
            self.assertEqual(len(fk_violations), 0, f"Foreign key violations found: {fk_violations}")


if __name__ == "__main__":
    unittest.main()
