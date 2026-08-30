from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TypeVar

from .config import Settings, get_settings
from .schemas import InterviewReport, InterviewTurn, ResumeData
from .topics import canonical_topic


T = TypeVar("T")


SCHEMA_SQL = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;

CREATE TABLE IF NOT EXISTS interviews (
    id TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    company TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'backend',
    stress INTEGER NOT NULL DEFAULT 0,
    duration_minutes INTEGER NOT NULL,
    voice_mode TEXT NOT NULL,
    resume_json TEXT NOT NULL,
    style_json TEXT NOT NULL,
    weak_topics_json TEXT NOT NULL DEFAULT '[]',
    system_prompt TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'created',
    breakdown_streak INTEGER NOT NULL DEFAULT 0,
    last_question TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    started_at REAL,
    deadline_at REAL,
    ended_at REAL,
    end_reason TEXT
);

CREATE INDEX IF NOT EXISTS idx_interviews_client_created
    ON interviews(client_id, created_at DESC);

CREATE TABLE IF NOT EXISTS interview_turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    interview_id TEXT NOT NULL REFERENCES interviews(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    question TEXT NOT NULL,
    answer TEXT NOT NULL,
    category TEXT NOT NULL,
    topic TEXT NOT NULL,
    score REAL NOT NULL,
    deductions_json TEXT NOT NULL,
    failed INTEGER NOT NULL DEFAULT 0,
    drill_dimension TEXT NOT NULL DEFAULT '',
    drill_depth INTEGER NOT NULL DEFAULT 0,
    anchor_keyword TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(interview_id, ordinal)
);

CREATE INDEX IF NOT EXISTS idx_turns_interview
    ON interview_turns(interview_id, ordinal);

