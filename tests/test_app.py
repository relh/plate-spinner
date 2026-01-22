import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from plate_spinner.daemon.app import create_app
from plate_spinner.daemon.db import Database


def test_post_event_creates_session():
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Database(Path(tmpdir) / "test.db")
        app = create_app(db)
        client = TestClient(app)

        response = client.post("/events", json={
            "session_id": "abc123",
            "project_path": "/path/to/project",
            "event_type": "tool_call",
            "tool_name": "Read",
        })
        assert response.status_code == 200

        sessions = client.get("/sessions").json()
        assert len(sessions) == 1
        assert sessions[0]["session_id"] == "abc123"
        assert sessions[0]["status"] == "running"
        assert sessions[0]["provider"] == "claude"

        db.close()


def test_ask_user_question_sets_awaiting_input():
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Database(Path(tmpdir) / "test.db")
        app = create_app(db)
        client = TestClient(app)

        client.post("/events", json={
            "session_id": "abc123",
            "project_path": "/path/to/project",
            "event_type": "tool_call",
            "tool_name": "AskUserQuestion",
        })

        sessions = client.get("/sessions").json()
        assert sessions[0]["status"] == "awaiting_input"

        db.close()


def test_health_endpoint():
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Database(Path(tmpdir) / "test.db")
        app = create_app(db)
        client = TestClient(app)

        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

        db.close()


def test_todowrite_stores_todos():
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Database(Path(tmpdir) / "test.db")
        app = create_app(db)
        client = TestClient(app)

        client.post("/events", json={
            "session_id": "abc123",
            "project_path": "/path/to/project",
            "event_type": "tool_call",
            "tool_name": "TodoWrite",
            "tool_params": {
                "todos": [
                    {"content": "Task 1", "status": "completed"},
                    {"content": "Task 2", "status": "in_progress"},
                    {"content": "Task 3", "status": "pending"},
                ]
            }
        })

        sessions = client.get("/sessions").json()
        assert sessions[0]["todo_progress"] == "1/3"

        db.close()


def test_stop_event_sets_closed():
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Database(Path(tmpdir) / "test.db")
        app = create_app(db)
        client = TestClient(app)

        client.post("/events", json={
            "session_id": "abc123",
            "project_path": "/path/to/project",
            "event_type": "tool_call",
            "tool_name": "Read",
        })

        client.post("/events", json={
            "session_id": "abc123",
            "project_path": "/path/to/project",
            "event_type": "stop",
        })

        sessions = client.get("/sessions").json()
        assert sessions[0]["status"] == "closed"
        assert sessions[0]["last_tool"] == "Read"

        db.close()


def test_stop_with_error_sets_error():
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Database(Path(tmpdir) / "test.db")
        app = create_app(db)
        client = TestClient(app)

        client.post("/events", json={
            "session_id": "abc123",
            "project_path": "/path/to/project",
            "event_type": "tool_call",
            "tool_name": "Bash",
        })

        client.post("/events", json={
            "session_id": "abc123",
            "project_path": "/path/to/project",
            "event_type": "stop",
            "error": "Session crashed",
        })

        sessions = client.get("/sessions").json()
        assert sessions[0]["status"] == "error"
        assert sessions[0]["last_tool"] == "Bash"

        db.close()


def test_sessions_stopped_sets_closed():
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Database(Path(tmpdir) / "test.db")
        app = create_app(db)
        client = TestClient(app)

        client.post("/events", json={
            "session_id": "abc123",
            "project_path": "/path/to/project",
            "event_type": "tool_call",
            "tool_name": "Read",
        })

        sessions = client.get("/sessions").json()
        assert sessions[0]["status"] == "running"

        response = client.post("/sessions/stopped", json={
            "project_path": "/path/to/project",
        })
        assert response.status_code == 200
        assert response.json()["count"] == 1

        sessions = client.get("/sessions").json()
        assert sessions[0]["status"] == "closed"

        db.close()


def test_sessions_stopped_skips_already_closed():
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Database(Path(tmpdir) / "test.db")
        app = create_app(db)
        client = TestClient(app)

        client.post("/events", json={
            "session_id": "abc123",
            "project_path": "/path/to/project",
            "event_type": "tool_call",
            "tool_name": "Read",
        })

        client.post("/sessions/stopped", json={
            "project_path": "/path/to/project",
        })

        response = client.post("/sessions/stopped", json={
            "project_path": "/path/to/project",
        })
        assert response.json()["count"] == 0

        db.close()


def test_git_branch_stored_and_returned():
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Database(Path(tmpdir) / "test.db")
        app = create_app(db)
        client = TestClient(app)

        client.post("/events", json={
            "session_id": "abc123",
            "project_path": "/path/to/project",
            "event_type": "session_start",
            "git_branch": "feature/test",
        })

        sessions = client.get("/sessions").json()
        assert sessions[0]["git_branch"] == "feature/test"

        db.close()


def test_codex_agent_message_sets_awaiting_input():
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Database(Path(tmpdir) / "test.db")
        app = create_app(db)
        client = TestClient(app)

        client.post("/events", json={
            "session_id": "abc123",
            "project_path": "/path/to/project",
            "event_type": "agent_message",
            "provider": "codex",
        })

        sessions = client.get("/sessions").json()
        assert sessions[0]["status"] == "awaiting_input"
        assert sessions[0]["provider"] == "codex"

        db.close()


def test_codex_review_mode_sets_awaiting_approval():
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Database(Path(tmpdir) / "test.db")
        app = create_app(db)
        client = TestClient(app)

        client.post("/events", json={
            "session_id": "abc123",
            "project_path": "/path/to/project",
            "event_type": "review_mode",
            "provider": "codex",
        })

        sessions = client.get("/sessions").json()
        assert sessions[0]["status"] == "awaiting_approval"

        db.close()
