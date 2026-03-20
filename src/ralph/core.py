"""Core logic for ralph loop orchestration.

This module is the engine: data types, state management, provider configuration,
prompt building, iteration execution, and cron management. It has no CLI or
display concerns — that's cli.py's job.

All filesystem state lives under ~/.ralph/loops/<loop-id>/.
"""

import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType
from typing import Literal

# ─── Constants ───────────────────────────────────────────────────────────────

RALPH_DIR: Path = Path.home() / ".ralph"
LOOPS_DIR: Path = RALPH_DIR / "loops"
COMPLETION_SIGNAL: str = "RALPH_COMPLETE"


# ─── Data Types ──────────────────────────────────────────────────────────────

Status = Literal["running", "completed", "stopped", "failed"]


@dataclass
class ProviderConfig:
    """How to invoke a specific coding agent CLI.

    Each provider maps to a binary (claude, codex, etc.) and knows how to
    construct the correct command-line invocation for non-interactive mode.
    """

    binary: str  # Executable name, e.g. "claude"
    subcommand: str  # Required subcommand, e.g. "exec" for codex. Empty if none.
    prompt_flag: str  # Flag to pass prompt, e.g. "-p". Empty = positional arg.
    extra_args: list[str]  # Always-on flags, e.g. ["--dangerously-skip-permissions"]
    model_flag: str  # Flag for model selection, e.g. "--model"


@dataclass
class LoopState:
    """Persistent state for a ralph loop, serialized as state.json.

    Created once by create_loop(), updated after every iteration.
    All paths are stored as absolute strings.
    """

    id: str
    name: str
    status: Status
    provider: str
    model: str
    workdir: str
    iteration: int
    max_iterations: int
    delay_seconds: int
    cron_expression: str | None
    created_at: str
    last_run_at: str | None


@dataclass
class IterationResult:
    """What happened during a single loop iteration.

    Returned by execute_one_iteration() so callers can display status,
    decide whether to continue, etc.
    """

    state: LoopState
    output: str
    exit_code: int
    log_path: Path
    completed: bool  # True if RALPH_COMPLETE was found in output


# ─── Default Provider Configurations ─────────────────────────────────────────
# These are the built-in providers. Users can override via ~/.ralph/config.toml
# in the future, but for v1 these are hardcoded.

DEFAULT_PROVIDERS: dict[str, ProviderConfig] = {
    "claude": ProviderConfig(
        binary="claude",
        subcommand="",
        prompt_flag="-p",
        extra_args=["--dangerously-skip-permissions"],
        model_flag="--model",
    ),
    "codex": ProviderConfig(
        binary="codex",
        subcommand="exec",
        prompt_flag="",
        extra_args=["--yolo"],
        model_flag="-m",
    ),
    "aider": ProviderConfig(
        binary="aider",
        subcommand="",
        prompt_flag="--message",
        extra_args=["--yes-always"],
        model_flag="--model",
    ),
    "opencode": ProviderConfig(
        binary="opencode",
        subcommand="run",
        prompt_flag="",
        extra_args=[],
        model_flag="--model",
    ),
}


# ─── Prompt Template ─────────────────────────────────────────────────────────
# The user only writes the task. Everything below is injected automatically by
# ralph before each iteration. The agent sees this composite prompt, never the
# raw user prompt alone.

PROMPT_TEMPLATE: str = """\
# Task
{task}

# Loop context
You are on iteration {iteration} of {max_iterations} in an automated loop.
Each iteration starts with fresh context — you have no memory of previous
iterations except what is recorded below.

# Memory from previous iterations
{memory}

# End-of-iteration instructions
1. Update the memory file at {memory_path} with a concise summary of what
   you accomplished, what remains, and any context the next iteration needs.
2. Keep the memory file under 200 lines — summarize older entries as needed.
3. If the task is fully complete, print the line: RALPH_COMPLETE
"""


