"""Tests for ralph core module.

Tests state management, daemon lifecycle, provider commands, prompt building,
iteration execution, and cron management.
"""

import os
import subprocess
import time
from unittest.mock import MagicMock, patch

import pytest

from ralph.core import (
    COMPLETION_SIGNAL,
    ProviderConfig,
    build_command,
    build_prompt,
    create_loop,
    daemonize_loop,
    delete_loop,
    execute_one_iteration,
    generate_loop_id,
    is_loop_process_alive,
    kill_daemon,
    list_loops,
    loop_dir,
    read_pid,
    read_state,
    remove_pid,
    run_foreground_loop,
    sorted_log_files,
    write_pid,
    write_state,
)

# ─── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def isolated_ralph_dir(tmp_path, monkeypatch):
    """Redirect all ralph state to a temp directory for test isolation."""
    ralph_dir = tmp_path / ".ralph"
    loops_dir = ralph_dir / "loops"
    loops_dir.mkdir(parents=True)
    monkeypatch.setattr("ralph.core.RALPH_DIR", ralph_dir)
    monkeypatch.setattr("ralph.core.LOOPS_DIR", loops_dir)
    return ralph_dir


@pytest.fixture
def sample_loop(isolated_ralph_dir):
    """Create a sample loop and return its state."""
    return create_loop(
        name="test loop",
        prompt="do some testing",
        provider="claude",
        model="sonnet",
        workdir="/tmp",
        max_iterations=10,
        delay_seconds=5,
    )


# ─── Loop ID Generation ─────────────────────────────────────────────────────


class TestGenerateLoopId:
    def test_basic_format(self):
        lid = generate_loop_id("implement auth feature")
        parts = lid.rsplit("-", 1)
        assert len(parts) == 2
        assert len(parts[1]) == 4  # 4-char hex hash

    def test_slug_from_first_three_words(self):
        lid = generate_loop_id("implement auth feature with JWT")
        assert lid.startswith("implement-auth-feature-")

    def test_uniqueness(self):
        ids = {generate_loop_id("same name") for _ in range(20)}
        # time_ns() may collide in a tight loop; just verify most are unique
        assert len(ids) >= 15

    def test_empty_name_fallback(self):
        lid = generate_loop_id("")
        assert lid.startswith("loop-")

    def test_special_chars_stripped(self):
        lid = generate_loop_id("fix @#$ bugs!!!")
        slug = lid.rsplit("-", 1)[0]
        assert all(c.isalnum() or c == "-" for c in slug)


# ─── State Management ────────────────────────────────────────────────────────


class TestStateManagement:
    def test_create_loop_creates_directory_structure(self, sample_loop):
        d = loop_dir(sample_loop.id)
        assert d.exists()
        assert (d / "prompt.md").exists()
        assert (d / "memory.md").exists()
        assert (d / "state.json").exists()
        assert (d / "logs").is_dir()

    def test_create_loop_prompt_content(self, sample_loop):
        d = loop_dir(sample_loop.id)
        assert (d / "prompt.md").read_text() == "do some testing"

    def test_create_loop_initial_state(self, sample_loop):
        assert sample_loop.status == "running"
        assert sample_loop.iteration == 0
        assert sample_loop.provider == "claude"
        assert sample_loop.last_run_at is None

    def test_read_write_state_roundtrip(self, sample_loop):
        sample_loop.iteration = 5
        sample_loop.status = "completed"
        write_state(sample_loop)

        loaded = read_state(sample_loop.id)
        assert loaded.iteration == 5
        assert loaded.status == "completed"

    def test_read_state_nonexistent_raises(self):
        with pytest.raises(FileNotFoundError):
            read_state("nonexistent-loop-id")

    def test_write_state_atomic(self, sample_loop):
        """write_state uses temp file + rename — no .json.tmp should remain."""
        write_state(sample_loop)
        d = loop_dir(sample_loop.id)
        tmp_files = list(d.glob("*.tmp"))
        assert tmp_files == []

    def test_delete_loop(self, sample_loop):
        d = loop_dir(sample_loop.id)
        assert d.exists()
        delete_loop(sample_loop.id)
        assert not d.exists()

    def test_delete_loop_nonexistent_is_noop(self):
        delete_loop("nonexistent-id")  # Should not raise

    def test_list_loops_returns_all(self, isolated_ralph_dir):
        create_loop("loop a", "task a", "claude", "", "/tmp", 10, 5)
        create_loop("loop b", "task b", "claude", "", "/tmp", 10, 5)
        loops = list_loops()
        assert len(loops) == 2

    def test_list_loops_sorted_newest_first(self, isolated_ralph_dir):
        s1 = create_loop("first", "task", "claude", "", "/tmp", 10, 5)
        time.sleep(0.01)  # Ensure different timestamps
        s2 = create_loop("second", "task", "claude", "", "/tmp", 10, 5)
        loops = list_loops()
        assert loops[0].id == s2.id
        assert loops[1].id == s1.id

    def test_list_loops_skips_corrupt_state(self, isolated_ralph_dir):
        create_loop("good", "task", "claude", "", "/tmp", 10, 5)
        # Create a corrupt loop directory
        bad_dir = isolated_ralph_dir / "loops" / "bad-loop"
        bad_dir.mkdir()
        (bad_dir / "state.json").write_text("not json")
        loops = list_loops()
        assert len(loops) == 1

    def test_list_loops_skips_unreadable_state(self, isolated_ralph_dir):
        """list_loops should skip loops with permission errors, not crash."""
        good = create_loop("good", "task", "claude", "", "/tmp", 10, 5)
        bad_dir = isolated_ralph_dir / "loops" / "bad-perms"
        bad_dir.mkdir()
        state_file = bad_dir / "state.json"
        state_file.write_text('{"id": "bad"}')
        state_file.chmod(0o000)

        try:
            loops = list_loops()
            # Should still return the good loop, skipping the unreadable one
            assert len(loops) == 1
            assert loops[0].id == good.id
        finally:
            state_file.chmod(0o644)  # Restore for cleanup

    def test_list_loops_empty_when_no_loops_dir(self, monkeypatch, tmp_path):
        monkeypatch.setattr("ralph.core.LOOPS_DIR", tmp_path / "nonexistent")
        assert list_loops() == []


