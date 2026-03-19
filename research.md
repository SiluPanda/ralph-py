# Building a Python CLI for Ralph loop orchestration

**The Ralph loop — a bash while-loop that repeatedly invokes coding agents with fresh context — has become the dominant pattern for autonomous long-running coding tasks.** Created by Geoffrey Huntley in mid-2025 and exploding in popularity by January 2026, the pattern now has 30+ GitHub implementations, an official Anthropic plugin, and a dedicated subreddit. A well-designed Python CLI that orchestrates Ralph loops across multiple agent backends (Claude Code, Codex CLI, Aider, OpenCode) would fill a clear gap: most existing tools are bash scripts or single-agent wrappers, and developers are manually cobbling together scheduling, state management, and cost controls. This report synthesizes findings across the Ralph ecosystem, agent CLI interfaces, Python framework best practices, scheduling patterns, and architectural decisions to inform a comprehensive tech spec.

---

## The Ralph loop: origin, mechanics, and why it works

Geoffrey Huntley coined the "Ralph Wiggum" technique in mid-2025, publishing the canonical blog post at ghuntley.com/ralph on July 14, 2025. The name references both Ralph Wiggum from The Simpsons — embodying persistent iteration despite setbacks — and slang for vomiting, because Huntley said the implications of what he discovered "made me want to vomit." The technique went viral in December 2025 and January 2026, drawing coverage from VentureBeat, The Register, and widespread Hacker News discussion.

In its purest form, the Ralph loop is a single line of bash:

```bash
while :; do cat PROMPT.md | claude -p ; done
```

The core insight is deceptively simple: **LLMs degrade in long sessions as context fills with old conversation history, failed attempts, and noise** — what Huntley calls "context rot." Past 60-70% context capacity, models enter "the Dumb Zone" where performance collapses. Ralph solves this by starting each iteration with a **completely fresh context window**, treating files and git history as the memory layer instead of the LLM's conversation buffer. Each iteration reads a structured task list (typically `prd.json`), picks one incomplete task, implements it, runs tests, commits, and exits. The outer loop restarts the agent with clean context.

The architecture follows an outer/inner loop pattern. The **inner loop** is the agent's own tool-use cycle (LLM → tools → LLM → tools until the subtask is done). The **outer loop** is the Ralph loop itself, which verifies completion and decides whether to inject feedback or restart. A **completion promise** like `<promise>COMPLETE</promise>` signals the loop to stop.

Huntley describes the workflow in three phases: (1) define requirements as a structured PRD, (2) run a planning pass that generates a prioritized task list without implementation, and (3) enter building mode where the loop iterates until all tasks pass. He calculates the cost at roughly **$10.42/hour** running Claude Sonnet — comparing favorably to developer salaries. The pattern works best for mechanical tasks: framework migrations, test coverage, documentation, refactoring, and greenfield feature buildout from well-defined specs.

---

## 30+ Ralph implementations reveal clear architectural patterns

The ecosystem has produced a remarkable variety of implementations, from single-file bash scripts to multi-agent Rust orchestrators. Key projects include:

**Official/canonical sources.** Anthropic created an official Ralph Wiggum plugin for Claude Code (`anthropics/claude-code/plugins/ralph-wiggum`) that uses a Stop Hook to intercept exit attempts via exit code 2 and re-inject the original prompt. Boris Cherny, Head of Claude Code, formalized this in late 2025. Huntley published his own methodology at `ghuntley/how-to-ralph-wiggum`.

**Standalone frameworks.** `PageAI-Pro/ralph-loop` provides Docker sandbox support with one-liner agent swapping across Claude Code, Codex, Gemini, and OpenCode. `frankbria/ralph-claude-code` (v0.11.5, 566 tests) adds intelligent exit detection, rate limiting at 100 calls/hour, a circuit breaker pattern, and a semantic response analyzer. `fstandhartinger/ralph-wiggum` uses a "constitution" config file with separate loop scripts per agent and Telegram notifications. `Th0rgal/open-ralph-wiggum` provides the cleanest CLI interface with `ralph "prompt"` and supports agent rotation via `--rotation`.

