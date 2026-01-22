import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from plate_spinner.daemon.summarizer import get_api_key, summarize_session


class TestGetApiKey:
    def test_returns_env_var_if_set(self):
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-test-key"}):
            assert get_api_key() == "sk-test-key"

    def test_reads_from_config_file(self, tmp_path):
        config_dir = tmp_path / ".plate-spinner"
        config_dir.mkdir()
        config_file = config_dir / "config"
        config_file.write_text("ANTHROPIC_API_KEY=sk-from-config\n")

        with patch.dict("os.environ", {}, clear=True):
            with patch("plate_spinner.daemon.summarizer.Path.home", return_value=tmp_path):
                assert get_api_key() == "sk-from-config"

    def test_returns_none_if_no_key(self, tmp_path):
        with patch.dict("os.environ", {}, clear=True):
            with patch("plate_spinner.daemon.summarizer.Path.home", return_value=tmp_path):
                assert get_api_key() is None


class TestSummarizeSession:
    def test_returns_none_without_api_key(self, tmp_path):
        transcript = tmp_path / "test.jsonl"
        transcript.write_text('{"type": "user", "message": {"content": "hello"}}\n')

        with patch("plate_spinner.daemon.summarizer.get_api_key", return_value=None):
            assert summarize_session(str(transcript)) is None

    def test_returns_none_for_missing_transcript(self):
        with patch("plate_spinner.daemon.summarizer.get_api_key", return_value="sk-test"):
            assert summarize_session("/nonexistent/path.jsonl") is None

    def test_returns_none_for_none_transcript(self):
        with patch("plate_spinner.daemon.summarizer.get_api_key", return_value="sk-test"):
            assert summarize_session(None) is None

    def test_returns_none_for_empty_transcript(self, tmp_path):
        transcript = tmp_path / "empty.jsonl"
        transcript.write_text("")

        with patch("plate_spinner.daemon.summarizer.get_api_key", return_value="sk-test"):
            assert summarize_session(str(transcript)) is None

    def test_parses_user_messages(self, tmp_path):
        transcript = tmp_path / "test.jsonl"
        transcript.write_text(
            '{"type": "user", "message": {"content": "hello world"}}\n'
            '{"type": "assistant", "message": {"content": [{"type": "text", "text": "hi there"}]}}\n'
        )

        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="Greeting exchange")]

        with patch("plate_spinner.daemon.summarizer.get_api_key", return_value="sk-test"):
            with patch("plate_spinner.daemon.summarizer.Anthropic") as mock_anthropic:
                mock_client = MagicMock()
                mock_client.messages.create.return_value = mock_response
                mock_anthropic.return_value = mock_client

                result = summarize_session(str(transcript))

                assert result == "Greeting exchange"
                call_args = mock_client.messages.create.call_args
                prompt = call_args.kwargs["messages"][0]["content"]
                assert "User: hello world" in prompt
                assert "Assistant: hi there" in prompt

    def test_parses_tool_use_blocks(self, tmp_path):
        transcript = tmp_path / "test.jsonl"
        transcript.write_text(
            '{"type": "user", "message": {"content": "read the file"}}\n'
            '{"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read"}]}}\n'
        )

        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="File reading task")]

        with patch("plate_spinner.daemon.summarizer.get_api_key", return_value="sk-test"):
            with patch("plate_spinner.daemon.summarizer.Anthropic") as mock_anthropic:
                mock_client = MagicMock()
                mock_client.messages.create.return_value = mock_response
                mock_anthropic.return_value = mock_client

                result = summarize_session(str(transcript))

                call_args = mock_client.messages.create.call_args
                prompt = call_args.kwargs["messages"][0]["content"]
                assert "Tool: Read" in prompt

    def test_skips_non_message_entries(self, tmp_path):
        transcript = tmp_path / "test.jsonl"
        transcript.write_text(
            '{"type": "file-history-snapshot", "snapshot": {}}\n'
            '{"type": "progress", "sessionId": "123"}\n'
            '{"type": "user", "message": {"content": "actual message"}}\n'
        )

        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="Test summary")]

        with patch("plate_spinner.daemon.summarizer.get_api_key", return_value="sk-test"):
            with patch("plate_spinner.daemon.summarizer.Anthropic") as mock_anthropic:
                mock_client = MagicMock()
                mock_client.messages.create.return_value = mock_response
                mock_anthropic.return_value = mock_client

                result = summarize_session(str(transcript))

                call_args = mock_client.messages.create.call_args
                prompt = call_args.kwargs["messages"][0]["content"]
                assert "actual message" in prompt
                assert "file-history-snapshot" not in prompt
                assert "progress" not in prompt

    def test_handles_malformed_json_lines(self, tmp_path):
        transcript = tmp_path / "test.jsonl"
        transcript.write_text(
            '{"type": "user", "message": {"content": "valid message"}}\n'
            'not valid json\n'
            '{"type": "assistant", "message": {"content": [{"type": "text", "text": "response"}]}}\n'
        )

        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="Summary")]

        with patch("plate_spinner.daemon.summarizer.get_api_key", return_value="sk-test"):
            with patch("plate_spinner.daemon.summarizer.Anthropic") as mock_anthropic:
                mock_client = MagicMock()
                mock_client.messages.create.return_value = mock_response
                mock_anthropic.return_value = mock_client

                result = summarize_session(str(transcript))
                assert result == "Summary"

    def test_uses_last_15_messages(self, tmp_path):
        transcript = tmp_path / "test.jsonl"
        lines = []
        for i in range(20):
            lines.append(f'{{"type": "user", "message": {{"content": "message {i}"}}}}\n')
        transcript.write_text("".join(lines))

        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="Summary")]

        with patch("plate_spinner.daemon.summarizer.get_api_key", return_value="sk-test"):
            with patch("plate_spinner.daemon.summarizer.Anthropic") as mock_anthropic:
                mock_client = MagicMock()
                mock_client.messages.create.return_value = mock_response
                mock_anthropic.return_value = mock_client

                summarize_session(str(transcript))

                call_args = mock_client.messages.create.call_args
                prompt = call_args.kwargs["messages"][0]["content"]
                assert "message 5" in prompt
                assert "message 19" in prompt
                assert "message 0" not in prompt

    def test_truncates_long_messages(self, tmp_path):
        transcript = tmp_path / "test.jsonl"
        long_message = "x" * 500
        transcript.write_text(f'{{"type": "user", "message": {{"content": "{long_message}"}}}}\n')

        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="Summary")]

        with patch("plate_spinner.daemon.summarizer.get_api_key", return_value="sk-test"):
            with patch("plate_spinner.daemon.summarizer.Anthropic") as mock_anthropic:
                mock_client = MagicMock()
                mock_client.messages.create.return_value = mock_response
                mock_anthropic.return_value = mock_client

                summarize_session(str(transcript))

                call_args = mock_client.messages.create.call_args
                prompt = call_args.kwargs["messages"][0]["content"]
                assert len(prompt) < 500

    def test_calls_haiku_model(self, tmp_path):
        transcript = tmp_path / "test.jsonl"
        transcript.write_text('{"type": "user", "message": {"content": "test"}}\n')

        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="Summary")]

        with patch("plate_spinner.daemon.summarizer.get_api_key", return_value="sk-test"):
            with patch("plate_spinner.daemon.summarizer.Anthropic") as mock_anthropic:
                mock_client = MagicMock()
                mock_client.messages.create.return_value = mock_response
                mock_anthropic.return_value = mock_client

                summarize_session(str(transcript))

                call_args = mock_client.messages.create.call_args
                assert call_args.kwargs["model"] == "claude-3-5-haiku-latest"
                assert call_args.kwargs["max_tokens"] == 30

    def test_parses_codex_messages(self, tmp_path):
        transcript = tmp_path / "codex.jsonl"
        transcript.write_text(
            '{"type": "session_meta", "payload": {"id": "abc", "cwd": "/tmp"}}\n'
            '{"type": "event_msg", "payload": {"type": "user_message", "message": "please help"}}\n'
            '{"type": "response_item", "payload": {"type": "function_call", "name": "shell_command"}}\n'
            '{"type": "event_msg", "payload": {"type": "agent_message", "message": "working on it"}}\n'
        )

        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="Codex summary")]

        with patch("plate_spinner.daemon.summarizer.get_api_key", return_value="sk-test"):
            with patch("plate_spinner.daemon.summarizer.Anthropic") as mock_anthropic:
                mock_client = MagicMock()
                mock_client.messages.create.return_value = mock_response
                mock_anthropic.return_value = mock_client

                result = summarize_session(str(transcript))

                assert result == "Codex summary"
                call_args = mock_client.messages.create.call_args
                prompt = call_args.kwargs["messages"][0]["content"]
                assert "User: please help" in prompt
                assert "Assistant: working on it" in prompt
                assert "Tool: shell_command" in prompt