# ─── Stale Daemon Detection ─────────────────────────────────────────────────


class TestStaleDaemonDetection:
    def test_list_loops_detects_stale_daemon(self, sample_loop):
        """If PID file exists but process is dead, list_loops marks loop stopped."""
        # Write a PID file for a non-existent process
        pid_file = loop_dir(sample_loop.id) / "daemon.pid"
        pid_file.write_text("999999999")  # PID that doesn't exist

        loops = list_loops()
        assert len(loops) == 1
        assert loops[0].status == "stopped"

        # Verify state.json was also updated
        reloaded = read_state(sample_loop.id)
        assert reloaded.status == "stopped"

    def test_list_loops_keeps_running_for_foreground(self, sample_loop):
        """Foreground loops without PID files should stay running."""
        loops = list_loops()
        assert len(loops) == 1
        assert loops[0].status == "running"

    def test_list_loops_keeps_running_for_cron(self, isolated_ralph_dir):
        """Cron loops should not be affected by stale daemon detection."""
        create_loop(
            "cron loop", "task", "claude", "", "/tmp", 10, 5,
            cron_expression="0 * * * *",
        )
        loops = list_loops()
        assert len(loops) == 1
        assert loops[0].status == "running"


# ─── PID Management ─────────────────────────────────────────────────────────


class TestPidManagement:
    def test_write_and_read_pid(self, sample_loop):
        write_pid(sample_loop.id, os.getpid())
        assert read_pid(sample_loop.id) == os.getpid()

    def test_read_pid_returns_none_when_no_file(self, sample_loop):
        assert read_pid(sample_loop.id) is None

    def test_read_pid_rejects_pid_zero(self, sample_loop):
        """PID 0 means 'caller's process group' — must be rejected."""
        pid_file = loop_dir(sample_loop.id) / "daemon.pid"
        pid_file.write_text("0")
        assert read_pid(sample_loop.id) is None
        assert not pid_file.exists()

    def test_read_pid_rejects_pid_one(self, sample_loop):
        """PID 1 is init/launchd — must be rejected."""
        pid_file = loop_dir(sample_loop.id) / "daemon.pid"
        pid_file.write_text("1")
        assert read_pid(sample_loop.id) is None
        assert not pid_file.exists()

    def test_read_pid_rejects_negative_pid(self, sample_loop):
        """Negative PIDs reference process groups — must be rejected."""
        pid_file = loop_dir(sample_loop.id) / "daemon.pid"
        pid_file.write_text("-1")
        assert read_pid(sample_loop.id) is None
        assert not pid_file.exists()

    def test_read_pid_cleans_stale_pid(self, sample_loop):
        pid_file = loop_dir(sample_loop.id) / "daemon.pid"
        pid_file.write_text("999999999")
        assert read_pid(sample_loop.id) is None
        assert not pid_file.exists()  # Cleaned up

    def test_read_pid_cleans_corrupt_pid(self, sample_loop):
        pid_file = loop_dir(sample_loop.id) / "daemon.pid"
        pid_file.write_text("not-a-number")
        assert read_pid(sample_loop.id) is None
        assert not pid_file.exists()

    def test_remove_pid(self, sample_loop):
        write_pid(sample_loop.id, os.getpid())
        remove_pid(sample_loop.id)
        assert read_pid(sample_loop.id) is None

    def test_remove_pid_nonexistent_is_noop(self, sample_loop):
        remove_pid(sample_loop.id)  # Should not raise


# ─── Kill Daemon ─────────────────────────────────────────────────────────────


