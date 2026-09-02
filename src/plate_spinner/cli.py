import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import uvicorn

from .daemon.app import create_app
from .daemon.db import Database
from .codex import (
    CODEX_SESSIONS_DIR,
    CODEX_PROVIDER,
    CodexSessionInfo,
    find_codex_session,
    post_session_start,
    post_session_stop,
    tail_codex_session,
)

DAEMON_PORT = 7890
DAEMON_URL = f"http://localhost:{DAEMON_PORT}"


def get_db_path() -> Path:
    return Path.home() / ".plate-spinner" / "state.db"


def daemon_running() -> bool:
    try:
        httpx.get(f"{DAEMON_URL}/health", timeout=1)
        return True
    except httpx.RequestError:
        return False


def _ensure_daemon_running() -> None:
    if daemon_running():
        return
    subprocess.Popen(
        [sys.executable, "-m", "plate_spinner.cli", "daemon"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env=os.environ.copy(),
    )
    time.sleep(1)


def cmd_daemon(args: argparse.Namespace) -> None:
    db = Database(get_db_path())
    app = create_app(db)
    uvicorn.run(app, host="127.0.0.1", port=DAEMON_PORT, log_level="warning")


def cmd_tui(args: argparse.Namespace) -> None:
    _ensure_daemon_running()
    from .tui.app import run
    result = run()

    if result and str(result).startswith("resume:"):
        parts = str(result).split(":", 2)
        session_id = parts[1]
        project_path = parts[2] if len(parts) > 2 else None
        if project_path:
            os.chdir(project_path)
        env = os.environ.copy()
        env["PLATE_SPINNER"] = "1"
        provider = _resolve_session_provider(session_id)
        if provider == CODEX_PROVIDER and project_path:
            os.execvpe("codex", ["codex", "resume", "-C", project_path, session_id], env)
        else:
            os.execvpe("claude", ["claude", "--resume", session_id], env)


def _notify_stopped(project_path: str) -> None:
    try:
        httpx.post(
            f"{DAEMON_URL}/sessions/stopped",
            json={"project_path": project_path},
            timeout=2
        )
    except httpx.RequestError:
        pass


def _resolve_session_provider(session_id: str) -> str | None:
    try:
        response = httpx.get(f"{DAEMON_URL}/sessions", timeout=2)
        for session in response.json():
            if session.get("session_id") == session_id:
                return session.get("provider") or "claude"
    except httpx.RequestError:
        return None
    return None


def cmd_run(args: argparse.Namespace) -> None:
    _ensure_daemon_running()
    env = os.environ.copy()
    env["PLATE_SPINNER"] = "1"

    project_path = os.getcwd()

    pid = os.fork()
    if pid == 0:
        os.execvpe("claude", ["claude"] + args.claude_args, env)
    else:
        def cleanup(_sig: int, _frame: object) -> None:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            _notify_stopped(project_path)
            sys.exit(0)

        signal.signal(signal.SIGHUP, cleanup)
        signal.signal(signal.SIGTERM, cleanup)
        signal.signal(signal.SIGINT, cleanup)

        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass
        _notify_stopped(project_path)


def _wait_for_codex_session(start_time: float, existing_paths: set[Path]) -> CodexSessionInfo | None:
    return find_codex_session(start_time=start_time, existing_paths=existing_paths)


def cmd_codex(args: argparse.Namespace) -> None:
    _ensure_daemon_running()
    env = os.environ.copy()
    env["PLATE_SPINNER"] = "1"

    codex_args = getattr(args, "codex_args", [])
    if CODEX_SESSIONS_DIR.exists():
        existing_paths = {path for path in CODEX_SESSIONS_DIR.rglob("*.jsonl")}
    else:
        existing_paths = set()

    start_time = time.time()

    pid = os.fork()
    if pid == 0:
        os.execvpe("codex", ["codex"] + codex_args, env)
    else:
        stop_event = threading.Event()
        session_info: CodexSessionInfo | None = None

        def cleanup(_sig: int, _frame: object) -> None:
            stop_event.set()
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            if session_info:
                post_session_stop(DAEMON_URL, session_info.session_id, session_info.project_path)
            else:
                _notify_stopped(os.getcwd())
            sys.exit(0)

        signal.signal(signal.SIGHUP, cleanup)
        signal.signal(signal.SIGTERM, cleanup)
        signal.signal(signal.SIGINT, cleanup)

        session_info = _wait_for_codex_session(start_time, existing_paths)
        if session_info:
            post_session_start(DAEMON_URL, session_info)
            tail_thread = threading.Thread(
                target=tail_codex_session,
                args=(DAEMON_URL, session_info, stop_event.is_set),
                daemon=True,
            )
            tail_thread.start()

        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass

        stop_event.set()
        if session_info:
            post_session_stop(DAEMON_URL, session_info.session_id, session_info.project_path)
        else:
            _notify_stopped(os.getcwd())


def cmd_sessions(args: argparse.Namespace) -> None:
    try:
        response = httpx.get(f"{DAEMON_URL}/sessions", timeout=5)
        print(json.dumps(response.json(), indent=2))
    except httpx.RequestError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_kill(args: argparse.Namespace) -> None:
    # Try multiple patterns to catch daemon however it was started
    patterns = ["plate_spinner.cli daemon", "sp daemon"]
    killed = False
    for pattern in patterns:
        result = subprocess.run(["pkill", "-f", pattern], capture_output=True)
        if result.returncode == 0:
            killed = True
    if killed:
        print("Daemon stopped")
    else:
        print("No daemon running")


from .hooks import SCRIPTS_DIR, HOOK_SCRIPT_NAMES


def cmd_config(args: argparse.Namespace) -> None:
    from .config import get_config_path, load_config, save_config, Config
    import tomllib

    if args.config_command == "path":
        print(get_config_path())
    elif args.config_command == "export":
        config = load_config()
        path = get_config_path()
        if path.exists():
            print(path.read_text())
        else:
            save_config(config)
            print(path.read_text())
    elif args.config_command == "import":
        data = tomllib.loads(Path(args.file).read_text())
        config = Config.model_validate(data)
        save_config(config)
        print(f"Imported config from {args.file}")
    else:
        print(f"Config path: {get_config_path()}")


def cmd_install(args: argparse.Namespace) -> None:
    hooks_dir = Path.home() / ".plate-spinner" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)

    for name in HOOK_SCRIPT_NAMES:
        src = SCRIPTS_DIR / name
        dst = hooks_dir / name
        dst.write_text(src.read_text())
        dst.chmod(0o755)
        print(f"Installed {dst}")

    print("\nAdd to ~/.claude/settings.json:")
    print(json.dumps({
        "hooks": {
            "SessionStart": [{
                "hooks": [{
                    "type": "command",
                    "command": f'[ "$PLATE_SPINNER" = "1" ] && {hooks_dir}/session-start.sh || true'
                }]
            }],
            "PreToolUse": [{
                "matcher": "*",
                "hooks": [{
                    "type": "command",
                    "command": f'[ "$PLATE_SPINNER" = "1" ] && {hooks_dir}/pre-tool-use.sh || true'
                }]
            }],
            "PostToolUse": [{
                "matcher": "*",
                "hooks": [{
                    "type": "command",
                    "command": f'[ "$PLATE_SPINNER" = "1" ] && {hooks_dir}/post-tool-use.sh || true'
                }]
            }],
            "Stop": [{
                "hooks": [{
                    "type": "command",
                    "command": f'[ "$PLATE_SPINNER" = "1" ] && {hooks_dir}/stop.sh || true'
                }]
            }]
        }
    }, indent=2))


