# Design

## Context

See `proposal.md` for the motivation. Constraints that shape the approach:

- **The host is the Oracle free-tier VM `oracle-homelab`.** Checked on 2026-11-10:
  - aarch64, 2 cores, 12 GB RAM (about 10 GB available), 35 GB free disk;
  - Node 24, Python 3 and git are installed; `gh`, Docker and `uv` are not;
  - it already runs `pi-relay`, `pi-relay-browser` and `ntfy` under systemd.

  CPU is the scarce resource, not memory.
- **pi 1.5.0 is installed for `ubuntu`** at `~/.pi/agent/bin/pi`, a launcher that is not on the non-interactive PATH. An idle pi process uses about 210 MB RSS. The protocol notes below come from the 1.5.1 docs on the Mac and must be confirmed against 1.5.0 (see tasks).
- **pi RPC mode** (`pi --mode rpc`) works like this:
  - commands are JSONL on stdin and responses and events are JSONL on stdout;
  - `prompt`, `follow_up`, `abort`, `get_last_assistant_text`, `get_session_stats` and `get_messages` are commands;
  - a run is finished at `agent_settled` (`aborted` flag), not at `agent_end`;
  - provider failures arrive as events, not as exit codes;
  - extensions may send extension-UI requests (`confirm`, `select`, `input`, …) that the client must answer;
  - closing stdin or sending SIGTERM starts an orderly shutdown.
- **pi session flags:**
  - `--session-dir <dir> --session-id <id>` opens the session with that ID in that directory, or creates it, so one command line serves both a first run and a resume;
  - `--no-approve` ignores trust-gated project-local resources;
  - `--tools a,b` is an allowlist that replaces built-in and extension tools alike;
  - AGENTS.md loading does not depend on trust.
- `data-software-factory` (in `~/git/ae/factory-data`) is reference material only; no code is copied. Its proven patterns are reused: a code-driven state machine instead of an LLM foreman, pi over RPC, a per-task worktree, factory-run checks, the board as the source of truth, and atomic `run.json`.

## Goals / Non-Goals

**Goals:**
- Idle RSS of the factory process is at most 60 MB (excluding children). Zero runtime dependencies beyond the Python standard library.
- The factory and everything it spawns cannot starve pi-relay of CPU or memory.
- Every external effect (state change, comment, push, PR) is repeatable after a crash without duplicates.
- Fully testable without GitHub or a real model.

**Non-Goals:**
- Sandboxing the agent beyond worktree separation (accepted for v1; see Risks).
- Azure DevOps or Linear support (only the adapter seams are kept).
- GitHub Projects v2 boards as the state store (labels are used in v1).
- Reading GitHub PR review comments, auto-rebase, auto-merge, deployments.
- Webhooks, a database, a separate dashboard service, multi-host operation.
- Re-running a phase from scratch after a crash (only session resume and step replay).

## Decisions

### D1. Python standard library only, managed by uv
`urllib.request` handles the GitHub REST API (issues and pull requests), `tomllib` the config, `subprocess` the children, `http.server` the dashboard and `json` the records. pytest is the only (dev) dependency. uv pins and installs a managed Python (≥ 3.12) on the VM, so the system Python version doesn't matter. systemd starts `.venv/bin/pi-factory` directly, so uv isn't running in the background.
*Alternatives:* `httpx` (nicer API, more resident memory and an extra dependency); TypeScript with the pi SDK in-process. The SDK option was rejected: the agent would live in the factory's memory, and a stuck agent would take the factory down with it.

### D2. Threads, not asyncio
The runtime has three kinds of thread, and every shared structure sits behind one lock:
- the scheduler thread: poll, reconcile, dispatch, cancel, retention;
- one worker thread per active task (at most the global cap);
- one dashboard thread.

Each worker drives its pi child with blocking reads of stdout lines, plus a reader thread for stderr.
*Alternative:* asyncio. It gives cleaner cancellation, but the standard library has no asyncio HTTP server, and a handful of threads stays well below the memory budget.

