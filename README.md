# ralph-py

Minimal CLI for orchestrating [Ralph loops](https://ghuntley.com/ralph) across coding agents.

Ralph loops solve **context rot** — the degradation LLMs suffer in long sessions as conversation history fills with noise. Instead of one long session, ralph runs your agent repeatedly with fresh context, using a simple markdown file as memory between iterations.

```
pip install ralph-py
```

## Quick start

```bash
# Run a loop in the foreground (Ctrl+C to stop)
ralph run "implement user authentication with JWT tokens"

# Schedule a loop via system cron (survives reboots)
ralph schedule "run tests and fix failures" --cron "0 */2 * * *"

# Check on your loops
ralph list
ralph show <loop-id>
ralph logs <loop-id>

# Clean up
ralph remove <loop-id>
```

## How it works

```
┌──────────────────────────────────────────────────────────────┐
│                                                              │
│   ralph run "implement auth"                                 │
│       │                                                      │
│       ├── iteration 1 ─── agent runs ─── updates memory.md   │
│       │       ↓                                              │
│       │   sleep 5s                                           │
│       │       ↓                                              │
│       ├── iteration 2 ─── agent runs ─── updates memory.md   │
│       │       ↓                                              │
│       │   sleep 5s                                           │
│       │       ↓                                              │
│       ├── iteration 3 ─── agent runs ─── prints              │
│       │                                  RALPH_COMPLETE       │
│       │                                                      │
│       └── done.                                              │
│                                                              │
└──────────────────────────────────────────────────────────────┘
```

Each iteration:

1. Ralph reads your task prompt and the accumulated `memory.md`
2. Builds a composite prompt (your task + memory + iteration context + instructions)
3. Invokes the agent CLI as a blocking subprocess with fresh context
4. The agent works, updates `memory.md`, and exits
5. Ralph checks for `RALPH_COMPLETE` in stdout, updates state, repeats

**You only write the task.** Memory management, iteration tracking, and completion detection are automatic.

## Supported agents

| Agent | Provider flag | Non-interactive mode |
|-------|--------------|---------------------|
| [Claude Code](https://docs.anthropic.com/en/docs/claude-code) | `--provider claude` (default) | `claude -p "prompt"` |
| [Codex CLI](https://github.com/openai/codex) | `--provider codex` | `codex exec "prompt"` |
| [Aider](https://aider.chat) | `--provider aider` | `aider --message "prompt"` |
| [OpenCode](https://github.com/opencode-ai/opencode) | `--provider opencode` | `opencode run "prompt"` |

```bash
ralph run "refactor the database layer" --provider codex --model gpt-4.1
ralph run "add test coverage" --provider aider --model claude-sonnet-4-20250514
```

## Commands

### `ralph run`

Start a foreground loop. The agent runs repeatedly with a configurable delay between iterations.

```bash
ralph run "implement auth feature" [options]
```

| Option | Short | Default | Description |
|--------|-------|---------|-------------|
| `--provider` | `-p` | `claude` | Agent CLI to use |
| `--model` | `-m` | *(provider default)* | Model name |
| `--max-iter` | `-n` | `50` | Stop after N iterations |
| `--delay` | `-d` | `5` | Seconds between iterations |
| `--prompt-file` | `-f` | | Read prompt from a file |
| `--name` | | *(auto)* | Human-readable loop name |
| `--workdir` | `-w` | `.` | Working directory for the agent |

Ctrl+C gracefully stops — the current iteration finishes, state is saved.

### `ralph schedule`

Install a system cron job. Each tick runs one iteration. Survives reboots.

```bash
ralph schedule "run tests and fix failures" --cron "0 */2 * * *"
ralph schedule --prompt-file ./nightly-task.md --cron "0 3 * * *" --name "nightly fixes"
```

| Option | Short | Default | Description |
|--------|-------|---------|-------------|
| `--cron` | `-c` | *(required)* | Cron expression |

*All options from `ralph run` also apply (except `--delay`).*

### `ralph list`

Show all loops.

```
$ ralph list
┌──────────────────┬───────────────────┬───────────┬──────┬──────────────┬──────────┐
│ ID               │ NAME              │ STATUS    │ ITER │ SCHEDULE     │ LAST RUN │
├──────────────────┼───────────────────┼───────────┼──────┼──────────────┼──────────┤
│ implement-auth…  │ implement auth    │ running   │ 7/50 │ (foreground) │ 2m ago   │
│ nightly-fixes…   │ nightly fixes     │ running   │ 12/50│ 0 3 * * *    │ 8h ago   │
│ refactor-db…     │ refactor db layer │ completed │ 15/50│ (foreground) │ 1d ago   │
└──────────────────┴───────────────────┴───────────┴──────┴──────────────┴──────────┘
```

### `ralph show <loop-id>`

Full details: config, memory contents, and latest log tail.

### `ralph logs <loop-id>`

View the output log for an iteration.

```bash
ralph logs <loop-id>                # latest iteration
ralph logs <loop-id> --iter 3       # specific iteration
ralph logs <loop-id> --tail 20      # last 20 lines
```

### `ralph remove <loop-id>`

Stop a loop and delete its files. Use `--keep-files` to preserve logs and memory.

```bash
ralph remove <loop-id>              # remove everything
ralph remove <loop-id> --keep-files # stop cron, keep files
```

### `ralph once <loop-id>`

Run a single iteration. This is what cron calls — you can also use it manually.

## Memory

Memory is a plain markdown file (`~/.ralph/loops/<id>/memory.md`) that persists across iterations. Ralph injects it into the prompt automatically. The agent is instructed to update it after each iteration.

```markdown
## Iteration 3
- Implemented JWT token generation and validation
- All auth tests passing (12/12)
- TODO: refresh token endpoint, rate limiting

## Iteration 2
- Created login endpoint, password hashing with bcrypt
- Added test fixtures for auth module

## Iteration 1
- Set up project structure and database models
- Created user registration endpoint
```

**Memory lives outside your project directory** (`~/.ralph/loops/<id>/`), so multiple loops can target the same repo without conflicting, and nothing ralph-related ends up in your git history.

## Completion

The loop stops when any of these happen:

| Condition | Result status |
|-----------|--------------|
| Agent prints `RALPH_COMPLETE` on its own line | `completed` |
| Iteration count reaches `--max-iter` | `stopped` |
| `ralph remove <id>` | `stopped` |
| Ctrl+C during `ralph run` | `stopped` |

The agent is automatically instructed to print `RALPH_COMPLETE` when the task is done — you don't need to include this in your prompt.

## Data layout

All state lives under `~/.ralph/`:

```
~/.ralph/
└── loops/
    └── implement-auth-a1b2/
        ├── prompt.md       # Your original task (immutable)
        ├── memory.md       # Agent-maintained memory
        ├── state.json      # Iteration count, status, config
        └── logs/
            ├── 001.log     # stdout from iteration 1
            ├── 002.log
            └── 003.log
```

No databases. No daemon. Just files.

## Foreground vs. scheduled

| | `ralph run` | `ralph schedule` |
|---|---|---|
| Process | Foreground, needs a terminal | No process — cron manages it |
| Survives reboot | No | Yes |
| Timing | Fixed delay between iterations | Cron expression |
| Output | Streamed to terminal | Written to log files |
| Best for | Active development | Overnight/periodic tasks |

## Installation

Requires Python 3.11+.

```bash
# pip
pip install ralph-py

# uv
uv tool install ralph-py

# pipx
pipx install ralph-py

# From source
git clone https://github.com/SiluPanda/ralph-py.git
cd ralph-py
pip install -e .
```

## Dependencies

One runtime dependency: [`typer`](https://typer.tiangolo.com). Cron management uses the system `crontab` command directly — no extra libraries.

## What ralph doesn't do

Ralph is deliberately minimal. It does not:

- **Track costs** — delegate to the agent CLI (`--max-budget-usd` on Claude Code)
- **Manage git** — the agent handles commits, branches, etc.
- **Provide a TUI/dashboard** — use `ralph list` and `ralph logs`
- **Rotate agents** — one provider per loop
- **Retry with backoff** — fresh context *is* the retry mechanism
- **Run a daemon** — scheduling is the OS's job

## Background

The [Ralph loop](https://ghuntley.com/ralph) (named by Geoffrey Huntley, mid-2025) is a technique for running coding agents autonomously on long tasks. The insight: LLMs degrade as context fills — past 60-70% capacity, performance collapses. Ralph fixes this by restarting the agent with fresh context each iteration, using files and git as the memory layer instead of conversation history.

```bash
# The original Ralph loop is one line of bash:
while :; do cat PROMPT.md | claude -p; done
```

ralph-py wraps this pattern with scheduling, memory management, iteration tracking, and multi-agent support — while staying true to the philosophy of simplicity.

## License

MIT