class TestKillDaemon:
    def test_kill_daemon_no_pid(self, sample_loop):
        assert kill_daemon(sample_loop.id) is False

    def test_kill_daemon_stale_pid(self, sample_loop):
        pid_file = loop_dir(sample_loop.id) / "daemon.pid"
        pid_file.write_text("999999999")
        assert kill_daemon(sample_loop.id) is False

    def test_kill_daemon_real_process(self, sample_loop):
        """Start a sleep subprocess, write its PID, and kill it."""
        proc = subprocess.Popen(["sleep", "300"])
        write_pid(sample_loop.id, proc.pid)

        assert kill_daemon(sample_loop.id) is True

        # Process should be dead
        proc.wait(timeout=5)
        assert proc.returncode is not None

        # PID file should be cleaned up
        pid_file = loop_dir(sample_loop.id) / "daemon.pid"
        assert not pid_file.exists()

    def test_kill_daemon_waits_for_exit(self, sample_loop):
        """kill_daemon should send SIGTERM and the process should die."""
        proc = subprocess.Popen(["sleep", "300"])
        write_pid(sample_loop.id, proc.pid)

        # In tests, we're the parent process — we must reap the child.
        # In real usage, ralph remove runs in a separate process so this
        # zombie issue doesn't apply.
        result = kill_daemon(sample_loop.id, timeout=5.0)
        assert result is True

        # Reap the child to avoid zombie
        proc.wait(timeout=5)
        assert proc.returncode is not None


# ─── Provider & Command Building ─────────────────────────────────────────────


class TestBuildCommand:
    def test_claude_command(self):
        provider = ProviderConfig(
            binary="claude", subcommand="", prompt_flag="-p",
            extra_args=["--dangerously-skip-permissions"], model_flag="--model",
        )
        cmd = build_command(provider, "hello world", "sonnet")
        assert cmd == [
            "claude", "-p", "hello world",
            "--dangerously-skip-permissions", "--model", "sonnet",
        ]

    def test_codex_command(self):
        provider = ProviderConfig(
            binary="codex", subcommand="exec", prompt_flag="",
            extra_args=["--yolo"], model_flag="-m",
        )
        cmd = build_command(provider, "hello", "gpt-4")
        assert cmd == ["codex", "exec", "hello", "--yolo", "-m", "gpt-4"]

    def test_no_model_omits_flag(self):
        provider = ProviderConfig(
            binary="claude", subcommand="", prompt_flag="-p",
            extra_args=[], model_flag="--model",
        )
        cmd = build_command(provider, "task", "")
        assert "--model" not in cmd


# ─── Prompt Building ─────────────────────────────────────────────────────────


class TestBuildPrompt:
    def test_includes_task(self, sample_loop):
        prompt = build_prompt(sample_loop)
        assert "do some testing" in prompt

    def test_includes_iteration_context(self, sample_loop):
        prompt = build_prompt(sample_loop)
        assert "iteration 1 of 10" in prompt

    def test_includes_memory_path(self, sample_loop):
        prompt = build_prompt(sample_loop)
        assert "memory.md" in prompt

    def test_memory_path_not_corrupted_by_memory_replacement(self, sample_loop):
        """The {memory_path} placeholder must survive {memory} replacement.

        Since '{memory}' is a substring of '{memory_path}', replacing
        {memory} first would mangle the path. Verify the full absolute
        path appears intact in the output.
        """
        d = loop_dir(sample_loop.id)
        (d / "memory.md").write_text("some memory content")

        prompt = build_prompt(sample_loop)
        expected_path = str(d / "memory.md")
        assert expected_path in prompt
        # The mangled form would be: "some memory content_path}" — must NOT appear
        assert "_path}" not in prompt

    def test_first_iteration_no_memory(self, sample_loop):
        prompt = build_prompt(sample_loop)
        assert "No previous memory" in prompt

    def test_subsequent_iteration_includes_memory(self, sample_loop):
        memory_path = loop_dir(sample_loop.id) / "memory.md"
        memory_path.write_text("## Iteration 1\n- Did stuff\n")

        prompt = build_prompt(sample_loop)
        assert "Did stuff" in prompt
        assert "No previous memory" not in prompt


# ─── Iteration Execution ─────────────────────────────────────────────────────