### D3. Module layout (`src/pi_factory/`)
| Module | Responsibility |
|---|---|
| `cli.py` | `pi-factory run [--config]`, `pi-factory check [--config]` (preflight, see D14) |
| `config.py` | Typed dataclasses, strict TOML validation (unknown keys are errors), defaults |
| `secrets.py` | Read the secrets env file, check mode 0600 |
| `scheduler.py` | Poll loop, backoff, queue ordering, per-repo and global slots, cancellation, cleanup and retention sweeps |
| `tracker/base.py`, `tracker/github.py` | `Tracker` protocol and its GitHub Issues REST implementation (state labels, comments, assignees) |
| `codehost/base.py`, `codehost/github.py` | `CodeHost` protocol and its GitHub REST implementation (find or create PR, update body, mergeable) |
| `gitops.py` | Bare cached clone, fetch, worktree add and remove, commit as bot, push through a scoped credential helper |
| `pi_rpc.py` | Spawn pi, JSONL framing, prompt and wait for `agent_settled`, abort, extension-UI auto-answers, usage, output ring buffer |
| `phases/design.py`, `phases/build.py` | Phase logic on top of the adapters |
| `runs.py` | Run directory layout, atomic run record, archive, retention |
| `recovery.py` | Startup reconciliation (spec `run-recovery`) |
| `dashboard.py` | GET-only HTML views over a scheduler snapshot |
| `prompts/*.md` | Design, build, repair, resume and format-fix prompt templates |

`Tracker` and `CodeHost` are small protocols, so an Azure DevOps adapter can be added later without touching the phases.

### D4. GitHub Issues as the tracker
GitHub Issues has no workflow states, so the state machine lives in labels. State, priority and repo are all derived from the issue itself.

- **Issue identity.** An issue is addressed by its repo key and number, written `pi-relay#123`. The *issue key* used for run directories and session IDs is `pi-relay-123`. The repo is the one the issue lives in, so there is no routing label.
- **State labels.** `factory:ready-for-design`, `factory:designing`, `factory:design-review`, `factory:ready-for-build`, `factory:building`, `factory:in-review`, `factory:blocked`. An open issue with none of them is ignored. Done and Canceled are not labels: a closed issue is finished (`state_reason` `completed` is Done, `not_planned` is Canceled).
- **Priority.** `priority:urgent`, `priority:high`, `priority:medium`, `priority:low`. No priority label ranks below Low. If an issue carries several, the highest wins.
- **Polling.** Per configured repo and per polled label (`ready-for-design`, `ready-for-build`, `designing`, `building`), one `GET /repos/{o}/{r}/issues?state=open&labels=<label>` request. Pull requests, which the issues endpoint also returns, are skipped. Each issue the factory tracks (it has a run record) additionally gets a `GET /repos/{o}/{r}/issues/{n}` request, to see label changes and closure. The request count per poll is bounded by `repos × 4 + tracked issues`, far below the 5000 requests/hour limit at a 2-minute interval. Comments (`GET .../issues/{n}/comments`) are fetched only when a phase starts.
- **State changes are label swaps.** Add the new state label first, then remove the old one, so an issue never has no state. A crash between the two calls leaves two state labels. The poll ignores an issue with more than one state label and logs it, except that recovery repairs a factory-made pair using the run record's step.
- **Human comments** are comments whose author is not the bot account **and** whose `author_association` is `OWNER`, `MEMBER` or `COLLABORATOR`. Comments from other users are not passed to the agent. The IDs of factory-posted comments are also kept in the run record, for idempotent replay.
- **Owner gate.** Design Review and Blocked add the configured owner as assignee. GitHub notifies the owner. The factory removes no other assignees.
- **Labels must exist.** The preflight checks them in every repo. The factory never creates labels.
- **Authentication.** The same machine-account token as the code host is used. Requests send `Authorization: Bearer`, `Accept: application/vnd.github+json` and a pinned `X-GitHub-Api-Version`. A 403 or 429 with rate-limit headers counts as a poll failure and uses the same backoff.
- **Closing keyword limitation.** GitHub only closes issues through `Closes #N` when the PR merges into the repo's default branch. `base_branch` must therefore be the default branch; the preflight warns otherwise.

