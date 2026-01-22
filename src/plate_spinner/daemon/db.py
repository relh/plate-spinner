import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    project_path TEXT NOT NULL,
    transcript_path TEXT,
    git_branch TEXT,
    provider TEXT NOT NULL DEFAULT 'claude',
    status TEXT NOT NULL DEFAULT 'running',
    last_event_type TEXT,
    last_tool TEXT,
    summary TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS todos (
    session_id TEXT PRIMARY KEY REFERENCES sessions(session_id),
    todos_json TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status);
CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id);
"""


class Database:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        cursor = self.conn.execute("PRAGMA table_info(sessions)")
        columns = {row[1] for row in cursor.fetchall()}
        if "summary" not in columns:
            self.conn.execute("ALTER TABLE sessions ADD COLUMN summary TEXT")
        if "transcript_path" not in columns:
            self.conn.execute("ALTER TABLE sessions ADD COLUMN transcript_path TEXT")
        if "git_branch" not in columns:
            self.conn.execute("ALTER TABLE sessions ADD COLUMN git_branch TEXT")
        if "provider" not in columns:
            self.conn.execute("ALTER TABLE sessions ADD COLUMN provider TEXT DEFAULT 'claude'")

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    def commit(self) -> None:
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()
