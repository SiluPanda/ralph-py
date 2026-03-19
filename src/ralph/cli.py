"""CLI interface for ralph loop orchestrator.

Thin layer over core.py — validates inputs, calls core functions, formats output.
All commands are defined here using Typer.
"""

from datetime import UTC, datetime
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from ralph import __version__
from ralph.core import (
    IterationResult,
    create_loop,
    daemonize_loop,
    delete_loop,
    execute_one_iteration,
    install_cron,
    kill_daemon,
    list_loops,
    loop_dir,
    read_pid,
    read_state,
    remove_cron,
    remove_pid,
    run_foreground_loop,
    validate_provider,
    write_state,
)

app = typer.Typer(
    name="ralph",
    help="Minimal Ralph loop orchestrator for coding agents.",
    no_args_is_help=True,
)
console = Console()


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _resolve_prompt(prompt: str, prompt_file: Path | None) -> str:
    """Get prompt text from the positional argument or --prompt-file.

    Exits with an error if neither is provided.
    """
    if prompt_file is not None:
        if not prompt_file.exists():
            console.print(f"[red]Prompt file not found: {prompt_file}[/red]")
            raise typer.Exit(1)
        return prompt_file.read_text().strip()
    if prompt:
        return prompt
    console.print("[red]Provide a prompt argument or --prompt-file.[/red]")
    raise typer.Exit(1)


def _auto_name(name: str, prompt: str) -> str:
    """Use the explicit name if given, otherwise derive from the first words."""
    if name:
        return name
    return " ".join(prompt.split()[:6])


def _relative_time(iso_timestamp: str | None) -> str:
    """Format an ISO timestamp as a human-friendly relative string."""
    if iso_timestamp is None:
        return "never"
    try:
        dt = datetime.fromisoformat(iso_timestamp)
    except ValueError:
        return iso_timestamp
    delta = datetime.now(UTC) - dt
    seconds = int(delta.total_seconds())
    if seconds < 0:
        return "just now"
    if seconds < 60:
        return f"{seconds}s ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    return f"{days}d ago"


def _print_iteration(result: IterationResult) -> None:
    """Callback for run_foreground_loop: prints iteration status to terminal."""
    s = result.state
    iter_label = f"Iteration {s.iteration}/{s.max_iterations}"

    if result.completed:
        console.print(f"  [green]{iter_label} -- RALPH_COMPLETE[/green]")
    elif result.exit_code == 0:
        console.print(f"  {iter_label} done (exit 0)")
    else:
        console.print(f"  [yellow]{iter_label} done (exit {result.exit_code})[/yellow]")

    # Show first line of output as a preview
    lines = result.output.strip().splitlines()
    if lines:
        preview = lines[0][:120]
        console.print(f"  > {preview}")

    console.print(f"  Log: {result.log_path}")


# ─── Version callback ────────────────────────────────────────────────────────


def _version_callback(value: bool | None) -> None:
    """Print version and exit when --version is passed."""
    if value:
        console.print(f"ralph {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool | None = typer.Option(
        None, "--version", "-V",
        callback=_version_callback,
        is_eager=True,
        help="Show version and exit.",
    ),
) -> None:
    """Ralph -- minimal loop orchestrator for coding agents."""


# ─── Commands ────────────────────────────────────────────────────────────────


@app.command()
def run(
    prompt: str = typer.Argument("", help="Task prompt (or use --prompt-file)"),
    provider: str = typer.Option("claude", "--provider", "-p", help="Agent CLI to use"),
    model: str = typer.Option("", "--model", "-m", help="Model name"),
    max_iter: int = typer.Option(50, "--max-iter", "-n", help="Max iterations"),
    delay: int = typer.Option(5, "--delay", "-d", help="Seconds between iterations"),
    prompt_file: Path | None = typer.Option(
        None, "--prompt-file", "-f", help="Read prompt from file",
    ),
    name: str = typer.Option("", "--name", help="Human-readable loop name"),
    workdir: Path = typer.Option(
        ".", "--workdir", "-w", help="Working directory for the agent",
    ),
    daemon: bool = typer.Option(
        False, "--daemon", help="Run in background (survives SSH disconnect)",
    ),
) -> None:
    """Start a Ralph loop with constant delay between iterations."""
    task = _resolve_prompt(prompt, prompt_file)
    loop_name = _auto_name(name, task)

    # Validate the provider binary is installed
    try:
        validate_provider(provider)
    except (ValueError, FileNotFoundError) as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)

    state = create_loop(
        name=loop_name,
        prompt=task,
        provider=provider,
        model=model,
        workdir=str(workdir),
        max_iterations=max_iter,
        delay_seconds=delay,
    )

    console.print(f"[bold]Created loop:[/bold] {state.id}")
    console.print(f"  Provider: {provider} | Model: {model or '(default)'} | Max: {max_iter}")
    console.print(f"  Delay: {delay}s | Workdir: {state.workdir}")

    if daemon:
        pid = daemonize_loop(state.id, delay)
        console.print(f"  Daemon PID: {pid}")
        console.print()
        console.print(
            f"Loop running in background."
            f" Use [bold]ralph show {state.id}[/bold] to check status."
        )
        console.print(f"Use [bold]ralph remove {state.id}[/bold] to stop.")
        return

    console.print()
    final = run_foreground_loop(state.id, delay, on_iteration=_print_iteration)

    console.print()
    console.print(f"[bold]Loop {final.status}[/bold] after {final.iteration} iterations.")