# ─── Loop ID Generation ─────────────────────────────────────────────────────


def generate_loop_id(name: str) -> str:
    """Generate a human-readable unique loop ID.

    Format: {slug}-{hash} where slug is first 3 words of the name
    and hash is 4 hex chars for uniqueness.

    Examples: "implement-auth-feature-a1b2", "fix-tests-c3d4"
    """
    words = name.lower().split()[:3]
    slug = "-".join(words)
    # Strip anything that isn't alphanumeric or hyphen
    slug = "".join(c for c in slug if c.isalnum() or c == "-").strip("-") or "loop"
    # Hash name + nanosecond timestamp for uniqueness
    raw = f"{name}{time.time_ns()}".encode()
    short_hash = hashlib.sha256(raw).hexdigest()[:4]
    return f"{slug}-{short_hash}"


# ─── State Management ────────────────────────────────────────────────────────


def loop_dir(loop_id: str) -> Path:
    """Return the directory path for a loop."""
    return LOOPS_DIR / loop_id


def read_state(loop_id: str) -> LoopState:
    """Read a loop's state from state.json.

    Raises FileNotFoundError if the loop directory or state.json doesn't exist.
    Raises json.JSONDecodeError or TypeError if state.json is corrupt.
    """
    path = loop_dir(loop_id) / "state.json"
    data = json.loads(path.read_text())
    return LoopState(**data)


def write_state(state: LoopState) -> None:
    """Persist a loop's state to state.json atomically.

    Writes to a temp file first, then renames — prevents corruption if the
    process is killed mid-write.
    """
    target = loop_dir(state.id) / "state.json"
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(asdict(state), indent=2) + "\n")
    tmp.replace(target)


def list_loops() -> list[LoopState]:
    """List all loops, sorted by creation time (newest first).

    Silently skips loops with missing or corrupt state files.
    Detects stale daemon loops (PID file exists but process is dead)
    and marks them as stopped.
    """
    if not LOOPS_DIR.exists():
        return []

    loops: list[LoopState] = []
    for entry in LOOPS_DIR.iterdir():
        if not entry.is_dir():
            continue
        try:
            state = read_state(entry.name)
        except (OSError, json.JSONDecodeError, TypeError, KeyError):
            continue  # Skip corrupt, incomplete, or unreadable loop directories

        # Detect stale loops: running non-cron loops whose process has died.
        # Check PID file existence BEFORE is_loop_process_alive, since the
        # latter may clean up stale PID files via read_pid.
        if state.status == "running" and state.cron_expression is None:
            had_pid_file = (loop_dir(entry.name) / "daemon.pid").exists()
            if not is_loop_process_alive(entry.name):
                # Process is dead. Mark as stopped if the loop was ever started
                # (has a PID file or has run at least once).
                if had_pid_file or state.last_run_at is not None:
                    state.status = "stopped"
                    write_state(state)

        loops.append(state)

    loops.sort(key=lambda s: s.created_at, reverse=True)
    return loops


def create_loop(
    name: str,
    prompt: str,
    provider: str,
    model: str,
    workdir: str,
    max_iterations: int,
    delay_seconds: int,
    cron_expression: str | None = None,
) -> LoopState:
    """Create a new loop: directory structure, prompt, empty memory, and state.

    The workdir is resolved to an absolute path at creation time so that
    cron-triggered iterations work regardless of cwd.
    """
    loop_id = generate_loop_id(name)
    d = loop_dir(loop_id)

    # Create directory structure
    d.mkdir(parents=True, exist_ok=True)
    (d / "logs").mkdir(exist_ok=True)

    # Write the user's prompt (immutable after creation)
    (d / "prompt.md").write_text(prompt)

    # Initialize empty memory file
    (d / "memory.md").write_text("")

    state = LoopState(
        id=loop_id,
        name=name,
        status="running",
        provider=provider,
        model=model,
        workdir=str(Path(workdir).resolve()),
        iteration=0,
        max_iterations=max_iterations,
        delay_seconds=delay_seconds,
        cron_expression=cron_expression,
        created_at=datetime.now(UTC).isoformat(),
        last_run_at=None,
    )
    write_state(state)
    return state