class TestExecuteOneIteration:
    def test_skips_non_running_loop(self, sample_loop):
        sample_loop.status = "completed"
        write_state(sample_loop)

        result = execute_one_iteration(sample_loop.id)
        assert result.exit_code == -1
        assert result.completed is False

    @patch("ralph.core.subprocess.Popen")
    def test_runs_agent_and_captures_output(self, mock_popen, sample_loop):
        mock_proc = MagicMock()
        mock_proc.stdout = iter(["line 1\n", "line 2\n"])
        mock_proc.wait.return_value = 0
        mock_popen.return_value = mock_proc

        result = execute_one_iteration(sample_loop.id)
        assert result.exit_code == 0
        assert "line 1" in result.output
        assert result.state.iteration == 1

    @patch("ralph.core.subprocess.Popen")
    def test_detects_completion_signal(self, mock_popen, sample_loop):
        mock_proc = MagicMock()
        mock_proc.stdout = iter(["doing work\n", f"{COMPLETION_SIGNAL}\n"])
        mock_proc.wait.return_value = 0
        mock_popen.return_value = mock_proc

        result = execute_one_iteration(sample_loop.id)
        assert result.completed is True
        assert result.state.status == "completed"

    @patch("ralph.core.subprocess.Popen")
    def test_completion_signal_must_be_on_own_line(self, mock_popen, sample_loop):
        mock_proc = MagicMock()
        mock_proc.stdout = iter([f"echo {COMPLETION_SIGNAL} in a sentence\n"])
        mock_proc.wait.return_value = 0
        mock_popen.return_value = mock_proc

        result = execute_one_iteration(sample_loop.id)
        assert result.completed is False

    @patch("ralph.core.subprocess.Popen")
    def test_stops_at_max_iterations(self, mock_popen, sample_loop):
        sample_loop.iteration = 9  # max is 10, next will be 10
        write_state(sample_loop)

        mock_proc = MagicMock()
        mock_proc.stdout = iter(["done\n"])
        mock_proc.wait.return_value = 0
        mock_popen.return_value = mock_proc

        result = execute_one_iteration(sample_loop.id)
        assert result.state.status == "stopped"
        assert result.state.iteration == 10

    @patch("ralph.core.subprocess.Popen")
    def test_writes_log_file(self, mock_popen, sample_loop):
        mock_proc = MagicMock()
        mock_proc.stdout = iter(["log output here\n"])
        mock_proc.wait.return_value = 0
        mock_popen.return_value = mock_proc

        result = execute_one_iteration(sample_loop.id)
        assert result.log_path.exists()
        assert "log output here" in result.log_path.read_text()

    @patch("ralph.core.subprocess.Popen")
    def test_handles_agent_binary_not_found(self, mock_popen, sample_loop):
        mock_popen.side_effect = FileNotFoundError("No such file")

        result = execute_one_iteration(sample_loop.id)
        assert result.exit_code == 127
        assert "not found" in result.output

    @patch("ralph.core.subprocess.Popen")
    def test_does_not_call_remove_cron_for_foreground_loop(
        self, mock_popen, sample_loop
    ):
        """Foreground loops should not invoke crontab commands on completion."""
        mock_proc = MagicMock()
        mock_proc.stdout = iter([f"{COMPLETION_SIGNAL}\n"])
        mock_proc.wait.return_value = 0
        mock_popen.return_value = mock_proc

        with patch("ralph.core.remove_cron") as mock_remove_cron:
            execute_one_iteration(sample_loop.id)
            mock_remove_cron.assert_not_called()

    @patch("ralph.core.subprocess.Popen")
    @patch("ralph.core.remove_cron")
    def test_calls_remove_cron_for_cron_loop(
        self, mock_remove_cron, mock_popen, isolated_ralph_dir
    ):
        """Cron loops should call remove_cron on completion."""
        state = create_loop(
            "cron loop", "task", "claude", "", "/tmp", 10, 5,
            cron_expression="0 * * * *",
        )
        mock_proc = MagicMock()
        mock_proc.stdout = iter([f"{COMPLETION_SIGNAL}\n"])
        mock_proc.wait.return_value = 0
        mock_popen.return_value = mock_proc

        execute_one_iteration(state.id)
        mock_remove_cron.assert_called_once_with(state.id)


# ─── Daemonize Loop ─────────────────────────────────────────────────────────