@app.command()
def schedule(
    prompt: str = typer.Argument("", help="Task prompt (or use --prompt-file)"),
    cron: str = typer.Option(..., "--cron", "-c", help="Cron expression (e.g. '0 */2 * * *')"),
    provider: str = typer.Option("claude", "--provider", "-p", help="Agent CLI to use"),
    model: str = typer.Option("", "--model", "-m", help="Model name"),
    max_iter: int = typer.Option(50, "--max-iter", "-n", help="Max iterations"),
    prompt_file: Path | None = typer.Option(
        None, "--prompt-file", "-f", help="Read prompt from file",
    ),
    name: str = typer.Option("", "--name", help="Human-readable loop name"),
    workdir: Path = typer.Option(
        ".", "--workdir", "-w", help="Working directory for the agent",
    ),
) -> None:
    """Schedule a Ralph loop as a system cron job (survives reboots)."""
    task = _resolve_prompt(prompt, prompt_file)
    loop_name = _auto_name(name, task)

    try:
        validate_provider(provider)
    except (ValueError, FileNotFoundError) as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)

    state = create_loop(
        name=loop_name,
        prompt=task,
        provider=provider,
        model=model,
        workdir=str(workdir),
        max_iterations=max_iter,
        delay_seconds=0,
        cron_expression=cron,
    )

    install_cron(state.id, cron)

    console.print(f"[bold]Scheduled loop:[/bold] {state.id}")
    console.print(f"  Schedule: {cron}")
    console.print(f"  Provider: {provider} | Model: {model or '(default)'} | Max: {max_iter}")
    console.print(f"  Workdir: {state.workdir}")


@app.command("_run-loop", hidden=True)
def _run_loop_cmd(
    loop_id: str = typer.Argument(...),
    delay: int = typer.Option(5, "--delay"),
) -> None:
    """Internal: run a loop in the foreground. Used by --daemon subprocess."""
    try:
        run_foreground_loop(loop_id, delay)
    finally:
        remove_pid(loop_id)


@app.command()
def once(
    loop_id: str = typer.Argument(..., help="Loop ID to run one iteration of"),
) -> None:
    """Run a single iteration of a loop (used by cron, or manually)."""
    try:
        result = execute_one_iteration(loop_id)
    except FileNotFoundError:
        console.print(f"[red]Loop not found: {loop_id}[/red]")
        raise typer.Exit(1)

    s = result.state
    if s.status == "completed":
        console.print(f"Loop {loop_id}: COMPLETED (iteration {s.iteration})")
    elif s.status == "stopped":
        console.print(f"Loop {loop_id}: STOPPED at iteration {s.iteration}/{s.max_iterations}")
    else:
        console.print(f"Loop {loop_id}: iteration {s.iteration} done (exit {result.exit_code})")


@app.command("list")
def list_cmd() -> None:
    """Show all loops with status and schedule."""
    loops = list_loops()

    if not loops:
        console.print("No loops found.")
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("ID")
    table.add_column("NAME")
    table.add_column("STATUS")
    table.add_column("ITER", justify="right")
    table.add_column("SCHEDULE")
    table.add_column("LAST RUN")

    # Map status to color for quick scanning
    status_colors: dict[str, str] = {
        "running": "green",
        "completed": "blue",
        "stopped": "yellow",
        "failed": "red",
    }

    for state in loops:
        color = status_colors.get(state.status, "white")
        table.add_row(
            state.id,
            state.name[:30],
            f"[{color}]{state.status}[/{color}]",
            f"{state.iteration}/{state.max_iterations}",
            state.cron_expression or "(foreground)",
            _relative_time(state.last_run_at),
        )

    console.print(table)


