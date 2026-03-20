"""Tests for ralph CLI interface."""

import os
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from ralph.cli import app
from ralph.core import (
    create_loop,
    loop_dir,
    read_pid,
    read_state,
    write_pid,
    write_state,
)

runner = CliRunner()


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


# ─── Version ─────────────────────────────────────────────────────────────────


def test_version():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "ralph" in result.output


# ─── List ────────────────────────────────────────────────────────────────────


def test_list_empty():
    result = runner.invoke(app, ["list"])
    assert result.exit_code == 0
    assert "No loops found" in result.output


def test_list_shows_loops():
    create_loop("my loop", "test task", "claude", "", "/tmp", 10, 5)
    result = runner.invoke(app, ["list"])
    assert result.exit_code == 0
    assert "my loop" in result.output


def test_list_shows_daemon_label():
    """Daemon loops should show '(daemon)' not '(foreground)' in list."""
    state = create_loop("daemon loop", "task", "claude", "", "/tmp", 10, 5)
    # Simulate a running daemon by writing PID of current process
    write_pid(state.id, os.getpid())

    result = runner.invoke(app, ["list"])
    assert result.exit_code == 0
    assert "(daemon)" in result.output


def test_list_shows_foreground_label():
    """Foreground loops without PID show '(foreground)'."""
    create_loop("fg loop", "task", "claude", "", "/tmp", 10, 5)
    result = runner.invoke(app, ["list"])
    assert "(foreground)" in result.output


def test_list_shows_cron_expression():
    create_loop("cron loop", "task", "claude", "", "/tmp", 10, 5,
                cron_expression="0 * * * *")
    result = runner.invoke(app, ["list"])
    assert "0 * * * *" in result.output


# ─── Show ────────────────────────────────────────────────────────────────────


def test_show_nonexistent():
    result = runner.invoke(app, ["show", "nonexistent-id"])
    assert result.exit_code == 1
    assert "not found" in result.output


def test_show_displays_details():
    state = create_loop("detail loop", "task prompt", "claude", "sonnet", "/tmp", 10, 5)
    result = runner.invoke(app, ["show", state.id])
    assert result.exit_code == 0
    assert state.id in result.output
    assert "claude" in result.output
    assert "sonnet" in result.output


def test_show_daemon_indicator():
    """Show command should display daemon PID."""
    state = create_loop("daemon loop", "task", "claude", "", "/tmp", 10, 5)
    write_pid(state.id, os.getpid())

    result = runner.invoke(app, ["show", state.id])
    assert "Daemon" in result.output
    assert str(os.getpid()) in result.output


# ─── Remove ──────────────────────────────────────────────────────────────────


def test_remove_nonexistent():
    result = runner.invoke(app, ["remove", "nonexistent-id"])
    assert result.exit_code == 1


def test_remove_deletes_files():
    state = create_loop("to remove", "task", "claude", "", "/tmp", 10, 5)
    d = loop_dir(state.id)
    assert d.exists()

    result = runner.invoke(app, ["remove", state.id])
    assert result.exit_code == 0
    assert not d.exists()


def test_remove_keep_files():
    state = create_loop("to stop", "task", "claude", "", "/tmp", 10, 5)
    d = loop_dir(state.id)

    result = runner.invoke(app, ["remove", state.id, "--keep-files"])
    assert result.exit_code == 0
    assert d.exists()  # Directory preserved

    reloaded = read_state(state.id)
    assert reloaded.status == "stopped"


def test_remove_with_cron(monkeypatch):
    """Remove should call remove_cron for cron loops."""
    state = create_loop("cron remove", "task", "claude", "", "/tmp", 10, 5,
                        cron_expression="0 * * * *")

    with patch("ralph.cli.remove_cron") as mock_remove:
        result = runner.invoke(app, ["remove", state.id])
        assert result.exit_code == 0
        mock_remove.assert_called_once_with(state.id)


# ─── Logs ────────────────────────────────────────────────────────────────────