def sorted_log_files(loop_id: str) -> list[Path]:
    """Return log files for a loop, sorted by iteration number (numeric)."""
    logs_dir = loop_dir(loop_id) / "logs"
    if not logs_dir.exists():
        return []

    def _iter_num(p: Path) -> int:
        try:
            return int(p.stem)
        except ValueError:
            return 0

    return sorted(logs_dir.glob("*.log"), key=_iter_num)


def delete_loop(loop_id: str) -> None:
    """Delete a loop directory and all its contents."""
    d = loop_dir(loop_id)
    if d.exists():
        shutil.rmtree(d)


# ─── Daemon PID Management ──────────────────────────────────────────────────
# When `ralph run --daemon` spawns a background process, the PID is stored in
# daemon.pid so we can check status and kill it later.


def write_pid(loop_id: str, pid: int) -> None:
    """Write the daemon PID to a file."""
    (loop_dir(loop_id) / "daemon.pid").write_text(str(pid))


def read_pid(loop_id: str) -> int | None:
    """Read the daemon PID if it exists and the process is still alive.

    Returns None if no PID file exists, the PID is invalid, or the process
    is no longer running. Cleans up stale PID files automatically.
    """
    pid_file = loop_dir(loop_id) / "daemon.pid"
    if not pid_file.exists():
        return None
    try:
        pid = int(pid_file.read_text().strip())
        if pid <= 1:
            raise ValueError(f"Invalid daemon PID: {pid}")
        os.kill(pid, 0)  # Signal 0 = check if process exists, don't kill it
        return pid
    except (ValueError, ProcessLookupError, PermissionError):
        # PID file is stale or corrupt — clean it up
        pid_file.unlink(missing_ok=True)
        return None


def remove_pid(loop_id: str) -> None:
    """Remove the daemon PID file."""
    (loop_dir(loop_id) / "daemon.pid").unlink(missing_ok=True)


def is_loop_process_alive(loop_id: str) -> bool:
    """Check if a loop's runner process is alive.

    Uses daemon.lock (flock-based, robust across reboots) if available,
    falling back to PID file + os.kill(pid, 0) for older loops.
    """
    # Preferred: flock-based check (immune to PID reuse after reboot)
    lock_path = loop_dir(loop_id) / "daemon.lock"
    if lock_path.exists():
        try:
            fd = os.open(str(lock_path), os.O_RDONLY)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fd, fcntl.LOCK_UN)
                return False  # Lock was available → process is dead
            except OSError:
                return True  # Lock is held → process is alive
            finally:
                os.close(fd)
        except OSError:
            pass  # Fall through to PID check

    # Fallback: PID-based check (for loops started before daemon.lock existed)
    pid_file = loop_dir(loop_id) / "daemon.pid"
    if pid_file.exists():
        return read_pid(loop_id) is not None

    return False


def _send_signal(pid: int, sig: int) -> bool:
    """Send a signal to a process group first, falling back to the process itself.

    The daemon is spawned with start_new_session=True, making it a process
    group leader. Sending to the group kills agent children too. Falls back
    to single-process kill if group kill fails (e.g., PID isn't a group leader).
    Returns False if the process doesn't exist at all.
    """
    # Try process group first (kills daemon + agent children)
    try:
        os.killpg(pid, sig)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        pass  # Group doesn't exist or not a leader — try single process

    # Fallback: signal just the daemon process
    try:
        os.kill(pid, sig)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def kill_daemon(loop_id: str, timeout: float = 30.0) -> bool:
    """Kill the daemon process for a loop, waiting for it to exit.

    Sends SIGTERM first (graceful), waits up to `timeout` seconds, then
    sends SIGKILL if the process is still alive. Signals are sent to the
    process group when possible so agent child processes are also killed.
    Returns True if a process was killed.
    """
    pid = read_pid(loop_id)
    if pid is None:
        return False

    if not _send_signal(pid, signal.SIGTERM):
        remove_pid(loop_id)
        return False

    # Wait for the daemon process itself to exit
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)  # Check if still alive
        except ProcessLookupError:
            break  # Process exited
        except PermissionError:
            break  # PID reused by another user's process — our daemon is gone
        time.sleep(0.5)
    else:
        # Still alive after timeout — force kill
        _send_signal(pid, signal.SIGKILL)

    remove_pid(loop_id)
    return True