### D5. GitHub access and git
- **Repositories** are cached as bare clones under `~/factory/repos/<key>.git`, and each issue gets a worktree at `~/factory/worktrees/<key>/<number>` on the branch `factory/<number>-<slug>`, where the slug is the issue title lowercased, reduced to `a-z0-9-` and cut to 40 characters.
- **The token reaches git** only through an inline credential helper that reads it from an environment variable set only on that git child process. It is never written into the remote URL or `.git/config`.
- **Commits** use `-c user.name/user.email` with the bot's noreply address.
- **PRs.**
  - Look up an existing PR with `GET /repos/{o}/{r}/pulls?head={o}:{branch}&state=open`, and create one with `POST /pulls` only if none exists.
  - Update the body with `PATCH`.
  - Read `mergeable` after pushing (retried once, because GitHub computes it lazily) for the conflict note.
- **The PR title** is `#123: <issue title>` and the body contains `Closes #123`. GitHub links the PR to the issue and closes the issue when the PR merges into the default branch.

### D6. pi invocation
```
<pi_path> --mode rpc --no-approve \
  --session-dir <run_dir>/sessions --session-id <issue-key>-<phase>-<round> \
  [--model <m>] [--thinking <t>] [--tools <design_tools>]        # cwd = worktree
```
- **Design** passes `--tools` set to `design.tools` (default `read,grep,find,ls`). Because the allowlist also drops extension tools, read-only extension tools such as web search must be added to `design.tools` explicitly. After the turn, `git status --porcelain` must be empty.
- **Build** passes no `--tools`, so it gets pi's defaults (`read,bash,edit,write`) plus every user extension tool. This fits "extensions on".
- **Turn protocol:**
  1. Send `prompt`.
  2. Consume events until `agent_settled`.
  3. Fetch `get_last_assistant_text`, and `get_session_stats` for usage.
  4. A turn has failed if any of these happened: `aborted`; the last assistant message's stop reason is `error`, `aborted` or `length`; the process exited before settling; the timeout fired.
- **Repair and resume** messages are sent as a new `prompt` on the same live process (repair) or on the relaunched process (resume).
- **Extension-UI requests** get automatic answers: `confirm` gets false, every other dialog is cancelled, and `notify`/`setStatus` are logged. Nothing can block on a missing human.
- **Stopping:**
  - Children start in their own process group.
  - On cancel or timeout: send `abort`, wait 10 s, close stdin and SIGTERM the group, wait 10 s, then SIGKILL.
  - On factory shutdown (SIGTERM from systemd), only stdin is closed and run records are left at `pi_turn`, so recovery resumes them.

### D7. Child environment
Children get an allowlisted environment:
- `HOME`, `USER`, `LOGNAME`, `SHELL`, `LANG`, `LC_*`, `TZ` and `XDG_*`;
- a `PATH` built from the service PATH plus `~/.local/bin` and the pi-node bin directory;
- `CI=1` and `TERM=dumb`;
- any names listed in `repos.<key>.env_passthrough`.

Everything else, including the GitHub token (`PI_FACTORY_*`), is dropped. The same rule applies to setup and check commands, which run under `/bin/sh -c` in the worktree with a per-command timeout (default 20 minutes) in their own process group.

### D8. State machine and run record
Each issue has `~/factory/state/runs/<issue-key>/run.json` (for example `runs/pi-relay-123/`), written as temp file, then `fsync`, then `rename`. It holds:
- `repo` (key), `issue_number`, `branch`, `phase` (`design` or `build`), `round`, `step`;
- `design_comment_id`, `factory_comment_ids[]`;
- `session_ids{}`, `recoveries{phase: n}`;
- `usage{phase-round: {...}}`, `pr_number`, `last_build_comment_at`.