def test_logs_nonexistent():
    result = runner.invoke(app, ["logs", "nonexistent-id"])
    assert result.exit_code == 1


def test_logs_no_logs_yet():
    state = create_loop("no logs", "task", "claude", "", "/tmp", 10, 5)
    result = runner.invoke(app, ["logs", state.id])
    assert result.exit_code == 0
    assert "No logs yet" in result.output


def test_logs_shows_content():
    state = create_loop("has logs", "task", "claude", "", "/tmp", 10, 5)
    log_file = loop_dir(state.id) / "logs" / "001.log"
    log_file.write_text("test log content\n")

    result = runner.invoke(app, ["logs", state.id])
    assert "test log content" in result.output


def test_logs_tail():
    state = create_loop("tail test", "task", "claude", "", "/tmp", 10, 5)
    log_file = loop_dir(state.id) / "logs" / "001.log"
    log_file.write_text("line1\nline2\nline3\nline4\n")

    result = runner.invoke(app, ["logs", state.id, "--tail", "2"])
    assert "line1" not in result.output
    assert "line3" in result.output
    assert "line4" in result.output


def test_logs_specific_iteration():
    state = create_loop("iter logs", "task", "claude", "", "/tmp", 10, 5)
    (loop_dir(state.id) / "logs" / "001.log").write_text("iter 1\n")
    (loop_dir(state.id) / "logs" / "002.log").write_text("iter 2\n")

    result = runner.invoke(app, ["logs", state.id, "--iter", "1"])
    assert "iter 1" in result.output


def test_logs_nonexistent_iteration():
    state = create_loop("no iter", "task", "claude", "", "/tmp", 10, 5)
    result = runner.invoke(app, ["logs", state.id, "--iter", "99"])
    assert result.exit_code == 1


# ─── Once ────────────────────────────────────────────────────────────────────


def test_once_nonexistent():
    result = runner.invoke(app, ["once", "nonexistent-id"])
    assert result.exit_code == 1


@patch("ralph.core.subprocess.Popen")
def test_once_runs_iteration(mock_popen):
    state = create_loop("once test", "task", "claude", "", "/tmp", 10, 5)

    mock_proc = MagicMock()
    mock_proc.stdout = iter(["done\n"])
    mock_proc.wait.return_value = 0
    mock_popen.return_value = mock_proc

    result = runner.invoke(app, ["once", state.id])
    assert result.exit_code == 0
    assert "iteration" in result.output


# ─── _run-loop (daemon internal command) ─────────────────────────────────────


@patch("ralph.cli.run_foreground_loop")
def test_run_loop_cmd_sets_failed_on_crash(mock_run, isolated_ralph_dir):
    """_run-loop should set status to 'failed' if an exception occurs."""
    state = create_loop("crash test", "task", "claude", "", "/tmp", 10, 5)
    write_pid(state.id, os.getpid())
    mock_run.side_effect = RuntimeError("boom")

    result = runner.invoke(app, ["_run-loop", state.id])
    # The command should fail but state should be updated
    assert result.exit_code != 0

    reloaded = read_state(state.id)
    assert reloaded.status == "failed"

    # PID file should be cleaned up
    assert read_pid(state.id) is None


@patch("ralph.cli.run_foreground_loop")
def test_run_loop_cmd_removes_pid_on_success(mock_run, isolated_ralph_dir):
    """PID file should be removed even on normal completion."""
    state = create_loop("clean exit", "task", "claude", "", "/tmp", 10, 5)
    write_pid(state.id, os.getpid())

    completed_state = read_state(state.id)
    completed_state.status = "completed"
    mock_run.return_value = completed_state

    runner.invoke(app, ["_run-loop", state.id])

    pid_file = loop_dir(state.id) / "daemon.pid"
    assert not pid_file.exists()


# ─── Corrupt State Handling ──────────────────────────────────────────────────