def daemonize_loop(loop_id: str, delay: int) -> int:
    """Spawn a detached background process running the loop.

    Uses the current Python interpreter to run `ralph _run-loop <id>`,
    detached from the terminal via start_new_session=True. This survives
    SSH disconnect, terminal close, etc.

    Returns the daemon PID. Raises RuntimeError if the child exits
    immediately (import error, bad Python path, etc.).
    """
    log_path = loop_dir(loop_id) / "daemon.log"

    with open(os.devnull) as devnull, open(log_path, "w") as log:
        proc = subprocess.Popen(
            [sys.executable, "-m", "ralph", "_run-loop", loop_id,
             "--delay", str(delay)],
            stdin=devnull,
            stdout=log,
            stderr=log,
            start_new_session=True,  # Detach from terminal session
        )

    # Parent's fds are now closed; child inherited copies via fork()
    write_pid(loop_id, proc.pid)

    # Brief pause to catch immediate startup failures (import errors, etc.)
    time.sleep(0.2)
    exit_code = proc.poll()
    if exit_code is not None:
        remove_pid(loop_id)
        hint = ""
        if log_path.exists():
            content = log_path.read_text().strip()
            if content:
                # Show last line of the error log
                hint = f": {content.splitlines()[-1]}"
        raise RuntimeError(
            f"Daemon exited immediately (code {exit_code}){hint}. "
            f"See {log_path}"
        )

    return proc.pid


# ─── Provider Management ─────────────────────────────────────────────────────


def get_provider(name: str) -> ProviderConfig:
    """Look up a provider by name. Raises ValueError if unknown."""
    if name not in DEFAULT_PROVIDERS:
        available = ", ".join(sorted(DEFAULT_PROVIDERS))
        raise ValueError(f"Unknown provider '{name}'. Available: {available}")
    return DEFAULT_PROVIDERS[name]


def validate_provider(name: str) -> None:
    """Check that a provider's binary exists on PATH.

    Raises FileNotFoundError with a descriptive message if not found.
    """
    provider = get_provider(name)
    if not shutil.which(provider.binary):
        raise FileNotFoundError(
            f"'{provider.binary}' not found on PATH. Install {name} first."
        )


def build_command(provider: ProviderConfig, prompt: str, model: str) -> list[str]:
    """Build the subprocess argv list for an agent invocation.

    The prompt is passed as a single argument element — subprocess handles
    OS-level quoting, no shell involved.
    """
    cmd: list[str] = [provider.binary]

    if provider.subcommand:
        cmd.append(provider.subcommand)

    # Prompt: either as a flag value ("-p", "prompt text") or positional
    if provider.prompt_flag:
        cmd.extend([provider.prompt_flag, prompt])
    else:
        cmd.append(prompt)

    cmd.extend(provider.extra_args)

    if model:
        cmd.extend([provider.model_flag, model])

    return cmd


# ─── Prompt Building ─────────────────────────────────────────────────────────