**Multi-agent orchestrators.** `mikeyobrien/ralph-orchestrator` (Rust, ~253 stars) supports 7+ AI backends with a Hat System for personas, TUI mode, Telegram human-in-the-loop, and a web dashboard. `alfredolopez80/multi-agent-ralph-loop` coordinates specialized sub-agents (ralph-coder, ralph-reviewer, ralph-tester, ralph-researcher) with quality gates. Vercel Labs published `ralph-loop-agent` as a TypeScript/npm package wrapping the AI SDK with `verifyCompletion` callbacks and cost limits.

Seven distinct implementation patterns have emerged across these projects:

- **Bare bash loop** (original): simplest form, fresh context each iteration, progress via files/git
- **Stop Hook** (Anthropic plugin): loop happens inside the session via exit code interception
- **PRD-driven loop**: JSON-based task list with acceptance criteria, agent picks next incomplete story
- **Cross-model review**: one model implements, a different model reviews (SHIP or REVISE verdict)
- **Constitution/spec-driven**: single authoritative config file, stuck detection after N attempts
- **Multi-agent orchestration**: specialized sub-agents coordinated with quality gates
- **Agent rotation**: cycling between different agents/models per iteration for diversity

Common elements across all patterns include fresh context per iteration, completion signals, max iteration limits, file-based state (`progress.txt`, `prd.json`), git as memory, and backpressure via tests and linters.

---

## Agent CLI interfaces are highly wrappable

All four major coding agent CLIs support non-interactive execution with process exit, making them ideal targets for outer-loop orchestration. The key flags a wrapper must normalize:

**Claude Code** offers the richest automation surface. The `-p` / `--print` flag enables headless execution. `--output-format stream-json` emits parseable NDJSON. `--dangerously-skip-permissions` bypasses all confirmation prompts. `--model` selects the model, `--effort` controls reasoning depth (low/medium/high/max), `--max-turns` limits agent turns, and `--max-budget-usd` sets a cost ceiling. Session resume works via `-c` (continue last) or `--resume <session-id>`. Four system prompt flags allow full replacement or append. Configuration lives in `settings.json` and hierarchical `CLAUDE.md` files.

**Codex CLI** uses `codex exec "prompt"` for non-interactive mode with `--json` for NDJSON output. Its permission model separates approval modes (`untrusted`, `on-request`, `never`) from sandbox modes (`read-only`, `workspace-write`, `danger-full-access`). The `--full-auto` shortcut combines `--ask-for-approval on-request` with `--sandbox workspace-write`. Configuration uses TOML in `~/.codex/config.toml` with named profiles. It has the `--yolo` flag (alias for `--dangerously-bypass-approvals-and-sandbox`).

**Aider** uses `--message "prompt"` for single-task mode with `--yes-always` for full automation. It has no built-in permission system — all safety comes from git (auto-commit by default, trivial rollback). Model selection uses `--model` with provider-specific keys via `--api-key PROVIDER=KEY`. Configuration lives in `.aider.conf.yml`.

**OpenCode** uses `opencode run "message"` for non-interactive mode where all permissions are auto-approved. Its client/server architecture (`opencode serve` + `opencode run --attach`) eliminates cold-boot overhead. Model selection uses `--model provider/model` notation. Configuration lives in `opencode.json`.

| Capability | Claude Code | Codex CLI | Aider | OpenCode |
|---|---|---|---|---|
| Non-interactive flag | `-p "prompt"` | `codex exec "prompt"` | `--message "prompt"` | `opencode run "prompt"` |
| Skip permissions | `--dangerously-skip-permissions` | `--yolo` | `--yes-always` | Auto in run mode |
| JSON output | `--output-format stream-json` | `--json` | None | `--format json` |
| Session resume | `-c` / `--resume <id>` | `resume --last` | `--restore-chat-history` | `--continue` / `-s <id>` |
| Model selection | `--model <name>` | `-m <name>` | `--model <name>` | `-m <provider/model>` |
| Config format | JSON + CLAUDE.md | TOML + AGENTS.md | YAML + .env | JSON + .env |