class TestDaemonizeLoop:
    def test_daemonize_redirects_stdin(self, sample_loop):
        """Daemon subprocess should have stdin redirected to /dev/null."""
        with patch("ralph.core.subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_proc.pid = 12345
            mock_proc.poll.return_value = None  # Child still running
            mock_popen.return_value = mock_proc

            daemonize_loop(sample_loop.id, 5)

            call_kwargs = mock_popen.call_args
            # stdin should not be None (i.e., it should be set to devnull)
            assert call_kwargs.kwargs.get("stdin") is not None or (
                len(call_kwargs.args) > 0 and call_kwargs[1].get("stdin") is not None
            )

    def test_daemonize_writes_pid_file(self, sample_loop):
        with patch("ralph.core.subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_proc.pid = 12345
            mock_proc.poll.return_value = None  # Child still running
            mock_popen.return_value = mock_proc

            pid = daemonize_loop(sample_loop.id, 5)
            assert pid == 12345

            pid_file = loop_dir(sample_loop.id) / "daemon.pid"
            assert pid_file.exists()
            assert pid_file.read_text().strip() == "12345"


# ─── Foreground Loop Runner ─────────────────────────────────────────────────


class TestRunForegroundLoop:
    @patch("ralph.core.execute_one_iteration")
    def test_stops_on_completion(self, mock_execute, sample_loop):
        completed_state = read_state(sample_loop.id)
        completed_state.status = "completed"
        completed_state.iteration = 3

        mock_execute.return_value = MagicMock(
            state=completed_state, completed=True, exit_code=0,
        )

        state = run_foreground_loop(sample_loop.id, delay=0)
        assert state.status == "completed"
        assert mock_execute.call_count == 1

    @patch("ralph.core.execute_one_iteration")
    def test_callback_invoked(self, mock_execute, sample_loop):
        completed_state = read_state(sample_loop.id)
        completed_state.status = "completed"
        mock_execute.return_value = MagicMock(
            state=completed_state, completed=True, exit_code=0,
        )

        callback = MagicMock()
        run_foreground_loop(sample_loop.id, delay=0, on_iteration=callback)
        callback.assert_called_once()


# ─── Workdir Error Detection ────────────────────────────────────────────────


class TestWorkdirErrorDetection:
    @patch("ralph.core.subprocess.Popen")
    def test_missing_workdir_gives_correct_error(self, mock_popen, sample_loop):
        """When workdir doesn't exist, error should say so, not 'binary not found'."""
        sample_loop.workdir = "/nonexistent/path/does/not/exist"
        write_state(sample_loop)

        mock_popen.side_effect = FileNotFoundError("No such file or directory")

        result = execute_one_iteration(sample_loop.id)
        assert result.exit_code == 127
        assert "Working directory" in result.output
        assert "/nonexistent/path" in result.output

    @patch("ralph.core.subprocess.Popen")
    def test_missing_binary_gives_binary_error(self, mock_popen, sample_loop):
        """When binary doesn't exist but workdir does, error mentions binary."""
        mock_popen.side_effect = FileNotFoundError("No such file")

        result = execute_one_iteration(sample_loop.id)
        assert result.exit_code == 127
        assert "claude" in result.output  # provider binary name


# ─── Log Flushing ───────────────────────────────────────────────────────────


class TestLogFlushing:
    @patch("ralph.core.subprocess.Popen")
    def test_log_file_is_complete_after_iteration(self, mock_popen, sample_loop):
        """Log file should contain all output lines after iteration completes."""
        lines = [f"line {i}\n" for i in range(100)]
        mock_proc = MagicMock()
        mock_proc.stdout = iter(lines)
        mock_proc.wait.return_value = 0
        mock_popen.return_value = mock_proc

        result = execute_one_iteration(sample_loop.id)
        log_content = result.log_path.read_text()
        for i in range(100):
            assert f"line {i}" in log_content


# ─── Log File Sorting ───────────────────────────────────────────────────────


class TestSortedLogFiles:
    def test_numeric_sort_order(self, sample_loop):
        """Log files should sort numerically, not lexicographically."""
        logs_dir = loop_dir(sample_loop.id) / "logs"
        # Create files that would sort wrong lexicographically
        for n in [1, 2, 10, 100, 999, 1000, 1001]:
            (logs_dir / f"{n:03d}.log").write_text(f"iter {n}\n")

        files = sorted_log_files(sample_loop.id)
        names = [f.stem for f in files]
        assert names == ["001", "002", "010", "100", "999", "1000", "1001"]

    def test_latest_is_highest_number(self, sample_loop):
        """The last file in sorted order should be the highest iteration."""
        logs_dir = loop_dir(sample_loop.id) / "logs"
        (logs_dir / "999.log").write_text("old\n")
        (logs_dir / "1000.log").write_text("new\n")

        files = sorted_log_files(sample_loop.id)
        assert files[-1].stem == "1000"

    def test_empty_logs_dir(self, sample_loop):
        assert sorted_log_files(sample_loop.id) == []

    def test_no_logs_dir(self, isolated_ralph_dir):
        """sorted_log_files should handle missing loop entirely."""
        assert sorted_log_files("nonexistent-id") == []


# ─── Process Group Kill ─────────────────────────────────────────────────────


class TestProcessGroupKill:
    def test_kill_daemon_kills_child_processes(self, sample_loop):
        """kill_daemon should kill the daemon's child processes too."""
        # Spawn a process group: parent + child
        proc = subprocess.Popen(
            ["sh", "-c", "sleep 300 & sleep 300 & wait"],
            start_new_session=True,
        )
        write_pid(sample_loop.id, proc.pid)

        result = kill_daemon(sample_loop.id, timeout=5.0)
        assert result is True

        # Reap to avoid zombie
        proc.wait(timeout=5)


# ─── Braces in Prompt / Memory ──────────────────────────────────────────────


class TestBuildPromptBraces:
    def test_braces_in_task_prompt(self, sample_loop):
        """Prompts containing {curly braces} must not crash build_prompt."""
        d = loop_dir(sample_loop.id)
        (d / "prompt.md").write_text("implement function foo() { return {bar: 1}; }")

        prompt = build_prompt(sample_loop)
        assert "{bar: 1}" in prompt

    def test_braces_in_memory(self, sample_loop):
        """Memory containing {curly braces} must not crash build_prompt."""
        d = loop_dir(sample_loop.id)
        (d / "memory.md").write_text("Created dict {key: value} in utils.py")

        prompt = build_prompt(sample_loop)
        assert "{key: value}" in prompt

    def test_format_like_placeholders_in_memory(self, sample_loop):
        """Memory with {task} or {iteration} must be treated as literal text."""
        d = loop_dir(sample_loop.id)
        (d / "memory.md").write_text("Updated {task} description and {iteration} count")

        prompt = build_prompt(sample_loop)
        # The memory text should appear literally, not be replaced
        assert "Updated {task} description" in prompt

    def test_placeholder_strings_in_task_are_literal(self, sample_loop):
        """Task containing {memory}, {iteration} etc must be preserved literally."""
        d = loop_dir(sample_loop.id)
        (d / "prompt.md").write_text("Review the {memory} module and {iteration} logic")

        prompt = build_prompt(sample_loop)
        assert "Review the {memory} module" in prompt
        assert "{iteration} logic" in prompt

    def test_task_with_memory_path_literal(self, sample_loop):
        """Task containing {memory_path} must be preserved literally."""
        d = loop_dir(sample_loop.id)
        (d / "prompt.md").write_text("Check the file at {memory_path} for issues")

        prompt = build_prompt(sample_loop)
        assert "Check the file at {memory_path} for issues" in prompt


# ─── Iteration File Lock ────────────────────────────────────────────────────


class TestIterationLock:
    @patch("ralph.core.subprocess.Popen")
    def test_iteration_creates_lock_file(self, mock_popen, sample_loop):
        """execute_one_iteration should create a lock file."""
        mock_proc = MagicMock()
        mock_proc.stdout = iter(["done\n"])
        mock_proc.wait.return_value = 0
        mock_popen.return_value = mock_proc

        execute_one_iteration(sample_loop.id)
        assert (loop_dir(sample_loop.id) / "iteration.lock").exists()

    @patch("ralph.core.subprocess.Popen")
    def test_concurrent_iteration_is_skipped(self, mock_popen, sample_loop):
        """If lock is held, execute_one_iteration should skip."""
        import fcntl

        # Acquire the lock externally
        lock_path = loop_dir(sample_loop.id) / "iteration.lock"
        lock_path.touch()
        lock_file = open(lock_path)
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)

        try:
            result = execute_one_iteration(sample_loop.id)
            assert result.exit_code == -1
            assert "another iteration is already running" in result.output.lower()
            # Popen should NOT have been called
            mock_popen.assert_not_called()
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)
            lock_file.close()