def build_prompt(state: LoopState) -> str:
    """Build the composite prompt the agent actually receives.

    Wraps the user's task with iteration context, accumulated memory, and
    end-of-iteration instructions. The user never writes or sees this wrapper.

    Uses manual substitution instead of str.format() because the task and
    memory can contain {curly braces} (code snippets, JSON, etc.) that
    str.format() would misinterpret as placeholders.
    """
    d = loop_dir(state.id)
    task = (d / "prompt.md").read_text().strip()
    memory_path = d / "memory.md"
    memory = memory_path.read_text().strip()

    if not memory:
        memory = "No previous memory — this is the first iteration."

    # Single-pass substitution: replaces only the placeholders present in the
    # original template. User content (task, memory) is never re-scanned, so
    # {braces} or placeholder-like strings in user text are preserved literally.
    values = {
        "{task}": task,
        "{iteration}": str(state.iteration + 1),
        "{max_iterations}": str(state.max_iterations),
        "{memory}": memory,
        "{memory_path}": str(memory_path),
    }
    # Match longest placeholders first so {memory_path} matches before {memory}
    pattern = "|".join(re.escape(k) for k in sorted(values, key=len, reverse=True))
    return re.sub(pattern, lambda m: values[m.group(0)], PROMPT_TEMPLATE)


# ─── Iteration Execution ─────────────────────────────────────────────────────


def execute_one_iteration(loop_id: str) -> IterationResult:
    """Execute a single iteration of a ralph loop.

    This is the core function that both `ralph run` and `ralph once` funnel
    through. It:
      1. Acquires an exclusive file lock (skips if another iteration is running)
      2. Reads current state (bails if not "running")
      3. Builds the composite prompt (task + memory + instructions)
      4. Runs the agent CLI as a blocking subprocess
      5. Captures output to a numbered log file
      6. Scans for RALPH_COMPLETE on its own line
      7. Updates state.json (iteration count, timestamps, status)

    Returns an IterationResult with everything the caller needs.
    """
    # Acquire an exclusive lock to prevent concurrent iterations (e.g. overlapping
    # cron fires). Non-blocking: if another iteration is running, skip this one.
    lock_path = loop_dir(loop_id) / "iteration.lock"
    lock_path.touch(exist_ok=True)
    lock_file = open(lock_path)  # noqa: SIM115 — held for duration of iteration
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        # Another iteration is already running — skip
        lock_file.close()
        state = read_state(loop_id)
        return IterationResult(
            state=state, output="Skipped: another iteration is already running.\n",
            exit_code=-1, log_path=loop_dir(loop_id) / "logs" / "000.log",
            completed=False,
        )

    try:
        return _execute_one_iteration_locked(loop_id)
    finally:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()


