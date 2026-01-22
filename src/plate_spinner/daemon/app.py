import json
from datetime import datetime, timezone

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from pydantic import BaseModel

from ..hooks import hooks_installed
from .db import Database
from .models import HookEvent, SessionStatus
from .summarizer import get_api_key, summarize_session


def _determine_status(event: HookEvent) -> SessionStatus:
    if event.event_type == "stop":
        return SessionStatus.ERROR if event.error else SessionStatus.CLOSED
    if event.event_type in ("session_start", "tool_start"):
        return SessionStatus.RUNNING
    if event.provider == "codex":
        if event.event_type == "agent_message":
            return SessionStatus.AWAITING_INPUT
        if event.event_type == "review_mode":
            return SessionStatus.AWAITING_APPROVAL
        if event.event_type in ("user_message", "review_mode_exit"):
            return SessionStatus.RUNNING
    return SessionStatus.from_tool(event.tool_name or "")


def _upsert_session(db: Database, event: HookEvent, status: SessionStatus, now: str) -> bool:
    existing = db.execute(
        "SELECT session_id FROM sessions WHERE session_id = ?",
        (event.session_id,)
    ).fetchone()

    provider = event.provider or "claude"

    if not existing:
        placeholder_id = f"pending:{event.project_path}"
        db.execute("DELETE FROM sessions WHERE session_id = ?", (placeholder_id,))
        db.execute(
            """INSERT INTO sessions
               (session_id, project_path, transcript_path, git_branch, provider, status,
                last_event_type, last_tool, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (event.session_id, event.project_path,
             event.transcript_path, event.git_branch, provider, status.value, event.event_type, event.tool_name, now, now)
        )
    else:
        db.execute(
            """UPDATE sessions SET
               status = ?, last_event_type = ?,
               last_tool = COALESCE(?, last_tool),
               transcript_path = COALESCE(?, transcript_path),
               git_branch = COALESCE(?, git_branch),
               provider = COALESCE(?, provider),
               updated_at = ?
               WHERE session_id = ?""",
            (status.value, event.event_type, event.tool_name,
             event.transcript_path, event.git_branch, event.provider, now, event.session_id)
        )
    return existing is not None


def _handle_todo_write(db: Database, event: HookEvent, now: str) -> None:
    if event.tool_name != "TodoWrite" or not event.tool_params:
        return
    todos = event.tool_params.get("todos", [])
    if todos:
        db.execute(
            """INSERT OR REPLACE INTO todos (session_id, todos_json, updated_at)
               VALUES (?, ?, ?)""",
            (event.session_id, json.dumps(todos), now)
        )


def _maybe_summarize(db: Database, event: HookEvent, status: SessionStatus) -> None:
    should_summarize = status in (SessionStatus.AWAITING_INPUT, SessionStatus.AWAITING_APPROVAL, SessionStatus.IDLE)
    if not should_summarize and event.event_type == "tool_call":
        event_count = db.execute(
            "SELECT COUNT(*) FROM events WHERE session_id = ?",
            (event.session_id,)
        ).fetchone()[0]
        should_summarize = event_count > 0 and event_count % 5 == 0

    if not should_summarize:
        row = db.execute(
            "SELECT summary FROM sessions WHERE session_id = ?",
            (event.session_id,)
        ).fetchone()
        should_summarize = row and row[0] is None

    if not should_summarize:
        return

    try:
        transcript = event.transcript_path
        if not transcript:
            row = db.execute(
                "SELECT transcript_path FROM sessions WHERE session_id = ?",
                (event.session_id,)
            ).fetchone()
            transcript = row[0] if row else None
        summary = summarize_session(transcript)
        if summary:
            db.execute(
                "UPDATE sessions SET summary = ? WHERE session_id = ?",
                (summary, event.session_id)
            )
    except Exception:
        pass


class RegisterRequest(BaseModel):
    project_path: str
    provider: str | None = None


class ConnectionManager:
    def __init__(self) -> None:
        self.connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self.connections.append(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        if websocket in self.connections:
            self.connections.remove(websocket)

    async def broadcast(self, message: dict) -> None:
        dead: list[WebSocket] = []
        for connection in self.connections:
            try:
                await connection.send_json(message)
            except (WebSocketDisconnect, RuntimeError, ConnectionError):
                dead.append(connection)
        for conn in dead:
            self.disconnect(conn)


def create_app(db: Database) -> FastAPI:
    app = FastAPI(title="Plate-Spinner Daemon")
    manager = ConnectionManager()

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    @app.get("/status")
    async def status() -> dict:
        return {
            "status": "ok",
            "api_key_configured": get_api_key() is not None,
            "hooks_installed": hooks_installed(),
        }

    @app.post("/events")
    async def post_event(event: HookEvent) -> dict:
        now = datetime.now(timezone.utc).isoformat()
        status = _determine_status(event)

        _upsert_session(db, event, status, now)
        _handle_todo_write(db, event, now)

        db.execute(
            """INSERT INTO events (session_id, event_type, payload, created_at)
               VALUES (?, ?, ?, ?)""",
            (event.session_id, event.event_type, event.model_dump_json(), now)
        )

        _maybe_summarize(db, event, status)
        db.commit()

        await manager.broadcast({"type": "session_update", "session_id": event.session_id})
        return {"status": "ok"}

    @app.post("/sessions/register")
    async def register_session(req: RegisterRequest) -> dict:
        now = datetime.now(timezone.utc).isoformat()
        placeholder_id = f"pending:{req.project_path}"

        existing = db.execute(
            "SELECT session_id FROM sessions WHERE session_id = ?",
            (placeholder_id,)
        ).fetchone()

        if not existing:
            db.execute(
                """INSERT INTO sessions
                   (session_id, project_path, provider, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (placeholder_id, req.project_path, req.provider or "claude", "starting", now, now)
            )
            db.commit()
            await manager.broadcast({"type": "session_update", "session_id": placeholder_id})

        return {"status": "ok", "placeholder_id": placeholder_id}

    @app.post("/sessions/stopped")
    async def mark_stopped(req: RegisterRequest) -> dict:
        now = datetime.now(timezone.utc).isoformat()
        rows = db.execute(
            """SELECT session_id FROM sessions
               WHERE project_path = ? AND status NOT IN ('closed', 'error')""",
            (req.project_path,)
        ).fetchall()

        for row in rows:
            session_id = row[0]
            db.execute(
                "UPDATE sessions SET status = ?, updated_at = ? WHERE session_id = ?",
                (SessionStatus.CLOSED.value, now, session_id)
            )
            await manager.broadcast({"type": "session_update", "session_id": session_id})

        db.commit()
        return {"status": "ok", "count": len(rows)}

    @app.get("/sessions")
    async def get_sessions() -> list[dict]:
        rows = db.execute(
            """SELECT s.session_id, s.project_path, s.git_branch, s.provider, s.status,
                      s.last_event_type, s.last_tool, s.summary, s.created_at, s.updated_at,
                      t.todos_json
               FROM sessions s
               LEFT JOIN todos t ON s.session_id = t.session_id
               ORDER BY s.updated_at DESC"""
        ).fetchall()

        result = []
        for row in rows:
            d = dict(row)
            todos_json = d.pop("todos_json", None)
            if todos_json:
                todos = json.loads(todos_json)
                completed = sum(1 for t in todos if t.get("status") == "completed")
                d["todo_progress"] = f"{completed}/{len(todos)}"
            else:
                d["todo_progress"] = None
            result.append(d)
        return result

    @app.delete("/sessions/{session_id}")
    async def delete_session(session_id: str) -> dict:
        db.execute("DELETE FROM todos WHERE session_id = ?", (session_id,))
        db.execute("DELETE FROM events WHERE session_id = ?", (session_id,))
        db.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
        db.commit()
        await manager.broadcast({"type": "session_deleted", "session_id": session_id})
        return {"status": "ok"}

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        await manager.connect(websocket)
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            manager.disconnect(websocket)

    return app