CREATE TABLE IF NOT EXISTS reports (
    id TEXT PRIMARY KEY,
    interview_id TEXT NOT NULL UNIQUE REFERENCES interviews(id) ON DELETE CASCADE,
    client_id TEXT NOT NULL,
    company TEXT NOT NULL,
    overall_score REAL NOT NULL,
    report_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_reports_client_created
    ON reports(client_id, created_at DESC);
"""


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.path = Path(self.settings.db_path)

    async def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)

        def operation(connection: sqlite3.Connection) -> None:
            connection.executescript(SCHEMA_SQL)
            connection.commit()

        await self._run(operation)

    async def create_interview(
        self,
        *,
        interview_id: str,
        client_id: str,
        company: str,
        role: str,
        stress: bool,
        duration_minutes: int,
        voice_mode: str,
        resume: ResumeData,
        style: dict[str, Any],
        weak_topics: list[str],
        system_prompt: str,
        initial_question: str,
    ) -> None:
        values = (
            interview_id,
            client_id,
            company,
            role,
            int(stress),
            duration_minutes,
            voice_mode,
            resume.model_dump_json(by_alias=True),
            json.dumps(style, ensure_ascii=False),
            json.dumps(weak_topics, ensure_ascii=False),
            system_prompt,
            initial_question,
            _utc_iso(),
        )

        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                """
                INSERT INTO interviews (
                    id, client_id, company, role, stress, duration_minutes,
                    voice_mode, resume_json, style_json, weak_topics_json,
                    system_prompt, last_question, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
            connection.commit()

        await self._run(operation)

    async def get_interview(self, interview_id: str) -> dict[str, Any] | None:
        def operation(connection: sqlite3.Connection) -> dict[str, Any] | None:
            row = connection.execute(
                "SELECT * FROM interviews WHERE id = ?", (interview_id,)
            ).fetchone()
            return self._decode_interview(row) if row else None

        return await self._run(operation)

    async def start_interview(self, interview_id: str) -> dict[str, Any] | None:
        def operation(connection: sqlite3.Connection) -> dict[str, Any] | None:
            row = connection.execute(
                "SELECT * FROM interviews WHERE id = ?", (interview_id,)
            ).fetchone()
            if not row:
                return None
            if row["started_at"] is None and row["status"] == "created":
                started = time.time()
                deadline = started + int(row["duration_minutes"]) * 60
                connection.execute(
                    """
                    UPDATE interviews
                    SET status = 'active', started_at = ?, deadline_at = ?
                    WHERE id = ?
                    """,
                    (started, deadline, interview_id),
                )
                connection.commit()
                row = connection.execute(
                    "SELECT * FROM interviews WHERE id = ?", (interview_id,)
                ).fetchone()
            return self._decode_interview(row)

        return await self._run(operation)

    async def append_turn(
        self,
        interview_id: str,
        turn: InterviewTurn,
        next_question: str,
    ) -> int:
        def operation(connection: sqlite3.Connection) -> int:
            connection.execute(
                """
                INSERT INTO interview_turns (
                    interview_id, ordinal, question, answer, category, topic,
                    score, deductions_json, failed, drill_dimension, drill_depth,
                    anchor_keyword, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    interview_id,
                    turn.ordinal,
                    turn.question,
                    turn.answer,
                    turn.category,
                    turn.topic,
                    turn.score,
                    json.dumps(turn.deductions, ensure_ascii=False),
                    int(turn.failed),
                    turn.drill_dimension,
                    turn.drill_depth,
                    turn.anchor_keyword,
                    turn.created_at,
                ),
            )
            row = connection.execute(
                "SELECT breakdown_streak FROM interviews WHERE id = ?",
                (interview_id,),
            ).fetchone()
            old_streak = int(row["breakdown_streak"]) if row else 0
            streak = old_streak + 1 if turn.failed else 0
            connection.execute(
                """
                UPDATE interviews
                SET breakdown_streak = ?, last_question = ?
                WHERE id = ?
                """,
                (streak, next_question, interview_id),
            )
            connection.commit()
            return streak

        return await self._run(operation)

    async def set_last_question(self, interview_id: str, question: str) -> None:
        question = question.strip()
        if not question:
            return

        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                "UPDATE interviews SET last_question = ? WHERE id = ?",
                (question, interview_id),
            )
            connection.commit()

        await self._run(operation)

    async def set_voice_mode(self, interview_id: str, voice_mode: str) -> None:
        if voice_mode not in {"L0", "L1", "L2", "L3"}:
            return

        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                "UPDATE interviews SET voice_mode = ? WHERE id = ?",
                (voice_mode, interview_id),
            )
            connection.commit()

        await self._run(operation)

    async def list_turns(self, interview_id: str) -> list[InterviewTurn]:
        def operation(connection: sqlite3.Connection) -> list[InterviewTurn]:
            rows = connection.execute(
                """
                SELECT * FROM interview_turns
                WHERE interview_id = ? ORDER BY ordinal
                """,
                (interview_id,),
            ).fetchall()
            return [
                InterviewTurn(
                    ordinal=row["ordinal"],
                    question=row["question"],
                    answer=row["answer"],
                    category=row["category"],
                    topic=row["topic"],
                    score=row["score"],
                    deductions=json.loads(row["deductions_json"]),
                    failed=bool(row["failed"]),
                    drill_dimension=row["drill_dimension"],
                    drill_depth=row["drill_depth"],
                    anchor_keyword=row["anchor_keyword"],
                    created_at=row["created_at"],
                )
                for row in rows
            ]

        return await self._run(operation)

    async def finish_interview(self, interview_id: str, reason: str) -> bool:
        def operation(connection: sqlite3.Connection) -> bool:
            cursor = connection.execute(
                """
                UPDATE interviews
                SET status = CASE WHEN status = 'reported' THEN status ELSE 'ended' END,
                    ended_at = COALESCE(ended_at, ?),
                    end_reason = COALESCE(end_reason, ?)
                WHERE id = ?
                """,
                (time.time(), reason, interview_id),
            )
            connection.commit()
            return cursor.rowcount > 0

        return await self._run(operation)

    async def mark_reporting(self, interview_id: str) -> bool:
        def operation(connection: sqlite3.Connection) -> bool:
            cursor = connection.execute(
                """
                UPDATE interviews SET status = 'reporting'
                WHERE id = ? AND status IN ('ended', 'reporting')
                """,
                (interview_id,),
            )
            connection.commit()
            return cursor.rowcount > 0

        return await self._run(operation)

    async def save_report(
        self, interview: dict[str, Any], report: InterviewReport
    ) -> None:
        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                """
                INSERT INTO reports (
                    id, interview_id, client_id, company, overall_score,
                    report_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(interview_id) DO UPDATE SET
                    id=excluded.id,
                    overall_score=excluded.overall_score,
                    report_json=excluded.report_json,
                    created_at=excluded.created_at
                """,
                (
                    report.report_id,
                    interview["id"],
                    interview["client_id"],
                    interview["company"],
                    report.overall_score,
                    report.model_dump_json(),
                    report.generated_at,
                ),
            )
            connection.execute(
                "UPDATE interviews SET status = 'reported' WHERE id = ?",
                (interview["id"],),
            )
            connection.commit()

        await self._run(operation)

    async def get_report(self, interview_id: str) -> dict[str, Any] | None:
        def operation(connection: sqlite3.Connection) -> dict[str, Any] | None:
            row = connection.execute(
                "SELECT report_json FROM reports WHERE interview_id = ?",
                (interview_id,),
            ).fetchone()
            return json.loads(row["report_json"]) if row else None

        return await self._run(operation)

    async def history(self, client_id: str, limit: int = 20) -> list[dict[str, Any]]:
        def operation(connection: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = connection.execute(
                """
                SELECT r.report_json, i.role, i.stress, i.duration_minutes,
                       i.ended_at, i.end_reason
                FROM reports r
                JOIN interviews i ON i.id = r.interview_id
                WHERE r.client_id = ?
                ORDER BY r.created_at DESC LIMIT ?
                """,
                (client_id, limit),
            ).fetchall()
            reports: list[dict[str, Any]] = []
            for row in rows:
                report = json.loads(row["report_json"])
                report.update(
                    role=row["role"],
                    stress=bool(row["stress"]),
                    duration_minutes=row["duration_minutes"],
                    ended_at=(
                        datetime.fromtimestamp(row["ended_at"], timezone.utc).isoformat()
                        if row["ended_at"] is not None
                        else report.get("generated_at")
                    ),
                    end_reason=row["end_reason"],
                )
                reports.append(report)
            return reports

        return await self._run(operation)

    async def delete_history_item(self, client_id: str, interview_id: str) -> bool:
        def operation(connection: sqlite3.Connection) -> bool:
            row = connection.execute(
                "SELECT id FROM interviews WHERE id = ? AND client_id = ?",
                (interview_id, client_id),
            ).fetchone()
            if not row:
                return False
            connection.execute("DELETE FROM interviews WHERE id = ?", (interview_id,))
            connection.commit()
            return True

        return await self._run(operation)

    async def clear_history(self, client_id: str) -> int:
        def operation(connection: sqlite3.Connection) -> int:
            cursor = connection.execute(
                "DELETE FROM interviews WHERE client_id = ?", (client_id,)
            )
            connection.commit()
            return cursor.rowcount

        return await self._run(operation)

    async def weak_topics(self, client_id: str, limit: int = 3) -> list[str]:
        reports = await self.history(client_id, limit=3)
        if not reports:
            return []
        weights = [0.6, 0.3, 0.1]
        weighted: dict[str, float] = {}
        totals: dict[str, float] = {}
        for index, report in enumerate(reports):
            weight = weights[index] if index < len(weights) else 0.05
            per_report: dict[str, list[float]] = {}
            for topic, raw_score in (report.get("topic_scores") or {}).items():
                try:
                    score = float(raw_score)
                except (TypeError, ValueError):
                    continue
                canonical = canonical_topic(str(topic))
                per_report.setdefault(canonical, []).append(score)
            for topic, scores in per_report.items():
                # Old reports can contain both fine-grained and broad aliases.
                # Consolidate them once per report before applying recency.
                score = sum(scores) / len(scores)
                weighted[topic] = weighted.get(topic, 0) + score * weight
                totals[topic] = totals.get(topic, 0) + weight
        averages = {
            topic: value / totals[topic]
            for topic, value in weighted.items()
            if totals.get(topic)
        }
        return [topic for topic, _ in sorted(averages.items(), key=lambda pair: pair[1])[:limit]]

    async def interview_count_today(self, client_id: str | None = None) -> int:
        today = datetime.now(timezone.utc).date().isoformat()

        def operation(connection: sqlite3.Connection) -> int:
            if client_id:
                row = connection.execute(
                    """
                    SELECT COUNT(*) AS count FROM interviews
                    WHERE substr(created_at, 1, 10) = ? AND client_id = ?
                    """,
                    (today, client_id),
                ).fetchone()
            else:
                row = connection.execute(
                    """
                    SELECT COUNT(*) AS count FROM interviews
                    WHERE substr(created_at, 1, 10) = ?
                    """,
                    (today,),
                ).fetchone()
            return int(row["count"] if row else 0)

        return await self._run(operation)

    async def _run(self, function: Callable[[sqlite3.Connection], T]) -> T:
        # Each operation is deliberately short and never surrounds a model or
        # network call. Direct access avoids a thread-pool dependency on tiny
        # single-worker hackathon hosts while WAL still handles concurrent reads.
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            return function(connection)
        finally:
            connection.close()

    @staticmethod
    def _decode_interview(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["stress"] = bool(data["stress"])
        data["resume"] = json.loads(data.pop("resume_json"))
        data["style"] = json.loads(data.pop("style_json"))
        data["weak_topics"] = json.loads(data.pop("weak_topics_json"))
        deadline = data.get("deadline_at")
        data["remaining_seconds"] = (
            max(0, int(float(deadline) - time.time())) if deadline else None
        )
        return data