def test_show_permission_denied():
    """show should print a friendly error for unreadable state.json."""
    state = create_loop("perm denied", "task", "claude", "", "/tmp", 10, 5)
    state_file = loop_dir(state.id) / "state.json"
    state_file.chmod(0o000)

    try:
        result = runner.invoke(app, ["show", state.id])
        assert result.exit_code == 1
        assert "Permission denied" in result.output
    finally:
        state_file.chmod(0o644)


def test_show_corrupt_state():
    """show should print a friendly error for corrupt state.json."""
    state = create_loop("corrupt show", "task", "claude", "", "/tmp", 10, 5)
    (loop_dir(state.id) / "state.json").write_text("{invalid json")

    result = runner.invoke(app, ["show", state.id])
    assert result.exit_code == 1
    assert "Corrupt state" in result.output


def test_remove_corrupt_state():
    """remove should print a friendly error for corrupt state.json."""
    state = create_loop("corrupt remove", "task", "claude", "", "/tmp", 10, 5)
    (loop_dir(state.id) / "state.json").write_text("{bad")

    result = runner.invoke(app, ["remove", state.id])
    assert result.exit_code == 1
    assert "Corrupt state" in result.output


def test_logs_corrupt_state():
    """logs should print a friendly error for corrupt state.json."""
    state = create_loop("corrupt logs", "task", "claude", "", "/tmp", 10, 5)
    (loop_dir(state.id) / "state.json").write_text("not json")

    result = runner.invoke(app, ["logs", state.id])
    assert result.exit_code == 1
    assert "Corrupt state" in result.output


def test_once_corrupt_state():
    """once should print a friendly error for corrupt state.json."""
    state = create_loop("corrupt once", "task", "claude", "", "/tmp", 10, 5)
    (loop_dir(state.id) / "state.json").write_text("[]")  # valid JSON, wrong type

    result = runner.invoke(app, ["once", state.id])
    assert result.exit_code == 1
    assert "Corrupt state" in result.output


# ─── Show Stale Daemon Detection ─────────────────────────────────────────────


def test_show_detects_stale_daemon():
    """show should detect a dead daemon and update status to stopped."""
    state = create_loop("stale daemon", "task", "claude", "", "/tmp", 10, 5)
    # Write a PID file for a non-existent process
    (loop_dir(state.id) / "daemon.pid").write_text("999999999")

    result = runner.invoke(app, ["show", state.id])
    assert result.exit_code == 0
    assert "stopped" in result.output

    reloaded = read_state(state.id)
    assert reloaded.status == "stopped"


def test_show_daemon_schedule_label():
    """show should display '(daemon)' for running daemon loops."""
    state = create_loop("daemon show", "task", "claude", "", "/tmp", 10, 5)
    write_pid(state.id, os.getpid())  # Current process is alive

    result = runner.invoke(app, ["show", state.id])
    assert "(daemon)" in result.output


# ─── Remove --keep-files state freshness ─────────────────────────────────────


def test_remove_keep_files_rereads_state():
    """remove --keep-files should re-read state after killing daemon."""
    state = create_loop("reread test", "task", "claude", "", "/tmp", 10, 5)
    # Simulate: daemon ran 5 more iterations before being killed
    state.iteration = 5
    state.last_run_at = "2026-03-20T00:00:00+00:00"
    write_state(state)

    result = runner.invoke(app, ["remove", state.id, "--keep-files"])
    assert result.exit_code == 0

    reloaded = read_state(state.id)
    assert reloaded.status == "stopped"
    assert reloaded.iteration == 5  # Preserved, not reverted to 0


# ─── Schedule Missing Crontab ────────────────────────────────────────────────


def test_schedule_missing_crontab():
    """schedule should give a clear error if crontab binary is missing."""
    with patch("ralph.cli.validate_provider"):  # Skip provider check
        with patch("ralph.cli.install_cron", side_effect=FileNotFoundError("No crontab")):
            result = runner.invoke(app, [
                "schedule", "test task", "--cron", "0 * * * *",
            ])
            assert result.exit_code == 1
            assert "crontab" in result.output


