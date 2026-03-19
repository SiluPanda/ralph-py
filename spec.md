# ralph-py — Specification

A minimal Python CLI that orchestrates Ralph loops (repeated fresh-context agent invocations) with system-managed scheduling and file-based memory.

---

## Philosophy

- **Tiny.** Under 1000 lines of Python. One package, few dependencies.
- **Files, not databases.** Each loop is a directory with markdown and JSON files. No SQLite.
- **System cron, not a daemon.** Scheduling survives reboots because the OS manages it.
- **The agent does the work.** Ralph just invokes, tracks iterations, and manages memory injection.

---

## Architecture

```
ralph (CLI)
├── run          → foreground while-loop with constant delay
├── schedule     → installs system crontab entry
├── once         → runs a single iteration (called by cron or internally by run)
├── list         → shows all loops and their status
├── show <id>    → details + recent log for a loop
├── remove <id>  → removes cron entry and optionally cleans up files
└── logs <id>    → tail iteration logs
```

All modes funnel through one function: `execute_one_iteration(loop_id)`.

---

## Data model

Each loop is a directory under `~/.ralph/loops/<loop-id>/`:

```
~/.ralph/
├── config.toml                  # Global defaults (provider, model, etc.)
└── loops/
    └── <loop-id>/
        ├── prompt.md            # Original user prompt (immutable after creation)
        ├── memory.md            # Persistent memory — read/written by the agent each iteration
        ├── state.json           # Iteration count, status, timestamps, provider config
        └── logs/
            ├── 001.log          # stdout/stderr of iteration 1
            ├── 002.log
            └── ...
```

### state.json

```json
{
  "id": "auth-feature-a1b2",
  "name": "implement auth feature",
  "status": "running",
  "provider": "claude",
  "model": "sonnet",
  "workdir": "/Users/me/project",
  "iteration": 7,
  "max_iterations": 50,
  "delay_seconds": 5,
  "cron_expression": null,
  "created_at": "2026-03-19T10:00:00Z",
  "last_run_at": "2026-03-19T10:35:00Z"
}
```

- `status`: `running` | `completed` | `stopped` | `failed`
- `cron_expression`: set only for scheduled loops; `null` for foreground loops
- `delay_seconds`: used by `ralph run`; ignored by cron

### memory.md

Starts empty. The agent is instructed to maintain it. Example after a few iterations:

```markdown
# Memory

## Iteration 3
- Implemented user registration endpoint
- Tests passing for signup flow
- TODO: login endpoint, JWT token generation

## Iteration 2
- Set up project structure, installed dependencies
- Created database models for User

## Iteration 1
- Analyzed requirements, created initial plan
```

Ralph does **not** parse or validate this file. It reads it, injects it into the prompt, and trusts the agent to keep it useful and concise.

---

## How memory injection works

**The user only writes their task.** All memory, iteration context, and completion instructions are automatically injected by ralph. The user's prompt is never modified — it's embedded inside a wrapper template.

### What the user writes

```
ralph run "implement auth feature with JWT tokens"
```

### What the agent actually receives

Ralph builds a composite prompt before each iteration:

```markdown
# Task
implement auth feature with JWT tokens

# Loop context
You are on iteration 4 of 50 in an automated loop. Each iteration starts
with fresh context — you have no memory of previous iterations except
what is recorded below.

# Memory from previous iterations
## Iteration 3
- Implemented registration endpoint, tests passing
- TODO: login endpoint, JWT token generation

## Iteration 2
- Set up project structure, installed dependencies
- Created database models for User

## Iteration 1
- Analyzed requirements, created initial plan

# End-of-iteration instructions
1. Update the memory file at ~/.ralph/loops/implement-auth-a1b2/memory.md
   with a concise summary of what you accomplished, what remains, and
   any context the next iteration needs.
2. Keep the memory file under 200 lines — summarize older entries as needed.
3. If the task is fully complete, print the line: RALPH_COMPLETE
```

The user never sees or writes any of this boilerplate. The template is built-in.

### Memory lives outside the workdir

Memory is stored at `~/.ralph/loops/<id>/memory.md`, **not** in the project directory. This means:

- **Multiple loops in the same workdir don't conflict** — each loop has its own memory file in its own loop directory
- **No workdir pollution** — no `.ralph-memory.md` or `.ralph/` in the project
- **No gitignore needed** — nothing ralph-related ends up in the repo
- The agent is given the absolute path and reads/writes it directly

### After each iteration

1. Ralph scans stdout for `RALPH_COMPLETE` — if found, sets status to `completed` and removes the cron entry (if any)
2. Ralph increments `iteration` in `state.json`
3. If `iteration >= max_iterations`, sets status to `stopped`

