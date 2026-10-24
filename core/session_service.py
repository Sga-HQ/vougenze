"""
Vougenze Session & Routine Materialization Service (Frozen Spec 2.7, Scenarios 13-14).
Handles routine templates, day schedules, horizon session materialization per local civil date,
and automatic plan invalidation upon session creation/materialization.
"""
from __future__ import annotations
import json
import sqlite3
import uuid
from datetime import datetime, date, timedelta, timezone
from typing import Optional, List, Dict, Any

from core.models import InvalidationReason, PlanStatus


class SessionService:
    """Service managing wear session templates and horizon session materialization."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    @staticmethod
    def _utc_now() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def create_template(
        self,
        user_id: str,
        template_name: str,
        effective_from: str,
        effective_until: Optional[str] = None,
    ) -> str:
        template_id = f"wst_{uuid.uuid4().hex[:12]}"
        now = self._utc_now()
        self.conn.execute(
            """
            INSERT INTO WearSessionTemplate(id, user_id, template_name, effective_from, effective_until, created_at)
            VALUES (?, ?, ?, ?, ?, ?);
            """,
            (template_id, user_id, template_name, effective_from, effective_until, now),
        )
        return template_id

    def add_template_day(
        self,
        template_id: str,
        day_of_week: int,  # 0=Monday, 6=Sunday
        session_type: str,
        importance: str = "NORMAL",
        required_roles: Optional[List[str]] = None,
        start_time: Optional[str] = None,
        expected_duration_hours: Optional[float] = None,
        sort_order: int = 0,
    ) -> str:
        day_id = f"wstd_{uuid.uuid4().hex[:12]}"
        roles_json = json.dumps(required_roles or ["top", "bottom", "footwear"])
        self.conn.execute(
            """
            INSERT INTO WearSessionTemplateDay(
                id, template_id, day_of_week, session_type, importance,
                required_roles, start_time, expected_duration_hours, sort_order
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                day_id,
                template_id,
                day_of_week,
                session_type,
                importance,
                roles_json,
                start_time,
                expected_duration_hours,
                sort_order,
            ),
        )
        return day_id

    def create_session(
        self,
        user_id: str,
        session_date: str,  # YYYY-MM-DD
        session_type: str,
        importance: str = "NORMAL",
        required_roles: Optional[List[str]] = None,
        weather_context: Optional[str] = None,
        start_time: Optional[str] = None,
        expected_duration_hours: Optional[float] = None,
        generated_from_template_id: Optional[str] = None,
    ) -> str:
        """Creates a concrete WearSession instance and invalidates overlapping proposed plans."""
        session_id = f"ws_{uuid.uuid4().hex[:12]}"
        now = self._utc_now()
        roles_json = json.dumps(required_roles or ["top", "bottom", "footwear"])

        self.conn.execute(
            """
            INSERT INTO WearSession(
                id, user_id, session_date, session_type, importance,
                required_roles, weather_context, start_time, expected_duration_hours,
                generated_from_template_id, status, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PLANNED', ?);
            """,
            (
                session_id,
                user_id,
                session_date,
                session_type,
                importance,
                roles_json,
                weather_context,
                start_time,
                expected_duration_hours,
                generated_from_template_id,
                now,
            ),
        )

        # Invalidate existing proposed plans spanning this date
        self._invalidate_overlapping_plans(user_id, session_date, reason=InvalidationReason.EVENT_ADDED_OR_MODIFIED)

        return session_id

    def materialize_sessions_for_horizon(
        self,
        user_id: str,
        start_date: str,  # YYYY-MM-DD
        end_date: str,    # YYYY-MM-DD
        template_id: Optional[str] = None,
    ) -> List[str]:
        """
        Materializes template days into concrete WearSession rows for the given civil date range.
        Evaluates active template validity per date so templates active for partial horizons work correctly.
        Invalidates overlapping proposed plans when new sessions are materialized.
        """
        if template_id:
            t_row = self.conn.execute(
                "SELECT user_id FROM WearSessionTemplate WHERE id = ?;", (template_id,)
            ).fetchone()
            if not t_row:
                raise KeyError(f"WearSessionTemplate {template_id} not found")
            if t_row["user_id"] != user_id:
                raise PermissionError("Template belongs to a different user")

        start_dt = date.fromisoformat(start_date)
        end_dt = date.fromisoformat(end_date)
        current = start_dt

        created_session_ids = []
        while current <= end_dt:
            curr_str = current.isoformat()
            dow = current.weekday()  # Monday is 0, Sunday is 6

            # Resolve active template for this specific civil date
            if template_id:
                active_t_id = template_id
            else:
                cur = self.conn.execute(
                    """
                    SELECT id FROM WearSessionTemplate
                    WHERE user_id = ? AND effective_from <= ?
                      AND (effective_until IS NULL OR effective_until >= ?)
                    ORDER BY effective_from DESC LIMIT 1;
                    """,
                    (user_id, curr_str, curr_str),
                )
                row = cur.fetchone()
                active_t_id = row["id"] if row else None

            if active_t_id:
                # Fetch template days for this dow
                days_cur = self.conn.execute(
                    """
                    SELECT * FROM WearSessionTemplateDay
                    WHERE template_id = ? AND day_of_week = ?
                    ORDER BY sort_order ASC;
                    """,
                    (active_t_id, dow),
                )
                for entry in days_cur.fetchall():
                    check_cur = self.conn.execute(
                        """
                        SELECT id FROM WearSession
                        WHERE user_id = ? AND session_date = ? AND session_type = ?
                          AND (start_time IS ? OR start_time = ?);
                        """,
                        (user_id, curr_str, entry["session_type"], entry["start_time"], entry["start_time"]),
                    )
                    if not check_cur.fetchone():
                        sid = f"ws_{uuid.uuid4().hex[:12]}"
                        now = self._utc_now()
                        self.conn.execute(
                            """
                            INSERT INTO WearSession(
                                id, user_id, session_date, session_type, importance,
                                required_roles, start_time, expected_duration_hours,
                                generated_from_template_id, status, created_at
                            )
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'PLANNED', ?);
                            """,
                            (
                                sid,
                                user_id,
                                curr_str,
                                entry["session_type"],
                                entry["importance"],
                                entry["required_roles"],
                                entry["start_time"],
                                entry["expected_duration_hours"],
                                active_t_id,
                                now,
                            ),
                        )
                        created_session_ids.append(sid)

            current += timedelta(days=1)

        # Invalidate overlapping plans if any new session was materialized
        if created_session_ids:
            self.conn.execute(
                """
                UPDATE RecommendationPlan
                SET status = 'SUPERSEDED',
                    invalidation_reason = ?
                WHERE user_id = ? AND status = 'PROPOSED'
                  AND horizon_start_date <= ? AND horizon_end_date >= ?;
                """,
                (InvalidationReason.EVENT_ADDED_OR_MODIFIED.value, user_id, end_date, start_date),
            )

        return created_session_ids

    def get_horizon_sessions(
        self,
        user_id: str,
        start_date: str,
        end_date: str,
    ) -> List[sqlite3.Row]:
        """Returns all planned/completed sessions in date range ordered by session_date and start_time."""
        cur = self.conn.execute(
            """
            SELECT * FROM WearSession
            WHERE user_id = ? AND session_date >= ? AND session_date <= ?
              AND status != 'CANCELLED'
            ORDER BY session_date ASC, start_time ASC, id ASC;
            """,
            (user_id, start_date, end_date),
        )
        return cur.fetchall()

    def _invalidate_overlapping_plans(
        self,
        user_id: str,
        session_date: str,
        reason: InvalidationReason,
    ) -> None:
        """Invalidates proposed plans overlapping the added/modified session."""
        self.conn.execute(
            """
            UPDATE RecommendationPlan
            SET status = 'SUPERSEDED',
                invalidation_reason = ?
            WHERE user_id = ? AND status = 'PROPOSED'
              AND horizon_start_date <= ? AND horizon_end_date >= ?;
            """,
            (reason.value, user_id, session_date, session_date),
        )