def test_schedule_crontab_rejects_content():
    """schedule should clean up if crontab rejects the content."""
    import subprocess as sp

    error = sp.CalledProcessError(1, "crontab -", stderr="bad cron")
    with patch("ralph.cli.validate_provider"):
        with patch("ralph.cli.install_cron", side_effect=error):
            result = runner.invoke(app, [
                "schedule", "test task", "--cron", "bad expr",
            ])
            assert result.exit_code == 1
            assert "Failed to install cron job" in result.output


# ─── Log Sorting in CLI ──────────────────────────────────────────────────────


def test_logs_latest_uses_numeric_sort():
    """ralph logs should pick the numerically highest log, not lexicographic."""
    state = create_loop("sort test", "task", "claude", "", "/tmp", 10, 5)
    logs_dir = loop_dir(state.id) / "logs"
    (logs_dir / "999.log").write_text("old iteration\n")
    (logs_dir / "1000.log").write_text("new iteration\n")

    result = runner.invoke(app, ["logs", state.id])
    assert "new iteration" in result.output


# ─── Remove kills foreground loops via PID ────────────────────────────────────


def test_remove_stops_foreground_via_pid():
    """remove should stop a running foreground loop if PID file exists."""
    state = create_loop("fg remove", "task", "claude", "", "/tmp", 10, 5)
    # Simulate a foreground loop with a dead PID (stale)
    (loop_dir(state.id) / "daemon.pid").write_text("999999999")

    result = runner.invoke(app, ["remove", state.id])
    assert result.exit_code == 0
    assert not loop_dir(state.id).exists()


# ─── List shows stale foreground as stopped ───────────────────────────────────


def test_list_stale_foreground_shows_stopped():
    """Foreground loops with dead PID should show as stopped."""
    state = create_loop("stale fg", "task", "claude", "", "/tmp", 10, 5)
    (loop_dir(state.id) / "daemon.pid").write_text("999999999")

    result = runner.invoke(app, ["list"])
    assert "stopped" in result.output


# ─── CLI run handles crash gracefully ─────────────────────────────────────────


@patch("ralph.cli.run_foreground_loop")
def test_run_foreground_crash_shows_error(mock_run):
    """CLI run should show a friendly error on crash, not a traceback."""
    with patch("ralph.cli.validate_provider"):
        mock_run.side_effect = OSError("disk full")
        result = runner.invoke(app, ["run", "test task"])
        assert result.exit_code == 1
        assert "crashed" in result.output


# ─── Once reports failed status ───────────────────────────────────────────────


@patch("ralph.core.subprocess.Popen")
def test_once_reports_failed_status(mock_popen):
    """once should report FAILED when the loop fails (e.g., binary not found)."""
    state = create_loop("fail test", "task", "claude", "", "/tmp", 10, 5)
    mock_popen.side_effect = FileNotFoundError("No such file")

    result = runner.invoke(app, ["once", state.id])
    assert result.exit_code == 0
    assert "FAILED" in result.output


def test_once_reports_stopped_loop():
    """once should say STOPPED for a stopped loop, not 'done (exit -1)'."""
    state = create_loop("stopped loop", "task", "claude", "", "/tmp", 10, 5)
    state.status = "stopped"
    write_state(state)

    result = runner.invoke(app, ["once", state.id])
    assert result.exit_code == 0
    assert "STOPPED" in result.output


# ─── Remove proceeds even if cron removal fails ──────────────────────────────


def test_remove_proceeds_when_cron_removal_fails():
    """remove should still delete files even if remove_cron fails."""
    state = create_loop("cron fail", "task", "claude", "", "/tmp", 10, 5,
                        cron_expression="0 * * * *")
    d = loop_dir(state.id)

    with patch("ralph.cli.remove_cron", side_effect=FileNotFoundError("no crontab")):
        result = runner.invoke(app, ["remove", state.id])
        assert result.exit_code == 0
        assert not d.exists()  # Files still deleted despite cron failure
        assert "Warning" in result.output


