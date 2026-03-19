"""Core logic for ralph loop orchestration.

This module is the engine: data types, state management, provider configuration,
prompt building, iteration execution, and cron management. It has no CLI or
display concerns — that's cli.py's job.

All filesystem state lives under ~/.ralph/loops/<loop-id>/.
"""

import hashlib
import json
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
    """
    if not LOOPS_DIR.exists():
        return []

    loops: list[LoopState] = []
    for entry in LOOPS_DIR.iterdir():
        if not entry.is_dir():
            continue
        try:
            loops.append(read_state(entry.name))
        except (FileNotFoundError, json.JSONDecodeError, TypeError, KeyError):
            pass  # Skip corrupt or incomplete loop directories

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


def delete_loop(loop_id: str) -> None:
    """Delete a loop directory and all its contents."""
    d = loop_dir(loop_id)
    if d.exists():
        shutil.rmtree(d)


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
    """
    d = loop_dir(state.id)
    task = (d / "prompt.md").read_text().strip()
    memory_path = d / "memory.md"
    memory = memory_path.read_text().strip()

    if not memory:
        memory = "No previous memory — this is the first iteration."

    return PROMPT_TEMPLATE.format(
        task=task,
        iteration=state.iteration + 1,  # 1-indexed for human display
        max_iterations=state.max_iterations,
        memory=memory,
        memory_path=str(memory_path),
    )


# ─── Iteration Execution ─────────────────────────────────────────────────────


def execute_one_iteration(loop_id: str) -> IterationResult:
    """Execute a single iteration of a ralph loop.

    This is the core function that both `ralph run` and `ralph once` funnel
    through. It:
      1. Reads current state (bails if not "running")
      2. Builds the composite prompt (task + memory + instructions)
      3. Runs the agent CLI as a blocking subprocess
      4. Captures output to a numbered log file
      5. Scans for RALPH_COMPLETE on its own line
      6. Updates state.json (iteration count, timestamps, status)

    Returns an IterationResult with everything the caller needs.
    """
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
    try:
        with open(log_path, "w") as log:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                cwd=state.workdir,
            )
            # stdout is guaranteed non-None because we set stdout=PIPE
            assert proc.stdout is not None
            for line in proc.stdout:
                log.write(line)
                output_lines.append(line)
            exit_code = proc.wait()
    except FileNotFoundError:
        # Agent binary not found at runtime (e.g. uninstalled between schedule and run)
        error_msg = f"ERROR: '{provider.binary}' not found. Is {state.provider} installed?\n"
        log_path.write_text(error_msg)
        state.iteration += 1
        state.last_run_at = datetime.now(UTC).isoformat()
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
        remove_cron(loop_id)
    elif state.iteration >= state.max_iterations:
        state.status = "stopped"
        remove_cron(loop_id)

    write_state(state)

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
    lines = [line for line in current.splitlines() if tag not in line]
    new_content = "\n".join(lines) + "\n" if lines else ""
    _write_crontab(new_content)


# ─── Foreground Loop Runner ──────────────────────────────────────────────────
# Used by `ralph run`. Handles SIGINT/SIGTERM gracefully: the current iteration
# finishes, state is saved, then the loop exits.

# Module-level shutdown flag, set by signal handler
_shutdown_requested: bool = False


def _handle_shutdown(signum: int, frame: FrameType | None) -> None:
    """Signal handler that requests graceful shutdown."""
    global _shutdown_requested
    _shutdown_requested = True


# Type alias for the iteration callback
IterationCallback = Callable[[IterationResult], None]


def run_foreground_loop(
    loop_id: str,
    delay: int,
    on_iteration: IterationCallback | None = None,
) -> LoopState:
    """Run a ralph loop in the foreground with constant delay.

    Blocks until the loop completes, is stopped, or receives SIGINT/SIGTERM.
    The on_iteration callback fires after each iteration for status display.

    The delay is interruptible — it checks the shutdown flag every second
    rather than sleeping the full duration at once.
    """
    global _shutdown_requested
    _shutdown_requested = False

    # Install signal handlers, saving originals for cleanup
    prev_sigint = signal.signal(signal.SIGINT, _handle_shutdown)
    prev_sigterm = signal.signal(signal.SIGTERM, _handle_shutdown)

    try:
        while not _shutdown_requested:
            result = execute_one_iteration(loop_id)

            if on_iteration is not None:
                on_iteration(result)

            # If the loop finished (completed, stopped, max iter), we're done
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