def _execute_one_iteration_locked(loop_id: str) -> IterationResult:
    """Inner implementation of execute_one_iteration, called with lock held."""
    state = read_state(loop_id)

    # Don't run if the loop isn't active
    if state.status != "running":
        return IterationResult(
            state=state, output="", exit_code=-1,
            log_path=loop_dir(loop_id) / "logs" / "000.log", completed=False,
        )

    # Build the full prompt and agent command
    prompt = build_prompt(state)
    provider = get_provider(state.provider)
    cmd = build_command(provider, prompt, state.model)

    # Log file for this iteration
    log_path = loop_dir(loop_id) / "logs" / f"{state.iteration + 1:03d}.log"

    # Run the agent as a blocking subprocess. We stream stdout line-by-line
    # to the log file and accumulate it for completion detection.
    output_lines: list[str] = []
    global _current_agent_proc
    try:
        with open(log_path, "w") as log:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                cwd=state.workdir,
            )
            _current_agent_proc = proc
            try:
                # stdout is guaranteed non-None because we set stdout=PIPE
                assert proc.stdout is not None
                for line in proc.stdout:
                    log.write(line)
                    log.flush()
                    output_lines.append(line)
                exit_code = proc.wait()
            finally:
                _current_agent_proc = None
    except FileNotFoundError:
        # Could be the agent binary OR the workdir that doesn't exist
        if not Path(state.workdir).is_dir():
            error_msg = f"ERROR: Working directory '{state.workdir}' not found.\n"
        else:
            error_msg = f"ERROR: '{provider.binary}' not found. Is {state.provider} installed?\n"
        log_path.write_text(error_msg)
        state.iteration += 1
        state.last_run_at = datetime.now(UTC).isoformat()
        state.status = "failed"
        write_state(state)
        return IterationResult(
            state=state, output=error_msg, exit_code=127,
            log_path=log_path, completed=False,
        )

    output = "".join(output_lines)

    # Completion: RALPH_COMPLETE must appear on its own line (not as part of
    # the echoed prompt template or a debug message)
    completed = any(line.strip() == COMPLETION_SIGNAL for line in output_lines)

    # Update state
    state.iteration += 1
    state.last_run_at = datetime.now(UTC).isoformat()

    if completed:
        state.status = "completed"
    elif state.iteration >= state.max_iterations:
        state.status = "stopped"

    # Persist state BEFORE removing cron — if remove_cron fails (e.g. crontab
    # binary missing), the status is already saved. The orphaned cron entry
    # will fire again but execute_one_iteration will skip (status != "running").
    write_state(state)

    if state.status in ("completed", "stopped") and state.cron_expression:
        try:
            remove_cron(loop_id)
        except Exception:
            pass  # State already persisted — cron entry is harmless

    return IterationResult(
        state=state,
        output=output,
        exit_code=exit_code,
        log_path=log_path,
        completed=completed,
    )


# ─── Cron Management ─────────────────────────────────────────────────────────
# Direct crontab manipulation via subprocess — no extra dependencies.
# Each ralph entry is tagged with "# ralph:<loop-id>" for identification.


def _read_crontab() -> str:
    """Read the current user's crontab. Returns empty string if none exists."""
    result = subprocess.run(
        ["crontab", "-l"], capture_output=True, text=True,
    )
    # crontab -l exits non-zero when no crontab exists (macOS/Linux)
    return result.stdout if result.returncode == 0 else ""


def _write_crontab(content: str) -> None:
    """Replace the user's crontab. Content is piped via stdin."""
    subprocess.run(
        ["crontab", "-"], input=content, text=True, check=True,
    )


def install_cron(loop_id: str, cron_expr: str) -> None:
    """Install a cron entry that runs `ralph once <loop-id>` on schedule.

    Uses the full path to the ralph binary (or python -m ralph as fallback)
    so the entry works regardless of cron's limited PATH.
    """
    ralph_bin = shutil.which("ralph")
    if ralph_bin is not None:
        ralph_cmd = ralph_bin
    else:
        # Fallback: invoke via the current Python interpreter
        ralph_cmd = f"{sys.executable} -m ralph"

    log_path = loop_dir(loop_id) / "cron.log"
    tag = f"# ralph:{loop_id}"
    job = f"{cron_expr} {ralph_cmd} once {loop_id} >> {log_path} 2>&1 {tag}"

    current = _read_crontab().rstrip("\n")
    new_content = f"{current}\n{job}\n" if current else f"{job}\n"
    _write_crontab(new_content)


def remove_cron(loop_id: str) -> None:
    """Remove the cron entry for a loop. No-op if no matching entry exists."""
    current = _read_crontab()
    if not current:
        return

    tag = f"# ralph:{loop_id}"
    # Use endswith (not substring `in`) to avoid matching loop IDs that share a prefix
    lines = [line for line in current.splitlines() if not line.rstrip().endswith(tag)]
    new_content = "\n".join(lines) + "\n" if lines else ""
    _write_crontab(new_content)


# ─── Foreground Loop Runner ──────────────────────────────────────────────────
# Used by `ralph run`. Handles SIGINT/SIGTERM gracefully: the current iteration
# finishes, state is saved, then the loop exits.

# Module-level shutdown flag, set by signal handler
_shutdown_requested: bool = False

