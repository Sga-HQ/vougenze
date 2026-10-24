"""
Vougenze SQLite Connection Factory and Transaction Boundaries.
Enforces foreign_keys = ON, WAL mode, migration ledger, and explicit transaction contexts.
"""
from __future__ import annotations
import hashlib
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional, Union, List

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def split_sql_statements(sql: str) -> List[str]:
    """Splits SQL text into executable statements using sqlite3's statement completion."""
    clean_lines = [line for line in sql.splitlines() if not line.strip().startswith("--")]
    clean_sql = "\n".join(clean_lines)
    statements: List[str] = []
    current: List[str] = []
    for line in clean_sql.splitlines():
        current.append(line)
        candidate = "\n".join(current)
        if sqlite3.complete_statement(candidate):
            cand_str = candidate.strip()
            if cand_str:
                statements.append(cand_str)
            current = []
    remainder = "\n".join(current).strip()
    if remainder:
        statements.append(remainder)
    return statements


class Database:
    """Manages SQLite connections, foreign keys enforcement, and transactional boundaries."""

    def __init__(self, db_path: Union[str, Path] = ":memory:"):
        path_str = str(db_path)
        self._is_memory = (path_str == ":memory:")
        if self._is_memory:
            # Persistent shared-memory URI so multiple connections can access the in-memory DB
            mem_id = uuid.uuid4().hex
            self.db_path = f"file:mem_{mem_id}?mode=memory&cache=shared"
            self._is_uri = True
            # Keep anchor connection alive for the lifetime of this Database instance
            self._anchor_conn: Optional[sqlite3.Connection] = self._create_connection()
        else:
            self.db_path = path_str
            self._is_uri = path_str.startswith("file:")
            self._anchor_conn = None

    def _create_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.db_path,
            detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES,
            timeout=30.0,
            isolation_level=None,  # Explicit transaction control
            uri=self._is_uri,
            check_same_thread=False if self._is_memory else True,
        )
        conn.row_factory = sqlite3.Row

        # Critical: foreign_keys must be ON and verified
        conn.execute("PRAGMA foreign_keys = ON;")
        fk_val = conn.execute("PRAGMA foreign_keys;").fetchone()[0]
        if fk_val != 1:
            conn.close()
            raise RuntimeError("SQLite foreign-key enforcement is disabled")

        if not self._is_memory:
            journal_mode = conn.execute("PRAGMA journal_mode = WAL;").fetchone()[0]
            conn.execute("PRAGMA synchronous = NORMAL;")
        conn.execute("PRAGMA busy_timeout = 5000;")
        return conn

    def get_connection(self) -> sqlite3.Connection:
        if self._is_memory and self._anchor_conn is not None:
            return self._anchor_conn
        return self._create_connection()

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """Provides a managed connection without an explicit transaction block."""
        conn = self.get_connection()
        try:
            yield conn
        finally:
            if not self._is_memory:
                conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """
        Transactional boundary.
        Executes BEGIN IMMEDIATE to acquire write lock, commits on success, rolls back on error.
        Fails closed if a transaction is already active on the connection.
        """
        conn = self.get_connection()
        if conn.in_transaction:
            raise RuntimeError("Nested transaction attempted without savepoint support")
        conn.execute("BEGIN IMMEDIATE;")
        try:
            yield conn
            conn.execute("COMMIT;")
        except Exception:
            conn.execute("ROLLBACK;")
            raise
        finally:
            if not self._is_memory:
                conn.close()

    def migrate(self) -> None:
        """Executes unapplied .sql files in migrations/ under the SchemaMigration ledger."""
        migration_files = sorted(MIGRATIONS_DIR.glob("*.sql"))
        conn = self.get_connection()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS SchemaMigration (
                    version TEXT PRIMARY KEY,
                    filename TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    applied_at TEXT NOT NULL
                );
                """
            )
            for sql_file in migration_files:
                version = sql_file.stem
                sql_content = sql_file.read_text(encoding="utf-8")
                file_sha = hashlib.sha256(sql_content.encode("utf-8")).hexdigest()

                row = conn.execute(
                    "SELECT sha256 FROM SchemaMigration WHERE version = ?;", (version,)
                ).fetchone()
                if row:
                    if row["sha256"] != file_sha:
                        raise RuntimeError(
                            f"Applied migration {version} differs from current file"
                        )
                    continue

                now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

                statements = split_sql_statements(sql_content)
                conn.execute("BEGIN IMMEDIATE;")
                try:
                    for stmt in statements:
                        upper_stmt = stmt.strip().upper()
                        if upper_stmt in ("BEGIN;", "BEGIN IMMEDIATE;", "COMMIT;"):
                            continue
                        conn.execute(stmt)
                    conn.execute(
                        "INSERT INTO SchemaMigration(version, filename, sha256, applied_at) VALUES (?, ?, ?, ?);",
                        (version, sql_file.name, file_sha, now),
                    )
                    conn.execute("COMMIT;")
                except Exception:
                    conn.execute("ROLLBACK;")
                    raise
        finally:
            if not self._is_memory:
                conn.close()

    def close(self) -> None:
        if self._anchor_conn is not None:
            self._anchor_conn.close()
            self._anchor_conn = None