Every external call that has side effects first records its intent: the `step` is written before the call and the result after it. Re-running a step looks up existing results first (PR by head branch; a comment by a marker line with the run ID and step, matched against recent comments) before creating new ones.

State transitions are idempotent by nature, since the factory sets a target label: adding a label that is present and removing one that is absent are both no-ops.

### D9. Scheduler loop
Each tick of the scheduler loop:
1. Poll, with exponential backoff on failure capped at 15 minutes.
2. Re-check active issues and cancel any that no longer carry `factory:designing` or `factory:building`, or that are closed.
3. Clean up tracked issues that are closed.
4. Ignore issues with more than one state label (logged and shown on the dashboard).
5. Order the queue: by priority label (urgent before high before medium before low before none), then `created_at`.
6. Fill free slots: at most one task per repo, at most `max_concurrent` in total.
7. Run a retention sweep once a day.

A worker sets Designing or Building before it launches pi, so a restarted factory can see which issues were in flight.

### D10. Recovery
This implements the `run-recovery` spec. It runs once at startup, before the first dispatch:
- If the step is `pi_turn`, relaunch pi with the same `--session-dir` and `--session-id`, then send the resume prompt.
- If the step is a factory-owned step, re-run it.
- If an issue carries two state labels and the run record shows the factory was mid-transition, remove the stale one.
- If `recoveries[phase] ≥ 1`, or the session can't be loaded, move the issue to Blocked.

### D11. Dashboard
- `ThreadingHTTPServer` on `127.0.0.1:8787`, serving only GET. Any other method gets 405.
- Pages are rendered server-side, with all values HTML-escaped and `<meta http-equiv="refresh" content="10">`.
- Data comes from a locked snapshot of scheduler state, plus run records for recent runs.
- Live tails come from ring buffers of 200 lines per active task. They are fed with assistant text deltas, tool-call names and their results (truncated), and check stdout and stderr.
- It is exposed with `tailscale serve --bg --https=<port> http://127.0.0.1:8787`, never Funnel.

### D12. Protecting the shared VM
The systemd unit `pi-factory.service` runs as `User=ubuntu` with:
- `EnvironmentFile=` pointing at `~/.config/pi-factory/secrets.env` (0600);
- `Nice=10` and `CPUWeight=50`, so pi-relay wins under contention;
- `MemoryHigh=4G` and `MemoryMax=6G` for the whole cgroup, including pi and check processes;
- `Restart=on-failure`;
- `TimeoutStopSec=30` and `KillMode=mixed`: the factory gets SIGTERM first, and children are left to the factory's orderly shutdown.

### D13. Testing
pytest suites:
- **Fake GitHub:** one in-process HTTP server recording calls, serving both the Issues API (labels, comments, assignees, closure, `author_association`) and the pull request API.
- **Fake pi:** a small Python script that speaks the RPC JSONL protocol, with scripted behaviours: success, file edits, error stop, hang, crash in the middle of a turn, extension-UI request.
- **Git:** a temporary bare "remote" repository.

Every spec scenario maps to at least one test. A separate, opt-in smoke test (`PI_FACTORY_SMOKE=1`) runs against the real pi binary with a trivial prompt, to confirm protocol compatibility on the installed version.

### D14. Preflight command
`pi-factory check` checks:
- config and secrets-file mode;
- the pi binary and its version;
- GitHub access per repo (`GET /repos/{o}/{r}` with the bot token: push permission, issues enabled);
- that every `factory:*` label exists in each repo (the `priority:*` labels are optional and only warned about), and that the owner is a valid assignee;
- a warning when `base_branch` is not the repo's default branch (closing keywords would not fire);
- that the state and worktree directories are writable.

`run` performs the same checks and exits non-zero on failure.

## Risks / Trade-offs