# ─── Daemon startup failure ──────────────────────────────────────────────────


def test_run_daemon_startup_failure():
    """CLI run --daemon should show error if daemon dies immediately."""
    with patch("ralph.cli.validate_provider"):
        error = RuntimeError("Daemon exited immediately (code 1)")
        with patch("ralph.cli.daemonize_loop", side_effect=error):
            result = runner.invoke(app, ["run", "test task", "--daemon"])
            assert result.exit_code == 1
            assert "Daemon exited immediately" in result.output


def test_run_daemon_startup_failure_marks_failed():
    """If daemon fails to start, loop state should be 'failed', not 'running'."""
    with patch("ralph.cli.validate_provider"):
        with patch("ralph.cli.daemonize_loop", side_effect=RuntimeError("boom")):
            result = runner.invoke(app, ["run", "test task", "--daemon"])
            assert result.exit_code == 1

    # Find the loop that was created and verify its status
    from ralph.core import list_loops
    loops = list_loops()
    assert len(loops) == 1
    assert loops[0].status == "failed"


# ─── Remove --keep-files preserves completed/failed ──────────────────────────


def test_remove_keep_files_preserves_completed():
    """remove --keep-files should not overwrite 'completed' with 'stopped'."""
    state = create_loop("done loop", "task", "claude", "", "/tmp", 10, 5)
    state.status = "completed"
    state.iteration = 10
    write_state(state)

    result = runner.invoke(app, ["remove", state.id, "--keep-files"])
    assert result.exit_code == 0

    reloaded = read_state(state.id)
    assert reloaded.status == "completed"  # NOT "stopped"


def test_remove_keep_files_preserves_failed():
    """remove --keep-files should not overwrite 'failed' with 'stopped'."""
    state = create_loop("fail loop", "task", "claude", "", "/tmp", 10, 5)
    state.status = "failed"
    write_state(state)

    result = runner.invoke(app, ["remove", state.id, "--keep-files"])
    assert result.exit_code == 0

    reloaded = read_state(state.id)
    assert reloaded.status == "failed"  # NOT "stopped"


# ─── Remove robust against kill_daemon errors ────────────────────────────────


def test_remove_proceeds_when_kill_daemon_fails():
    """remove should still delete files even if kill_daemon raises."""
    state = create_loop("kill fail", "task", "claude", "", "/tmp", 10, 5)
    d = loop_dir(state.id)

    with patch("ralph.cli.kill_daemon", side_effect=OSError("disk error")):
        result = runner.invoke(app, ["remove", state.id])
        assert result.exit_code == 0
        assert not d.exists()
        assert "Warning" in result.output


# ─── Once handles unexpected exceptions ──────────────────────────────────────


def test_once_handles_unexpected_exception():
    """once should show friendly error for non-FileNotFoundError exceptions."""
    state = create_loop("err test", "task", "claude", "", "/tmp", 10, 5)

    with patch(
        "ralph.cli.execute_one_iteration",
        side_effect=PermissionError("lock file denied"),
    ):
        result = runner.invoke(app, ["once", state.id])
        assert result.exit_code == 1
        assert "Error running iteration" in result.output


# ─── Stop command ─────────────────────────────────────────────────────────────


def test_stop_nonexistent():
    result = runner.invoke(app, ["stop", "nonexistent-id"])
    assert result.exit_code == 1


def test_stop_running_loop():
    state = create_loop("to stop", "task", "claude", "", "/tmp", 10, 5)
    result = runner.invoke(app, ["stop", state.id])
    assert result.exit_code == 0
    assert "stopped" in result.output

    reloaded = read_state(state.id)
    assert reloaded.status == "stopped"
    assert loop_dir(state.id).exists()  # Files preserved