(Memory updates are handled by the agent writing to the file directly — ralph doesn't need to copy or parse anything.)

### Custom templates (power users)

The default template works for most cases. Power users can override it in `~/.ralph/config.toml`:

```toml
[defaults]
prompt_template = "~/.ralph/my-template.md"
```

The template file uses `{task}`, `{iteration}`, `{max_iterations}`, `{memory}`, and `{memory_path}` placeholders.

---

## Provider configuration

Providers are defined in `~/.ralph/config.toml`. Built-in defaults ship with the package; users can override or add custom providers.

```toml
[defaults]
provider = "claude"
model = ""                          # empty = provider default
max_iterations = 50
delay_seconds = 5

[providers.claude]
binary = "claude"
cmd_template = "{binary} -p {prompt} --dangerously-skip-permissions"
model_flag = "--model"

[providers.codex]
binary = "codex"
cmd_template = "{binary} exec {prompt} --yolo"
model_flag = "-m"

[providers.aider]
binary = "aider"
cmd_template = "{binary} --message {prompt} --yes-always"
model_flag = "--model"

[providers.opencode]
binary = "opencode"
cmd_template = "{binary} run {prompt}"
model_flag = "--model"
```

`cmd_template` is a Python format string. Ralph substitutes `{binary}`, `{prompt}` (shell-escaped), and appends `{model_flag} {model}` if a model is specified.

Users can add any CLI agent by adding a `[providers.myagent]` section.

---

## CLI commands

### `ralph run "prompt" [options]`

Starts a foreground Ralph loop. Creates the loop directory, then runs iterations in a while-loop with `delay_seconds` between each.

```
ralph run "implement auth feature" --provider claude --model sonnet --max-iter 25 --delay 10
ralph run --prompt-file ./task.md --name "auth feature"
```

Options:
| Flag | Short | Default | Description |
|------|-------|---------|-------------|
| `--provider` | `-p` | `claude` | Agent CLI to use |
| `--model` | `-m` | (provider default) | Model name |
| `--max-iter` | `-n` | `50` | Max iterations before stopping |
| `--delay` | `-d` | `5` | Seconds between iterations |
| `--prompt-file` | `-f` | | Read prompt from file |
| `--name` | | (auto from prompt) | Human-readable loop name |
| `--workdir` | `-w` | `.` | Working directory for the agent |

Behavior:
- Creates `~/.ralph/loops/<id>/` with prompt.md, empty memory.md, state.json
- Enters a while-loop:
  1. Calls `execute_one_iteration()` which runs the agent CLI as a **blocking subprocess** — ralph waits for the process to exit (an iteration might take 30 seconds or 30 minutes)
  2. On process exit: captures output, checks for `RALPH_COMPLETE`, updates state
  3. If still `running`: sleeps `delay_seconds`, then starts next iteration
  4. If `completed` or `stopped`: breaks out of loop
- Ctrl+C (SIGINT): sets a shutdown flag, current subprocess runs to completion, state is saved, loop exits
- Prints iteration number + first/last lines of agent output to terminal

The fixed delay is **between** iterations, not a timeout. The sequence is: agent runs (blocks) → agent exits → sleep delay → next agent run.

### `ralph schedule "prompt" [options]`

Installs a system crontab entry that runs `ralph once <id>` on the specified schedule.

```
ralph schedule "run tests and fix failures" --cron "0 */2 * * *" --provider claude
ralph schedule --prompt-file ./nightly.md --cron "0 3 * * *" --name "nightly fixes"
```

Additional option:
| Flag | Short | Default | Description |
|------|-------|---------|-------------|
| `--cron` | `-c` | (required) | Cron expression |

Behavior:
- Creates the loop directory (same as `run`)
- Sets `cron_expression` in state.json
- Writes a crontab entry: `<cron_expr> /path/to/ralph once <loop-id> >> ~/.ralph/loops/<id>/cron.log 2>&1`
- Prints the loop ID and confirms the schedule

### `ralph once <loop-id>`

Executes a single iteration of a loop. This is the command cron calls. Can also be called manually.

Behavior:
1. Read state.json — if status is not `running`, exit
2. Read memory.md and prompt.md
3. Build composite prompt (auto-injects memory, iteration context, and end-of-iteration instructions around the user's task)
4. Write composite prompt to a temp file
5. Run the provider command with `cwd` set to `workdir`, stream stdout/stderr to `logs/{iteration}.log`
6. Scan output for `RALPH_COMPLETE`
7. Update state.json (increment iteration, update last_run_at, update status if complete/stopped)
8. If complete or max iterations reached: remove cron entry, update status

Memory updates happen during step 5 — the agent writes directly to `~/.ralph/loops/<id>/memory.md` as instructed by the injected prompt. Ralph doesn't need to copy or post-process anything.

### `ralph list`

Shows all loops with status, iteration count, and schedule.

```
$ ralph list
ID              NAME                  STATUS    ITER   SCHEDULE         LAST RUN
auth-a1b2       implement auth        running   7/50   (foreground)     2 min ago
nightly-c3d4    nightly fixes         running   12/50  0 3 * * *        8 hours ago
refactor-e5f6   refactor models       completed 15/50  (foreground)     yesterday
```

### `ralph show <loop-id>`

Shows full details of a loop: state.json contents, last 20 lines of memory.md, last log entry.

### `ralph remove <loop-id> [--keep-files]`

Removes the cron entry (if any) and deletes the loop directory. `--keep-files` preserves the directory but removes the cron entry and sets status to `stopped`.

### `ralph logs <loop-id> [--iter N] [--tail N]`

Shows the log for a specific iteration (default: latest). `--tail` limits output to last N lines.

---

## Constant delay vs. scheduled modes

| Aspect | `ralph run` (foreground) | `ralph schedule` (cron) |
|--------|--------------------------|-------------------------|
| Process lifetime | Runs until done or Ctrl+C | No persistent process |
| Survives reboot | No | Yes |
| Delay between iters | Configurable (seconds) | Cron expression |
| Use case | Active development, watching output | Overnight/periodic tasks |
| Terminal needed | Yes | No |
| Output | Streamed to terminal | Written to log files |

Both modes use the same loop directory structure, memory system, and `execute_one_iteration()` core.

---

## Loop ID generation

Loop IDs are `{slug}-{short-hash}`:
- `slug`: first 3 words of the prompt (or `--name`), lowercased, hyphenated
- `short-hash`: first 4 chars of a random hex string

Examples: `implement-auth-feature-a1b2`, `nightly-fixes-c3d4`

---

## Completion detection

Simple and predictable:

1. **Explicit signal**: stdout contains `RALPH_COMPLETE` → status = `completed`
2. **Max iterations**: iteration count reaches `max_iterations` → status = `stopped`
3. **Manual stop**: `ralph remove <id>` → status = `stopped`
4. **Foreground interrupt**: Ctrl+C during `ralph run` → finishes current iteration, status = `stopped`

No LLM-as-judge, no semantic analysis. The agent is told to output `RALPH_COMPLETE` when done. If it doesn't, the max iteration limit is the backstop.

---

## Error handling

- **Agent CLI not found**: error on `ralph run` / `ralph schedule`, don't create the loop
- **Agent exits non-zero**: log the error, increment iteration, continue (the next fresh-context iteration may succeed)
- **Cron iteration fails**: logged to `cron.log` and iteration log; next cron tick retries with fresh context
- **Ctrl+C during run**: SIGINT handler sets a flag, current iteration completes, state is saved
- **Corrupt state.json**: ralph refuses to run, prints error, user can fix or `ralph remove`

No retry/backoff logic. Failures are logged and the next iteration starts clean — this *is* the Ralph philosophy. Fresh context is the retry mechanism.

---

## Dependencies

```toml
[project]
name = "ralph-py"
requires-python = ">=3.11"
dependencies = [
    "typer>=0.9",
    "python-crontab>=3.0",
]

[project.optional-dependencies]
dev = ["pytest", "ruff"]

[project.scripts]
ralph = "ralph.cli:app"
```

Total: **2 runtime dependencies.** Typer brings Rich and Click transitively. `python-crontab` provides cross-platform crontab manipulation.

---

## Project structure

```
ralph-py/
├── pyproject.toml
├── src/
│   └── ralph/
│       ├── __init__.py
│       ├── cli.py              # Typer app, all commands (~200 lines)
│       ├── loop.py             # execute_one_iteration, prompt building (~150 lines)
│       ├── providers.py        # Provider config loading, command building (~80 lines)
│       ├── state.py            # state.json read/write, loop directory management (~100 lines)
│       ├── cron.py             # Crontab install/remove/list (~60 lines)
│       └── defaults.toml       # Built-in provider definitions
├── tests/
│   ├── test_cli.py
│   ├── test_loop.py
│   ├── test_providers.py
│   ├── test_state.py
│   └── test_cron.py
├── spec.md
├── research.md
└── README.md
```

Target: **~600 lines of application code** across 6 files.

---

## What this spec intentionally omits

- **Cost tracking**: delegate to the agent CLI (`--max-budget-usd` on Claude Code)
- **Git integration**: the agent manages git, ralph doesn't
- **State machine library**: simple if/else on `state.status` is sufficient
- **Rich TUI / dashboard**: plain terminal output
- **Multi-agent review / rotation**: single provider per loop
- **Provider ABC class hierarchy**: a dict + format string is enough
- **Session resume within an iteration**: if an iteration crashes mid-way, the next iteration starts fresh
- **Lock files**: cron intervals should be wide enough that iterations don't overlap; if they do, the agent CLI handles its own locking