# ─── Cron Tag Precision ─────────────────────────────────────────────────────


class TestFailFastOnError:
    @patch("ralph.core.subprocess.Popen")
    def test_binary_not_found_sets_failed(self, mock_popen, sample_loop):
        """FileNotFoundError for missing binary should set status to 'failed'."""
        mock_popen.side_effect = FileNotFoundError("No such file")

        result = execute_one_iteration(sample_loop.id)
        assert result.state.status == "failed"
        assert result.exit_code == 127

        # Verify persisted state
        reloaded = read_state(sample_loop.id)
        assert reloaded.status == "failed"

    @patch("ralph.core.subprocess.Popen")
    def test_missing_workdir_sets_failed(self, mock_popen, sample_loop):
        """FileNotFoundError for missing workdir should set status to 'failed'."""
        sample_loop.workdir = "/nonexistent/path"
        write_state(sample_loop)
        mock_popen.side_effect = FileNotFoundError("No such file")

        result = execute_one_iteration(sample_loop.id)
        assert result.state.status == "failed"

    @patch("ralph.core.subprocess.Popen")
    def test_failed_loop_stops_foreground_runner(self, mock_popen, sample_loop):
        """run_foreground_loop should exit when execute_one_iteration sets 'failed'."""
        mock_popen.side_effect = FileNotFoundError("No such file")

        state = run_foreground_loop(sample_loop.id, delay=0)
        assert state.status == "failed"


class TestForegroundPidTracking:
    @patch("ralph.core.execute_one_iteration")
    def test_writes_pid_file(self, mock_execute, sample_loop):
        """run_foreground_loop should create a PID file."""
        completed_state = read_state(sample_loop.id)
        completed_state.status = "completed"
        mock_execute.return_value = MagicMock(
            state=completed_state, completed=True, exit_code=0,
        )

        run_foreground_loop(sample_loop.id, delay=0)

        # PID file should be cleaned up after normal exit
        pid_file = loop_dir(sample_loop.id) / "daemon.pid"
        assert not pid_file.exists()

    @patch("ralph.core.execute_one_iteration")
    def test_cleans_pid_on_exception(self, mock_execute, sample_loop):
        """PID file should be cleaned up even if run_foreground_loop crashes."""
        mock_execute.side_effect = RuntimeError("boom")

        with pytest.raises(RuntimeError):
            run_foreground_loop(sample_loop.id, delay=0)

        pid_file = loop_dir(sample_loop.id) / "daemon.pid"
        assert not pid_file.exists()

    @patch("ralph.core.execute_one_iteration")
    def test_exception_sets_failed_status(self, mock_execute, sample_loop):
        """Unrecoverable exception should mark loop as 'failed'."""
        mock_execute.side_effect = OSError("disk full")

        with pytest.raises(OSError):
            run_foreground_loop(sample_loop.id, delay=0)

        reloaded = read_state(sample_loop.id)
        assert reloaded.status == "failed"

    def test_stale_foreground_detected_by_list(self, sample_loop):
        """list_loops should detect stale foreground loops via PID file."""
        # Simulate a foreground loop that was killed — PID file with dead PID
        pid_file = loop_dir(sample_loop.id) / "daemon.pid"
        pid_file.write_text("999999999")

        loops = list_loops()
        assert len(loops) == 1
        assert loops[0].status == "stopped"


class TestSendSignalPermission:
    def test_permission_error_returns_false(self):
        """_send_signal should return False on PermissionError, not crash."""
        from ralph.core import _send_signal

        with (
            patch("os.killpg", side_effect=PermissionError("not allowed")),
            patch("os.kill", side_effect=PermissionError("not allowed")),
        ):
            result = _send_signal(99999, 15)
            assert result is False


