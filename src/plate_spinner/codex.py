import json
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
import subprocess

import httpx


CODEX_SESSIONS_DIR = Path.home() / ".codex" / "sessions"
CODEX_PROVIDER = "codex"


@dataclass(frozen=True)
class CodexSessionInfo:
    session_id: str
    project_path: str
    transcript_path: str


def _parse_iso_timestamp(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _read_session_meta(path: Path) -> dict[str, Any] | None:
    try:
        with path.open() as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("type") == "session_meta":
                    return entry.get("payload", {})
                break
    except OSError:
        return None
    return None


def find_codex_session(
    start_time: float,
    existing_paths: set[Path],
    timeout: float = 15.0,
    poll_interval: float = 0.25,
) -> CodexSessionInfo | None:
    if not CODEX_SESSIONS_DIR.exists():
        return None

    deadline = time.time() + timeout
    while time.time() < deadline:
        candidates: list[tuple[float, Path]] = []
        for path in CODEX_SESSIONS_DIR.rglob("*.jsonl"):
            if path in existing_paths:
                continue
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            candidates.append((mtime, path))

        candidates.sort(reverse=True)
        for _, path in candidates:
            meta = _read_session_meta(path)
            if not meta:
                continue
            timestamp = _parse_iso_timestamp(meta.get("timestamp"))
            if timestamp and timestamp + 1 < start_time:
                continue
            session_id = meta.get("id")
            project_path = meta.get("cwd")
            if session_id and project_path:
                return CodexSessionInfo(
                    session_id=session_id,
                    project_path=project_path,
                    transcript_path=str(path),
                )
        time.sleep(poll_interval)
    return None


def _get_git_branch(project_path: str) -> str | None:
    try:
        branch = subprocess.check_output(
            ["git", "-C", project_path, "rev-parse", "--abbrev-ref", "HEAD"],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
        return branch or None
    except Exception:
        return None


def _post_event(daemon_url: str, payload: dict[str, Any]) -> None:
    try:
        httpx.post(f"{daemon_url}/events", json=payload, timeout=2)
    except httpx.RequestError:
        pass


def post_session_start(daemon_url: str, info: CodexSessionInfo) -> None:
    _post_event(
        daemon_url,
        {
            "session_id": info.session_id,
            "project_path": info.project_path,
            "event_type": "session_start",
            "transcript_path": info.transcript_path,
            "git_branch": _get_git_branch(info.project_path),
            "provider": CODEX_PROVIDER,
        },
    )


def post_session_stop(daemon_url: str, session_id: str, project_path: str | None) -> None:
    payload: dict[str, Any] = {
        "session_id": session_id,
        "project_path": project_path or "",
        "event_type": "stop",
        "provider": CODEX_PROVIDER,
    }
    _post_event(daemon_url, payload)


def _parse_tool_args(arguments: str | None) -> dict[str, Any] | None:
    if not arguments:
        return None
    try:
        parsed = json.loads(arguments)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


def tail_codex_session(
    daemon_url: str,
    info: CodexSessionInfo,
    should_stop: Callable[[], bool],
    poll_interval: float = 0.25,
) -> None:
    call_id_to_name: dict[str, str] = {}
    try:
        with open(info.transcript_path) as handle:
            while not should_stop():
                line = handle.readline()
                if not line:
                    time.sleep(poll_interval)
                    continue
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                entry_type = entry.get("type")
                if entry_type == "session_meta":
                    continue

                if entry_type == "event_msg":
                    payload = entry.get("payload", {})
                    event_type = payload.get("type")
                    if event_type == "user_message":
                        _post_event(
                            daemon_url,
                            {
                                "session_id": info.session_id,
                                "project_path": info.project_path,
                                "event_type": "user_message",
                                "provider": CODEX_PROVIDER,
                            },
                        )
                    elif event_type == "agent_message":
                        _post_event(
                            daemon_url,
                            {
                                "session_id": info.session_id,
                                "project_path": info.project_path,
                                "event_type": "agent_message",
                                "provider": CODEX_PROVIDER,
                            },
                        )
                    elif event_type == "entered_review_mode":
                        _post_event(
                            daemon_url,
                            {
                                "session_id": info.session_id,
                                "project_path": info.project_path,
                                "event_type": "review_mode",
                                "provider": CODEX_PROVIDER,
                            },
                        )
                    elif event_type == "exited_review_mode":
                        _post_event(
                            daemon_url,
                            {
                                "session_id": info.session_id,
                                "project_path": info.project_path,
                                "event_type": "review_mode_exit",
                                "provider": CODEX_PROVIDER,
                            },
                        )
                    continue

                if entry_type == "response_item":
                    payload = entry.get("payload", {})
                    item_type = payload.get("type")
                    if item_type == "function_call":
                        tool_name = payload.get("name")
                        call_id = payload.get("call_id")
                        if call_id and tool_name:
                            call_id_to_name[call_id] = tool_name
                        _post_event(
                            daemon_url,
                            {
                                "session_id": info.session_id,
                                "project_path": info.project_path,
                                "event_type": "tool_start",
                                "tool_name": tool_name,
                                "tool_params": _parse_tool_args(payload.get("arguments")),
                                "provider": CODEX_PROVIDER,
                            },
                        )
                    elif item_type == "function_call_output":
                        call_id = payload.get("call_id")
                        tool_name = call_id_to_name.get(call_id)
                        _post_event(
                            daemon_url,
                            {
                                "session_id": info.session_id,
                                "project_path": info.project_path,
                                "event_type": "tool_call",
                                "tool_name": tool_name,
                                "provider": CODEX_PROVIDER,
                            },
                        )
    except OSError:
        return
