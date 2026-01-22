from plate_spinner.daemon.models import HookEvent, Session, SessionStatus


def test_hook_event_from_dict():
    data = {
        "session_id": "abc123",
        "project_path": "/path/to/project",
        "event_type": "tool_call",
        "tool_name": "AskUserQuestion",
        "provider": "codex",
    }
    event = HookEvent(**data)
    assert event.session_id == "abc123"
    assert event.tool_name == "AskUserQuestion"
    assert event.provider == "codex"


def test_session_status_from_tool():
    assert SessionStatus.from_tool("AskUserQuestion") == SessionStatus.AWAITING_INPUT
    assert SessionStatus.from_tool("ExitPlanMode") == SessionStatus.AWAITING_APPROVAL
    assert SessionStatus.from_tool("Read") == SessionStatus.RUNNING


def test_session_status_values():
    assert SessionStatus.RUNNING.value == "running"
    assert SessionStatus.IDLE.value == "idle"
    assert SessionStatus.AWAITING_INPUT.value == "awaiting_input"
    assert SessionStatus.AWAITING_APPROVAL.value == "awaiting_approval"
    assert SessionStatus.ERROR.value == "error"
    assert SessionStatus.CLOSED.value == "closed"


def test_hook_event_normalizes_raw_claude_format():
    raw_data = {
        "session_id": "abc123",
        "cwd": "/path/to/project",
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "ls"},
    }
    event = HookEvent(**raw_data)
    assert event.project_path == "/path/to/project"
    assert event.event_type == "tool_call"
    assert event.tool_params == {"command": "ls"}
    assert event.tool_name == "Bash"