Several existing orchestration tools already wrap these CLIs. **ComposioHQ/agent-orchestrator** is agent-agnostic (Claude Code, Codex, Aider) and runtime-agnostic (tmux, Docker) with a web dashboard. **ROMA** (Go) registers any CLI agent and supports direct, coordinate, and vote modes. **Claude Squad** (6.4k stars) and **cmux** (8.1k stars) manage parallel sessions via tmux. These tools validate the architectural approach but none offer the combination of a polished Python CLI with scheduling, provider abstraction, session persistence, and cost management that a purpose-built tool could provide.

---

## Recommended tech stack and framework choices

**Typer is the clear winner for the CLI framework.** Built on Click by Sebastián Ramírez (FastAPI creator), Typer uses Python type hints as the interface — function parameters with type annotations automatically become CLI arguments with auto-generated help, validation, and shell completion. It inherits Click's battle-tested ecosystem (38.7% of CLI projects use Click) while dramatically reducing boilerplate. Install with `typer[all]` to include Rich and Shellingham.

**Rich handles all terminal output.** Rich's `Live` class supports continuously updating displays for streaming LLM output as rendered Markdown. `Progress` provides multi-task progress bars with spinners. `Layout` enables split-screen dashboards. `Console.log()` adds timestamped structured logging. For a coding agent orchestrator, the combination of streaming Markdown output in a `Live` display with a fixed status bar showing iteration count, cost, and elapsed time provides excellent visibility.