@app.command()
def show(
    loop_id: str = typer.Argument(..., help="Loop ID"),
) -> None:
    """Show full details of a loop: config, memory, and latest log."""
    try:
        state = read_state(loop_id)
    except FileNotFoundError:
        console.print(f"[red]Loop not found: {loop_id}[/red]")
        raise typer.Exit(1)

    d = loop_dir(loop_id)

    # Loop metadata
    console.print(f"[bold]Loop: {state.id}[/bold]")
    console.print(f"  Name:       {state.name}")
    console.print(f"  Status:     {state.status}")
    console.print(f"  Provider:   {state.provider}")
    console.print(f"  Model:      {state.model or '(default)'}")
    console.print(f"  Workdir:    {state.workdir}")
    console.print(f"  Iteration:  {state.iteration}/{state.max_iterations}")
    console.print(f"  Schedule:   {state.cron_expression or '(foreground)'}")
    pid = read_pid(loop_id)
    if pid is not None:
        console.print(f"  Daemon:     [green]running (PID {pid})[/green]")
    console.print(f"  Created:    {state.created_at}")
    console.print(f"  Last run:   {_relative_time(state.last_run_at)}")

    # Memory tail (last 20 lines)
    memory_file = d / "memory.md"
    if memory_file.exists():
        memory = memory_file.read_text().strip()
        if memory:
            console.print()
            console.print("[bold]Memory (last 20 lines):[/bold]")
            for line in memory.splitlines()[-20:]:
                console.print(f"  {line}")

    # Latest log tail (last 10 lines)
    logs_dir = d / "logs"
    if logs_dir.exists():
        log_files = sorted(logs_dir.glob("*.log"))
        if log_files:
            latest = log_files[-1]
            content = latest.read_text().strip()
            if content:
                console.print()
                console.print(f"[bold]Latest log ({latest.name}, last 10 lines):[/bold]")
                for line in content.splitlines()[-10:]:
                    console.print(f"  {line}")


@app.command()
def remove(
    loop_id: str = typer.Argument(..., help="Loop ID"),
    keep_files: bool = typer.Option(
        False, "--keep-files", help="Keep files, only remove cron and stop",
    ),
) -> None:
    """Remove a loop: stop cron job, optionally delete all files."""
    try:
        state = read_state(loop_id)
    except FileNotFoundError:
        console.print(f"[red]Loop not found: {loop_id}[/red]")
        raise typer.Exit(1)

    # Kill daemon process if running
    if kill_daemon(loop_id):
        console.print(f"Stopped daemon for {loop_id}")

    # Remove cron entry if one exists
    if state.cron_expression:
        remove_cron(loop_id)
        console.print(f"Removed cron job for {loop_id}")

    if keep_files:
        state.status = "stopped"
        write_state(state)
        console.print(f"Loop {loop_id} stopped (files preserved at {loop_dir(loop_id)})")
    else:
        delete_loop(loop_id)
        console.print(f"Loop {loop_id} removed.")


@app.command()
def logs(
    loop_id: str = typer.Argument(..., help="Loop ID"),
    iteration: int | None = typer.Option(
        None, "--iter", "-i", help="Iteration number (default: latest)",
    ),
    tail: int = typer.Option(0, "--tail", "-t", help="Show only last N lines (0 = all)"),
) -> None:
    """View the log for a specific iteration (defaults to latest)."""
    try:
        read_state(loop_id)  # Validate the loop exists
    except FileNotFoundError:
        console.print(f"[red]Loop not found: {loop_id}[/red]")
        raise typer.Exit(1)

    logs_dir = loop_dir(loop_id) / "logs"

    if iteration is not None:
        # Specific iteration requested
        log_file = logs_dir / f"{iteration:03d}.log"
    else:
        # Default to the latest log file
        log_files = sorted(logs_dir.glob("*.log"))
        if not log_files:
            console.print("No logs yet.")
            return
        log_file = log_files[-1]

    if not log_file.exists():
        console.print(f"[red]Log file not found: {log_file.name}[/red]")
        raise typer.Exit(1)

    content = log_file.read_text()
    if tail > 0:
        content = "\n".join(content.splitlines()[-tail:])

    console.print(content)
