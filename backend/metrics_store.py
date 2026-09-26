import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class SystemMetricsReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    commit_sha: str = Field(min_length=7, max_length=64)
    commit_message: str = Field(min_length=1, max_length=500)
    branch: str | None = Field(default=None, max_length=255)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    cpu_percent: float = Field(ge=0, le=100)
    memory_percent: float = Field(ge=0, le=100)
    memory_used_bytes: int = Field(ge=0)
    memory_total_bytes: int = Field(ge=0)
    disk_percent: float = Field(ge=0, le=100)
    disk_used_bytes: int = Field(ge=0)
    disk_total_bytes: int = Field(ge=0)
    network_sent_bytes: int = Field(ge=0)
    network_received_bytes: int = Field(ge=0)
    uptime_seconds: float = Field(ge=0)


class MetricsStore:
    def __init__(self, database_path: str | Path | None = None) -> None:
        self.database_path = (
            Path(database_path)
            if database_path is not None
            else Path(__file__).with_name("metrics.sqlite3")
        )

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with (
            closing(sqlite3.connect(self.database_path, timeout=5)) as connection,
            connection,
        ):
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                    CREATE TABLE IF NOT EXISTS system_usage_reports (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        system_id TEXT NOT NULL,
                        source TEXT NOT NULL DEFAULT 'reported',
                        commit_sha TEXT NOT NULL DEFAULT 'unknown',
                        commit_message TEXT NOT NULL DEFAULT '',
                        branch TEXT,
                        timestamp TEXT NOT NULL,
                        cpu_percent REAL NOT NULL,
                        memory_percent REAL NOT NULL,
                        memory_used_bytes INTEGER NOT NULL,
                        memory_total_bytes INTEGER NOT NULL,
                        disk_percent REAL NOT NULL,
                        disk_used_bytes INTEGER NOT NULL,
                        disk_total_bytes INTEGER NOT NULL,
                        network_sent_bytes INTEGER NOT NULL,
                        network_received_bytes INTEGER NOT NULL,
                        uptime_seconds REAL NOT NULL
                    )
                    """
            )
            columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(system_usage_reports)"
                ).fetchall()
            }
            if "source" not in columns:
                connection.execute(
                    "ALTER TABLE system_usage_reports "
                    "ADD COLUMN source TEXT NOT NULL DEFAULT 'reported'"
                )
            if "commit_sha" not in columns:
                connection.execute(
                    "ALTER TABLE system_usage_reports "
                    "ADD COLUMN commit_sha TEXT NOT NULL DEFAULT 'unknown'"
                )
            if "commit_message" not in columns:
                connection.execute(
                    "ALTER TABLE system_usage_reports "
                    "ADD COLUMN commit_message TEXT NOT NULL DEFAULT ''"
                )
            if "branch" not in columns:
                connection.execute(
                    "ALTER TABLE system_usage_reports ADD COLUMN branch TEXT"
                )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_usage_reports_system_source_time "
                "ON system_usage_reports(system_id, source, timestamp DESC)"
            )

    def save(
        self,
        system_id: str,
        report: SystemMetricsReport,
        source: str = "reported",
    ) -> dict[str, object]:
        timestamp = report.timestamp
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        else:
            timestamp = timestamp.astimezone(timezone.utc)

        values = report.model_dump(exclude={"timestamp"})
        values.update(
            system_id=system_id, source=source, timestamp=timestamp.isoformat()
        )
        with (
            closing(sqlite3.connect(self.database_path, timeout=5)) as connection,
            connection,
        ):
            cursor = connection.execute(
                """
                    INSERT INTO system_usage_reports (
                        system_id, source, commit_sha, commit_message, branch, timestamp,
                        cpu_percent, memory_percent,
                        memory_used_bytes, memory_total_bytes, disk_percent,
                        disk_used_bytes, disk_total_bytes, network_sent_bytes,
                        network_received_bytes, uptime_seconds
                    ) VALUES (
                        :system_id, :source, :commit_sha, :commit_message, :branch,
                        :timestamp, :cpu_percent, :memory_percent,
                        :memory_used_bytes, :memory_total_bytes, :disk_percent,
                        :disk_used_bytes, :disk_total_bytes, :network_sent_bytes,
                        :network_received_bytes, :uptime_seconds
                    )
                    """,
                values,
            )

        values["id"] = cursor.lastrowid
        return values

    def history(
        self,
        system_id: str,
        limit: int,
        source: str | None = None,
    ) -> list[dict[str, object]]:
        with closing(sqlite3.connect(self.database_path, timeout=5)) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                  SELECT source, commit_sha, commit_message, branch, timestamp,
                      cpu_percent, memory_percent, memory_used_bytes, memory_total_bytes,
                      disk_percent, disk_used_bytes,
                       disk_total_bytes, network_sent_bytes, network_received_bytes,
                       uptime_seconds
                FROM system_usage_reports
                WHERE system_id = ? AND (? IS NULL OR source = ?)
                ORDER BY timestamp DESC, id DESC
                LIMIT ?
                """,
                (system_id, source, source, limit),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def dashboard(
        self,
        system_id: str,
        limit: int,
        source: str | None = None,
    ) -> dict[str, object]:
        with closing(sqlite3.connect(self.database_path, timeout=5)) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                SELECT * FROM (
                    SELECT system_id, source, commit_sha, commit_message, branch,
                           timestamp, cpu_percent, memory_percent,
                           memory_used_bytes, memory_total_bytes, disk_percent,
                           disk_used_bytes, disk_total_bytes, network_sent_bytes,
                           network_received_bytes, uptime_seconds,
                           ROW_NUMBER() OVER (
                               PARTITION BY source, commit_sha
                               ORDER BY timestamp DESC, id DESC
                           ) AS commit_rank
                    FROM system_usage_reports
                    WHERE system_id = ? AND (? IS NULL OR source = ?)
                )
                WHERE commit_rank = 1
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (system_id, source, source, limit),
            ).fetchall()

        commits = [dict(row) for row in rows]
        comparisons: list[dict[str, object]] = [
            {
                "commit_sha": commit["commit_sha"],
                "commit_message": commit["commit_message"],
                "branch": commit["branch"],
                "timestamp": commit["timestamp"],
                "current": commit,
                "previous": commits[index + 1] if index + 1 < len(commits) else None,
            }
            for index, commit in enumerate(commits)
        ]
        return {
            "system_id": system_id,
            "source": source,
            "current": commits[0] if commits else None,
            "previous": commits[1] if len(commits) > 1 else None,
            "commits": comparisons,
        }

    def systems(self) -> list[dict[str, object]]:
        with closing(sqlite3.connect(self.database_path, timeout=5)) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                  SELECT system_id, source, MAX(timestamp) AS last_reported_at,
                      COUNT(*) AS report_count
                FROM system_usage_reports
                  GROUP BY system_id, source
                  ORDER BY system_id, source
                """
            ).fetchall()
        return [dict(row) for row in rows]