def test_stop_already_completed():
    state = create_loop("done loop", "task", "claude", "", "/tmp", 10, 5)
    state.status = "completed"
    write_state(state)

    result = runner.invoke(app, ["stop", state.id])
    assert result.exit_code == 0
    assert "already completed" in result.output

    reloaded = read_state(state.id)
    assert reloaded.status == "completed"  # Unchanged


def test_stop_already_stopped():
    state = create_loop("stopped loop", "task", "claude", "", "/tmp", 10, 5)
    state.status = "stopped"
    write_state(state)

    result = runner.invoke(app, ["stop", state.id])
    assert result.exit_code == 0
    assert "already stopped" in result.output


def test_stop_kills_daemon():
    """stop should kill a running daemon process."""
    state = create_loop("daemon stop", "task", "claude", "", "/tmp", 10, 5)
    write_pid(state.id, os.getpid())  # Pretend current process is the daemon

    with patch("ralph.cli.kill_daemon", return_value=True) as mock_kill:
        result = runner.invoke(app, ["stop", state.id])
        assert result.exit_code == 0
        mock_kill.assert_called_once_with(state.id)
        assert "Stopped process" in result.output


def test_stop_removes_cron():
    """stop should remove cron entry for cron loops."""
    state = create_loop("cron stop", "task", "claude", "", "/tmp", 10, 5,
                        cron_expression="0 * * * *")

    with patch("ralph.cli.remove_cron") as mock_remove:
        result = runner.invoke(app, ["stop", state.id])
        assert result.exit_code == 0
        mock_remove.assert_called_once_with(state.id)


def test_stop_preserves_completed_status():
    """stop should not overwrite completed with stopped."""
    state = create_loop("done", "task", "claude", "", "/tmp", 10, 5)
    state.status = "completed"
    write_state(state)

    runner.invoke(app, ["stop", state.id])
    reloaded = read_state(state.id)
    assert reloaded.status == "completed"


# ─── Daemon log visibility ───────────────────────────────────────────────────


def test_show_displays_daemon_log():
    """show should display daemon.log content when it exists."""
    state = create_loop("daemon log", "task", "claude", "", "/tmp", 10, 5)
    d = loop_dir(state.id)
    (d / "daemon.log").write_text("Traceback: ImportError: no module named foo\n")

    result = runner.invoke(app, ["show", state.id])
    assert result.exit_code == 0
    assert "Daemon log" in result.output
    assert "ImportError" in result.output


def test_show_skips_empty_daemon_log():
    """show should not show daemon log section if the file is empty."""
    state = create_loop("empty log", "task", "claude", "", "/tmp", 10, 5)
    d = loop_dir(state.id)
    (d / "daemon.log").write_text("")

    result = runner.invoke(app, ["show", state.id])
    assert "Daemon log" not in result.output


# ─── Schedule label accuracy ─────────────────────────────────────────────────


def test_list_shows_daemon_stopped_label():
    """Stopped daemon loops should show '(daemon, stopped)' not '(foreground)'."""
    state = create_loop("dead daemon", "task", "claude", "", "/tmp", 10, 5)
    state.status = "stopped"
    write_state(state)
    # daemon.log exists → was a daemon
    (loop_dir(state.id) / "daemon.log").write_text("started\n")

    result = runner.invoke(app, ["list"])
    # Rich table may wrap the label across lines, so check the parts separately
    assert "(daemon," in result.output
    assert "stopped)" in result.output
    assert "(foreground)" not in result.output


# ─── Stale detection via is_loop_process_alive ────────────────────────────────


def test_show_detects_stale_via_last_run(isolated_ralph_dir):
    """show should detect orphaned loop: running, no PID, has last_run_at."""
    state = create_loop("orphan", "task", "claude", "", "/tmp", 10, 5)
    state.last_run_at = "2026-03-20T00:00:00+00:00"
    write_state(state)

    result = runner.invoke(app, ["show", state.id])
    assert result.exit_code == 0
    assert "stopped" in result.output

    reloaded = read_state(state.id)
    assert reloaded.status == "stopped"