def main() -> None:
    # Handle 'run' specially to pass all args through to claude
    if len(sys.argv) >= 2 and sys.argv[1] == "run":
        args = argparse.Namespace(claude_args=sys.argv[2:])
        cmd_run(args)
        return
    if len(sys.argv) >= 2 and sys.argv[1] == "codex":
        args = argparse.Namespace(codex_args=sys.argv[2:])
        cmd_codex(args)
        return

    parser = argparse.ArgumentParser(prog="sp", description="Plate-Spinner")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("daemon", help="Run daemon in foreground")
    subparsers.add_parser("tui", help="Launch TUI")
    subparsers.add_parser("sessions", help="List sessions as JSON")
    subparsers.add_parser("install", help="Install hooks to ~/.plate-spinner")
    subparsers.add_parser("kill", help="Stop the daemon")
    subparsers.add_parser("run", help="Launch Claude with tracking (all args passed to claude)")
    subparsers.add_parser("codex", help="Launch Codex with tracking (all args passed to codex)")

    config_parser = subparsers.add_parser("config", help="Manage configuration")
    config_subparsers = config_parser.add_subparsers(dest="config_command")
    config_subparsers.add_parser("path", help="Print config file path")
    config_subparsers.add_parser("export", help="Export config to stdout")
    import_parser = config_subparsers.add_parser("import", help="Import config from file")
    import_parser.add_argument("file", help="Config file to import")

    args = parser.parse_args()

    if args.command == "daemon":
        cmd_daemon(args)
    elif args.command == "sessions":
        cmd_sessions(args)
    elif args.command == "install":
        cmd_install(args)
    elif args.command == "kill":
        cmd_kill(args)
    elif args.command == "codex":
        cmd_codex(args)
    elif args.command == "config":
        cmd_config(args)
    else:
        cmd_tui(args)


if __name__ == "__main__":
    main()