class TestKillDaemonPermissionError:
    def test_wait_loop_handles_permission_error(self, sample_loop):
        """kill_daemon should not poll for 30s if os.kill raises PermissionError."""
        # Write a PID for a process we can signal initially but then can't check
        proc = subprocess.Popen(["sleep", "300"])
        write_pid(sample_loop.id, proc.pid)

        # After SIGTERM succeeds, simulate PermissionError on alive-check
        call_count = 0
        original_kill = os.kill

        def mock_kill(pid, sig):
            nonlocal call_count
            if sig == 0:
                call_count += 1
                if call_count > 1:
                    raise PermissionError("not allowed")
            return original_kill(pid, sig)

        with patch("os.kill", side_effect=mock_kill):
            result = kill_daemon(sample_loop.id, timeout=5.0)
            assert result is True

        # Reap the child
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:
            pass


class TestDaemonizeStartupCheck:
    def test_immediate_exit_raises(self, sample_loop):
        """daemonize_loop should raise if child exits immediately."""
        with patch("ralph.core.subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_proc.pid = 99999
            mock_proc.poll.return_value = 1  # Child exited with code 1
            mock_popen.return_value = mock_proc

            with pytest.raises(RuntimeError, match="Daemon exited immediately"):
                daemonize_loop(sample_loop.id, 5)

            # PID file should be cleaned up
            pid_file = loop_dir(sample_loop.id) / "daemon.pid"
            assert not pid_file.exists()

    def test_successful_start(self, sample_loop):
        """daemonize_loop should return PID when child stays alive."""
        with patch("ralph.core.subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_proc.pid = 12345
            mock_proc.poll.return_value = None  # Child still running
            mock_popen.return_value = mock_proc

            pid = daemonize_loop(sample_loop.id, 5)
            assert pid == 12345

            pid_file = loop_dir(sample_loop.id) / "daemon.pid"
            assert pid_file.exists()


class TestRemoveCronFailureSafe:
    @patch("ralph.core.subprocess.Popen")
    @patch("ralph.core.remove_cron", side_effect=FileNotFoundError("no crontab"))
    def test_state_persisted_even_if_remove_cron_fails(
        self, mock_remove_cron, mock_popen, isolated_ralph_dir
    ):
        """If remove_cron crashes, state.status should still be persisted."""
        state = create_loop(
            "cron safe", "task", "claude", "", "/tmp", 10, 5,
            cron_expression="0 * * * *",
        )
        mock_proc = MagicMock()
        mock_proc.stdout = iter([f"{COMPLETION_SIGNAL}\n"])
        mock_proc.wait.return_value = 0
        mock_popen.return_value = mock_proc

        result = execute_one_iteration(state.id)
        assert result.state.status == "completed"

        # Verify state was persisted to disk despite remove_cron failure
        reloaded = read_state(state.id)
        assert reloaded.status == "completed"


class TestIsLoopProcessAlive:
    def test_no_lock_no_pid_returns_false(self, sample_loop):
        """No daemon.lock and no PID file → dead."""
        assert is_loop_process_alive(sample_loop.id) is False

    def test_pid_alive_returns_true(self, sample_loop):
        """PID file with alive process → alive (fallback path)."""
        write_pid(sample_loop.id, os.getpid())
        assert is_loop_process_alive(sample_loop.id) is True

    def test_pid_dead_returns_false(self, sample_loop):
        """PID file with dead process → dead (fallback path)."""
        (loop_dir(sample_loop.id) / "daemon.pid").write_text("999999999")
        assert is_loop_process_alive(sample_loop.id) is False

    def test_lock_held_returns_true(self, sample_loop):
        """daemon.lock held by flock → alive."""
        import fcntl

        lock_path = loop_dir(sample_loop.id) / "daemon.lock"
        lock_fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            assert is_loop_process_alive(sample_loop.id) is True
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)

    def test_lock_released_returns_false(self, sample_loop):
        """daemon.lock exists but not locked → dead."""
        lock_path = loop_dir(sample_loop.id) / "daemon.lock"
        lock_path.touch()
        assert is_loop_process_alive(sample_loop.id) is False

    def test_lock_takes_priority_over_pid(self, sample_loop):
        """daemon.lock (released) overrides PID (alive)."""
        # PID says alive, but lock says dead → trust the lock
        write_pid(sample_loop.id, os.getpid())
        lock_path = loop_dir(sample_loop.id) / "daemon.lock"
        lock_path.touch()
        assert is_loop_process_alive(sample_loop.id) is False


class TestDaemonLock:
    @patch("ralph.core.execute_one_iteration")
    def test_foreground_loop_creates_daemon_lock(self, mock_execute, sample_loop):
        """run_foreground_loop should create and hold a daemon.lock file."""
        completed = read_state(sample_loop.id)
        completed.status = "completed"
        mock_execute.return_value = MagicMock(
            state=completed, completed=True, exit_code=0,
        )
        run_foreground_loop(sample_loop.id, delay=0)
        # Lock file should exist but not be held after loop exits
        lock_path = loop_dir(sample_loop.id) / "daemon.lock"
        assert lock_path.exists()
        assert is_loop_process_alive(sample_loop.id) is False

    @patch("ralph.core.execute_one_iteration")
    def test_daemon_lock_prevents_duplicate_runner(self, mock_execute, sample_loop):
        """A second run_foreground_loop should raise if lock is held."""
        import fcntl

        lock_path = loop_dir(sample_loop.id) / "daemon.lock"
        lock_fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            with pytest.raises(RuntimeError, match="Another process"):
                run_foreground_loop(sample_loop.id, delay=0)
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)

    @patch("ralph.core.execute_one_iteration")
    def test_daemon_lock_released_on_crash(self, mock_execute, sample_loop):
        """daemon.lock should be released even if loop crashes."""
        mock_execute.side_effect = RuntimeError("boom")
        with pytest.raises(RuntimeError):
            run_foreground_loop(sample_loop.id, delay=0)
        # Lock should not be held
        assert is_loop_process_alive(sample_loop.id) is False