**TOML is the configuration standard.** Python 3.11+ includes `tomllib` in the standard library. All modern Python tools (ruff, black, pytest) use TOML. It supports comments (unlike JSON), has explicit typing (unlike YAML's ambiguity), and integrates with `pyproject.toml` via `[tool.mytool]` sections. The recommended configuration hierarchy is: CLI arguments → environment variables → project `.ralph.toml` → user `~/.config/ralph/config.toml` → built-in defaults.

**SQLite for session state.** Python's `sqlite3` is in the standard library. WAL mode supports concurrent readers (check status while a loop runs). ACID transactions prevent corruption on crashes. A session table tracks ID, status, model, iteration count, token usage, and cost. A steps table records each iteration's input, output, duration, and result. This enables resume-from-checkpoint after interruption.

**uv for distribution.** uv (by the Astral/Ruff team) is 10-100x faster than pip/pipx and is the dominant Python package manager in 2025-2026. `uv tool install ralph` provides isolated environments with globally available binaries. Support `pipx install` as fallback and include a one-liner curl installer for onboarding.

**Pydantic v2 for config validation.** Type-safe configuration models with automatic validation, serialization, and clear error messages. Pairs naturally with Typer's type-hint approach.

---

## Scheduling, process management, and error recovery

The orchestration CLI needs robust scheduling, subprocess management, and failure handling for long-running autonomous operation.

For scheduling, **the simplest pattern is a custom while-loop with configurable delay** — matching the Ralph philosophy of simplicity. APScheduler adds unnecessary complexity for a tool whose primary mode is "run repeatedly until done." The loop should support three modes: fixed-delay (sleep N seconds between iterations), max-iterations (stop after N), and completion-signal (stop when the agent outputs a completion promise). A `schedule` library integration could optionally support cron expressions for periodic runs, but this is a secondary feature.

**Subprocess management is the critical infrastructure.** Use `subprocess.Popen` with `stdout=PIPE` and iterate stdout line-by-line for real-time streaming. For JSON-outputting agents (Claude Code, Codex), parse NDJSON as it streams. Handle timeouts via `proc.communicate(timeout=N)` with cleanup on `TimeoutExpired`. Async subprocess via `asyncio.create_subprocess_exec()` enables non-blocking operation.

**Tenacity is the standard for retry logic.** Its decorator API supports exponential backoff with jitter (`wait_random_exponential`), custom retry conditions (retry on specific exit codes or output patterns), and configurable stop strategies (after N attempts or N seconds). The AWS "Full Jitter" algorithm (`wait_random_exponential(multiplier=1, max=60)`) is recommended for API rate limits. Tenacity's `before_log` and `after_log` hooks integrate with structured logging.

**Graceful shutdown requires signal handling.** The flag-based pattern is safest: register handlers for SIGINT and SIGTERM that set a shutdown flag, check the flag between iterations, and allow the current iteration to complete before exiting. Save state to SQLite on shutdown so `--resume` works seamlessly. A context manager wrapping the loop ensures cleanup runs regardless of exit path.

---

## Community pain points that the CLI must address

Community discussions on Hacker News, Reddit (r/ClaudeAI, r/RalphCoding), and developer blogs reveal six critical pain points:

**Context rot and the Dumb Zone.** The foundational problem Ralph solves. But implementations must be careful about what context carries between iterations — `progress.txt` and `prd.json` must stay concise. The SWE-rebench finding shows a **performance ceiling at ~1M tokens** regardless of window size, and agents waste **80% of tokens on orientation/navigation** rather than problem-solving.

**Cost management is non-negotiable.** A 50-iteration Ralph loop costs $50-100+ in API fees. The CLI must provide `--max-budget-usd` that enforces a hard spending limit, per-iteration cost tracking displayed in real-time, and session cost summaries. Claude Code already supports `--max-budget-usd`; the wrapper should normalize this across providers.

**Loop detection and termination failures.** Agents repeat the same failed approach 30-47 times without detecting they're stuck. `frankbria/ralph-claude-code` addresses this with a circuit breaker pattern and semantic response analysis. The CLI should track attempted actions in structured format, trigger reflection prompts when loops are detected, and auto-pause after configurable consecutive failures.

**Premature completion and overcooking.** Agents either declare victory too early or, left running too long, add unwanted features. The CLI should support LLM-as-judge verification (a separate lightweight model call to verify the agent's work meets acceptance criteria) and clear completion criteria tied to test suites.

**Error recovery and state management.** Crashes, API outages, and rate limits interrupt long loops. The CLI must checkpoint after every iteration (SQLite), implement retry with exponential backoff on transient errors, and support `resume` that picks up from the last successful iteration.

**Security with autonomous execution.** Running `--dangerously-skip-permissions` in an infinite loop raises legitimate concerns. The CLI should default to safe modes, support command allowlists, enforce directory scoping, and provide a structured audit trail of every agent invocation.

---

## Architectural blueprint for the CLI

The recommended architecture follows the **Orchestrator-Worker pattern with a state machine**, using the Adapter pattern for provider abstraction. The guiding principle from production orchestration research: "Make orchestration deterministic; keep judgment in the agent."

```
┌──────────────────────────────┐
│     CLI Entry (Typer)         │  ralph run / ralph status / ralph resume
└──────────┬───────────────────┘
           │
┌──────────▼───────────────────┐
│   Loop Engine (State Machine) │  idle → init → running → review → done/failed
│   python-statemachine         │  + paused orthogonal state
└──────────┬───────────────────┘
           │
┌──────────▼───────────────────┐
│  Provider Abstraction Layer   │  AgentProvider ABC
│  ├─ ClaudeCodeProvider        │  Translates flags, parses output
│  ├─ CodexProvider             │
│  ├─ AiderProvider             │
│  └─ OpenCodeProvider          │
└──────────┬───────────────────┘
           │
┌──────────▼───────────────────┐
│   Infrastructure Services     │
│  SessionManager (SQLite)      │  State persistence, resume
│  GitManager                   │  Branch, commit, rollback
│  CostTracker                  │  Per-iteration + cumulative
│  OutputRenderer (Rich)        │  Live display, progress, logs
└───────────────────────────────┘
```

The **AgentProvider** abstract base class normalizes four operations across all backends: `validate_installation()` checks the CLI exists, `build_command()` translates normalized config into provider-specific flags, `execute()` runs the subprocess with streaming output capture, and `parse_output()` normalizes results into a common `AgentResult` dataclass. New providers are added by subclassing and registering in a provider registry — no modification to core code required.

The **state machine** using `python-statemachine` manages the loop lifecycle with states: `idle → initializing → running → awaiting_review → completed/failed`, plus `paused` as a reachable state from `running` or `awaiting_review`. Callbacks on state transitions handle git branch creation (on `initializing`), subprocess execution (on `running`), result validation and auto-commit (on `awaiting_review`), and state persistence (on every transition).

**Git integration** should create a dedicated branch per session (`ralph/session-{id}`), use a bot identity for commits (`ralph-agent@noreply`), include metadata as git trailers (`Agent: claude`, `Iteration: 3/10`), and tag the pre-agent state for easy rollback. The CLI should support `ralph rollback` to undo all changes from a session.

The recommended **command structure**:

```
ralph run "implement auth feature"           # Start loop
ralph run --resume [session-id]              # Resume interrupted session
ralph run --provider codex --model gpt-5     # Provider/model override
ralph run --max-iter 25 --max-budget 50      # Safety limits
ralph status                                 # Show active/recent sessions
ralph sessions list                          # All sessions with costs
ralph sessions show <id>                     # Detailed session view
ralph rollback [session-id]                  # Undo agent changes
ralph providers list                         # Available + installed providers
ralph config init                            # Create .ralph.toml
ralph config show                            # Show resolved config
```

---

## Specification decisions that will define the developer experience

**Config-driven provider management** is the highest-leverage design decision. Rather than hardcoding provider knowledge, use a TOML-based provider config that maps normalized flags to provider-specific flags. This enables users to add custom providers or override flag mappings without code changes:

```toml
[providers.claude]
binary = "claude"
non_interactive_flag = ["-p"]
output_format_flag = ["--output-format", "stream-json"]
skip_permissions_flag = ["--dangerously-skip-permissions"]
model_flag = ["--model"]
resume_flag = ["-c"]

[providers.codex]
binary = "codex"
non_interactive_cmd = ["exec"]
output_format_flag = ["--json"]
skip_permissions_flag = ["--yolo"]
model_flag = ["-m"]
```

**Three-layer testing** should cover: Typer's `CliRunner` for fast in-process unit tests of command parsing and output formatting, `pytest-subprocess` for mocked integration tests of provider adapters, and real subprocess E2E tests against a mock agent script that simulates completion signals, failures, and rate limits.

**Security defaults matter.** The CLI should never default to `--dangerously-skip-permissions`. Instead, offer a `--permission-mode` flag with `safe` (provider's default permissions), `auto-approve` (auto-approve file edits only), and `yolo` (skip all permissions, requires explicit opt-in). Rate limiting should default to a sensible cap (e.g., 50 iterations per session) that users can override.

**The completion detection system** needs multiple strategies: regex matching for completion promises (`<promise>COMPLETE</promise>`), exit code interpretation (0 = task done, non-zero = continue), test suite results (all tests pass = done), and an optional LLM-as-judge call that asks a lightweight model whether the work is complete based on the diff and task description.

---

## Conclusion

The Ralph loop represents a genuine paradigm shift in how developers interact with coding agents — away from complex multi-agent frameworks and toward simple, composable loops with fresh context. The ecosystem is young and fragmented: **30+ implementations exist but none combine polished Python CLI ergonomics with provider abstraction, robust scheduling, session persistence, cost management, and safety controls** in a single tool. The technical foundations are mature (Typer, Rich, SQLite, Tenacity, python-statemachine), and the agent CLIs are highly wrappable via their non-interactive modes with JSON output.

The highest-impact architectural decisions are the provider adapter pattern (enabling agent-agnostic operation via config-driven flag mapping), the state machine lifecycle (enabling resume, pause, and graceful shutdown), and the completion detection system (preventing both premature exit and runaway loops). The CLI should embody the Ralph philosophy — **deterministically simple in an undeterministic world** — starting with a single-agent sequential loop that works perfectly before adding multi-provider rotation, parallel execution, or multi-agent review pipelines. Community pain points around cost management, loop detection, and error recovery should be first-class features, not afterthoughts. The tool that nails developer experience here — making it as easy to run a 50-iteration overnight coding loop as it is to run a test suite — will capture significant adoption in a rapidly growing space.