- **[The agent can read on-disk secrets]**: it shares the `ubuntu` user (decided, option A), so it can read the GitHub secret, pi auth and pi-relay files. *Mitigation:* keep the GitHub bot's reach to invited repos only, never pass secrets to children, and document token rotation. A dedicated user or LXD sandbox is the planned hardening path.
- **[Prompt injection through web extensions]**: content fetched through web tools could steer the agent. *Mitigation:* the factory never merges, branch protection requires the owner's approval, the bot token can't touch other repos, and design sessions only get web tools if they are explicitly allowlisted.
- **[Check commands run arbitrary repo code on a shared host]**: *Mitigation:* cgroup memory and CPU limits (D12), per-command timeouts, process-group kill.
- **[pi protocol drift between 1.5.0 and 1.5.1, or later updates]**: *Mitigation:* the fake-pi contract tests encode the expected protocol, the smoke test runs on the VM, and the pi version is logged on every start.
- **[A resumed session cut off mid-response may not load or may confuse the agent]**: *Mitigation:* only one resume is allowed, after which the issue goes to Blocked. The behaviour is checked in tasks before it is relied on.
- **[Subscription rate limits on the `openai-codex` login]**: *Mitigation:* the global cap of 2; provider errors become Blocked with the error text, with no automatic retry loop.
- **[Issue content from untrusted authors]**: on a public repo anyone can open an issue or comment, and that text reaches the agent. *Mitigation:* labelling requires triage rights, so a human always decides which issue starts; comments from users who are not owner, member or collaborator are filtered out; the design gate lets the owner see the agent's plan before any code is written.
- **[`Closes #N` does not fire]**: the PR targets a non-default branch, or the PR body was edited, so the issue stays open after merge. *Mitigation:* the preflight warns about a non-default `base_branch`, and the factory re-writes the body on every build round. The factory still cleans up whenever the issue is eventually closed.
- **[Label state is editable by anyone with triage rights, and two edits can race]**: *Mitigation:* the add-then-remove swap, ignoring issues with more than one state label, and re-reading active issues on every poll.
- **[GitHub API rate limits]**: *Mitigation:* a request count bounded by `repos × 4 + tracked issues` per poll, rate-limit responses handled as poll failures with backoff.
- **[Disk growth from `node_modules` per worktree]**: *Mitigation:* cleanup on Done and Canceled, the shared pnpm store, and a free-disk check before creating a worktree (Blocked if under 2 GB).
- **[All local state lives on one VM]**: GitHub (labels, comments, PRs) remains the source of truth; losing the VM only loses run records and session files.

## Migration Plan

1. **GitHub account:**
   - Create the machine account and invite it with write access to each factory repo.
   - Create a classic `repo` token for it.
2. **GitHub repo setup, on each factory repo:**
   - Make sure Issues are enabled.
   - Create the labels `factory:ready-for-design`, `factory:designing`, `factory:design-review`, `factory:ready-for-build`, `factory:building`, `factory:in-review`, `factory:blocked` and, optionally, `priority:urgent`, `priority:high`, `priority:medium`, `priority:low`.
   - Require one approval on the default branch and enable "automatically delete head branches".
3. **VM install:**
   - Install uv, clone `pi-factory`, run `uv sync`.
   - Write `~/.config/pi-factory/factory.toml` and `secrets.env` (0600).
   - Run `pi-factory check`.
   - Install the systemd unit and add the `tailscale serve` port.
4. **Smoke test:** run a throwaway issue on a scratch repository end to end. Then switch on the first real repository.
5. **Rollback:** `sudo systemctl disable --now pi-factory`. Issues keep their `factory:*` labels, and worktrees and runs stay on disk for inspection. Nothing else on the VM depends on the factory.

## Open Questions

- Which `tailscale serve` HTTPS port is free next to pi-relay's? Decide at install time.
- Which read-only extension tools, if any, to add to `design.tools` (for example web search)? A config value.
- The owner's GitHub login, and the bot's commit name and email. Config values.
- Later: should the state live in a GitHub Projects v2 Status field instead of labels? Only worth it if the board view matters; it would need GraphQL and a project-scoped token. The `Tracker` protocol keeps that swap local.