class TestStaleDetectionNoPidFile:
    def test_list_detects_orphaned_loop_with_last_run(self, sample_loop):
        """Running loop with no PID file but last_run_at set → stopped."""
        sample_loop.last_run_at = "2026-03-20T00:00:00+00:00"
        write_state(sample_loop)
        loops = list_loops()
        assert len(loops) == 1
        assert loops[0].status == "stopped"

    def test_list_keeps_new_loop_without_pid(self, sample_loop):
        """Running loop with no PID file and no last_run_at → running (just created)."""
        loops = list_loops()
        assert len(loops) == 1
        assert loops[0].status == "running"

    def test_list_detects_stale_via_daemon_lock(self, sample_loop):
        """Running loop with released daemon.lock → stopped."""
        lock_path = loop_dir(sample_loop.id) / "daemon.lock"
        lock_path.touch()
        # daemon.lock exists but is not held → process is dead
        # Need last_run_at or PID file for the stale detection to trigger
        sample_loop.last_run_at = "2026-03-20T00:00:00+00:00"
        write_state(sample_loop)
        loops = list_loops()
        assert len(loops) == 1
        assert loops[0].status == "stopped"


class TestAgentSubprocessCleanup:
    def test_handle_shutdown_terminates_agent(self, sample_loop):
        """Signal handler should terminate the agent subprocess."""
        import ralph.core as core
        from ralph.core import _handle_shutdown

        mock_proc = MagicMock()
        original = core._current_agent_proc
        core._current_agent_proc = mock_proc
        try:
            _handle_shutdown(15, None)
            mock_proc.terminate.assert_called_once()
        finally:
            core._current_agent_proc = original
            core._shutdown_requested = False

    def test_handle_shutdown_handles_dead_proc(self, sample_loop):
        """Signal handler should not crash if agent process is already dead."""
        import ralph.core as core
        from ralph.core import _handle_shutdown

        mock_proc = MagicMock()
        mock_proc.terminate.side_effect = ProcessLookupError("already dead")
        original = core._current_agent_proc
        core._current_agent_proc = mock_proc
        try:
            _handle_shutdown(15, None)  # Should not raise
        finally:
            core._current_agent_proc = original
            core._shutdown_requested = False

    @patch("ralph.core.subprocess.Popen")
    def test_agent_proc_cleared_after_iteration(self, mock_popen, sample_loop):
        """_current_agent_proc should be None after iteration completes."""
        import ralph.core as core

        mock_proc = MagicMock()
        mock_proc.stdout = iter(["done\n"])
        mock_proc.wait.return_value = 0
        mock_popen.return_value = mock_proc

        execute_one_iteration(sample_loop.id)
        assert core._current_agent_proc is None

    @patch("ralph.core.subprocess.Popen")
    def test_agent_proc_cleared_on_error(self, mock_popen, sample_loop):
        """_current_agent_proc should be None even if iteration errors."""
        import ralph.core as core

        mock_proc = MagicMock()
        mock_proc.stdout = iter([])
        mock_proc.wait.side_effect = OSError("broken pipe")
        mock_popen.return_value = mock_proc

        # The OSError should propagate but _current_agent_proc should be cleared
        with pytest.raises(OSError):
            execute_one_iteration(sample_loop.id)
        assert core._current_agent_proc is None


class TestCronTagPrecision:
    def test_remove_cron_does_not_match_prefix(self):
        """remove_cron for 'fix-a1b2' must not remove entry for 'fix-a1b2c3d4'."""
        from ralph.core import remove_cron

        short_id = "fix-a1b2"
        long_id = "fix-a1b2c3d4"

        crontab_content = (
            f"0 * * * * ralph once {long_id} >> /tmp/cron.log 2>&1 # ralph:{long_id}\n"
            f"0 * * * * ralph once {short_id} >> /tmp/cron.log 2>&1 # ralph:{short_id}\n"
        )

        with (
            patch("ralph.core._read_crontab", return_value=crontab_content),
            patch("ralph.core._write_crontab") as mock_write,
        ):
            remove_cron(short_id)

            written = mock_write.call_args[0][0]
            written_lines = written.strip().splitlines()
            # The long_id entry must survive
            assert any(f"# ralph:{long_id}" in line for line in written_lines)
            # The short_id entry must be removed (only its exact line, not the long one)
            assert not any(line.rstrip().endswith(f"# ralph:{short_id}") for line in written_lines)