# Reference to the currently-running agent subprocess so the signal handler
# can terminate it for prompt shutdown (prevents orphaned agent processes).
_current_agent_proc: subprocess.Popen | None = None


def _handle_shutdown(signum: int, frame: FrameType | None) -> None:
    """Signal handler that requests graceful shutdown.

    Also terminates the currently-running agent subprocess (if any) so the
    iteration exits promptly instead of waiting for the agent to finish.
    """
    global _shutdown_requested
    _shutdown_requested = True
    proc = _current_agent_proc
    if proc is not None:
        try:
            proc.terminate()
        except (ProcessLookupError, PermissionError, OSError):
            pass


# Type alias for the iteration callback
IterationCallback = Callable[[IterationResult], None]


def run_foreground_loop(
    loop_id: str,
    delay: int,
    on_iteration: IterationCallback | None = None,
) -> LoopState:
    """Run a ralph loop in the foreground with constant delay.

    Blocks until the loop completes, is stopped, or receives SIGINT/SIGTERM/SIGHUP.
    The on_iteration callback fires after each iteration for status display.

    Holds an exclusive flock on daemon.lock for the entire lifetime of the loop.
    This provides robust liveness detection that survives PID reuse after reboot,
    and prevents duplicate runners for the same loop.

    The delay is interruptible — it checks the shutdown flag every second
    rather than sleeping the full duration at once.
    """
    global _shutdown_requested
    _shutdown_requested = False

    # Acquire daemon lock — prevents duplicate runners and enables robust
    # liveness checking via flock (immune to PID reuse after reboot).
    daemon_lock_path = loop_dir(loop_id) / "daemon.lock"
    daemon_lock_fd = os.open(str(daemon_lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(daemon_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(daemon_lock_fd)
        raise RuntimeError(f"Another process is already running loop {loop_id}")

    # Write PID file so signal-based stop works (kill_daemon needs the PID).
    write_pid(loop_id, os.getpid())

    # Install signal handlers, saving originals for cleanup
    prev_sigint = signal.signal(signal.SIGINT, _handle_shutdown)
    prev_sigterm = signal.signal(signal.SIGTERM, _handle_shutdown)
    # Handle SIGHUP (terminal close) unless it's already ignored (e.g. nohup)
    prev_sighup = signal.getsignal(signal.SIGHUP)
    if prev_sighup != signal.SIG_IGN:
        signal.signal(signal.SIGHUP, _handle_shutdown)

    try:
        while not _shutdown_requested:
            try:
                result = execute_one_iteration(loop_id)
            except Exception:
                # Unrecoverable error (deleted directory, corrupt state, etc.)
                # Mark as failed if possible, then re-raise
                try:
                    st = read_state(loop_id)
                    if st.status == "running":
                        st.status = "failed"
                        write_state(st)
                except Exception:
                    pass
                raise

            if on_iteration is not None:
                on_iteration(result)

            # If the loop finished (completed, stopped, failed, max iter), we're done
            if result.state.status != "running":
                return result.state

            # Interruptible sleep: check shutdown flag each second
            for _ in range(delay):
                if _shutdown_requested:
                    break
                time.sleep(1)

        # Shutdown was requested — mark loop as stopped
        state = read_state(loop_id)
        if state.status == "running":
            state.status = "stopped"
            write_state(state)
        return state

    finally:
        # Restore original signal handlers
        signal.signal(signal.SIGINT, prev_sigint)
        signal.signal(signal.SIGTERM, prev_sigterm)
        if prev_sighup != signal.SIG_IGN:
            signal.signal(signal.SIGHUP, prev_sighup)
        remove_pid(loop_id)
        # Release daemon lock so liveness checks see the process as dead
        try:
            fcntl.flock(daemon_lock_fd, fcntl.LOCK_UN)
            os.close(daemon_lock_fd)
        except OSError:
            pass
