# Tasks

## 1. Project scaffolding

- [x] 1.1 Create the uv project (`pyproject.toml` with `requires-python >= 3.12`, no runtime dependencies, pytest as a dev dependency, `pi-factory` console script → `pi_factory.cli:main`, `src/pi_factory/` layout per design D3). Verify that `uv sync && uv run pi-factory --help` prints usage.
- [x] 1.2 Add `AGENTS.md` (scope rules, how to run checks, "never touch production state") and `.gitignore`. Add the check commands `uv run pytest`, `uv run ruff check` and `uv run ruff format --check` (ruff as a dev dependency). Verify that all three run cleanly on the empty package.

## 2. Configuration and secrets

- [x] 2.1 Implement `config.py`: typed dataclasses, defaults from the specs, strict validation (unknown keys reported with their full dotted path, required fields, types, `github.owner` and the bot identity, `repos.<key>.repo` in `owner/name` form with duplicate detection, optional `url` derived from `repo`, at least one repo and one check per repo). Verify with tests covering every `factory-config` scenario, including the `repos.pi-relay.chekcs` typo and the malformed `repo`.
- [x] 2.2 Implement `secrets.py`: load the env file, refuse anything other than mode 0600, expose secrets only through accessor functions, and redact secret values from exception strings. Verify with tests for the open-permissions refusal and the redaction of a secret value from a raised error.
- [x] 2.3 Write `docs/configuration.md` with an annotated example `factory.toml` (GitHub owner, `repos.<key>.repo`) and `secrets.env` (one GitHub token, no Linear secrets). Verify that a test loads the documented example and that it validates.

## 3. Run records

- [x] 3.1 Implement `runs.py`: the run directory layout, the atomic run record (temp file, fsync, rename), the factory-comment ID list, archive and retention. Verify with tests for an atomic write that survives a simulated crash, archive on cleanup, the 31-day prune, and Blocked runs never being pruned.
- [x] 3.2 Add structured stage logging to stdout (one line per event, with the issue identifier). Verify with a test that captures the log lines for a scripted phase.

## 4. pi RPC driver

- [x] 4.1 Write `tests/fake_pi.py`, a fake pi that speaks RPC JSONL with these scripted behaviours: success with text, edits files, error stop reason, hangs, exits mid-turn, sends an extension-UI `confirm`, reports usage. Verify that it is driven by the tests in 4.2.
- [x] 4.2 Implement `pi_rpc.py`:
  - build the command line from design D6 (session dir and ID, `--no-approve`, model, thinking, tools);
  - spawn with the scrubbed environment from D7 in its own process group;
  - `prompt` and wait for `agent_settled`;
  - fetch the final text and usage;
  - detect failures;
  - auto-answer extension-UI requests;
  - feed a ring buffer of output;
  - escalate on timeout and cancel (abort → stdin close and SIGTERM → SIGKILL).

  Verify with tests for every `agent-sessions` scenario against `fake_pi.py`, including an `env` check showing that no `PI_FACTORY_*` variable reaches the child.
- [x] 4.3 Confirm pi 1.5.0's actual behaviour on the VM, read-only and in a scratch directory, with no factory state:
  - the RPC event and command names used in D6;
  - that `--session-dir` with `--session-id` creates a new session and reopens it;
  - that a session killed mid-turn can be reopened and continued;
  - that `--tools read,grep,find,ls` hides `bash`, `edit` and `write`.

  Record the findings in `docs/pi-compat.md` and adjust `fake_pi.py` to match. Verify with the opt-in `PI_FACTORY_SMOKE=1` test passing on the VM.

## 5. Tracker adapter (GitHub Issues)

- [x] 5.1 Define the `Tracker` protocol: list issues by state label per repo, read one issue, swap the state label (add new, then remove old), add assignee, post comment, list trusted comments, check that the required labels exist. Extend the in-process fake GitHub server (`tests/fakes/github.py`) with the Issues API: labels, comments with `author_association`, assignees, `state` and `state_reason`, and pull requests mixed into the issues list. Verify that the protocol has a fake-backed contract test suite.
- [x] 5.2 Implement `tracker/github.py`: REST over `urllib` with the machine-account token, the pinned API version header, pagination, skipping pull requests, the bounded poll request count, priority-label parsing (highest wins), rate-limit responses reported as retryable errors, and trusted-comment filtering (not the bot, association OWNER, MEMBER or COLLABORATOR). Verify with contract tests against the fake server and tests for the `work-intake` polling and ordering scenarios, the `service-credentials` trusted-author and rate-limit scenarios, and the two-state-label scenario.
- [x] 5.3 Remove the Linear adapter (`tracker/linear.py`, `tests/fakes/linear.py`, `tests/test_tracker_linear.py`) and every Linear reference in config, secrets, preflight, docs and `AGENTS.md` (its scope rule naming `Tracker (Linear)` and the intro sentence about Linear workflow states). Verify with `grep -ri linear` over `src/`, `tests/`, `docs/`, `README.md`, `AGENTS.md`, `pyproject.toml` and `deploy/` returning nothing.

## 6. Code host adapter (GitHub) and git

- [x] 6.1 Implement `gitops.py`: bare cached clone, fetch, worktree add and remove on the branch `factory/<number>-<slug>`, commit as the bot with the `#N: title (round N)` message, push through the scoped credential helper, and refusal to push the base branch. Verify with tests against a temporary bare remote, including that no token appears in `.git/config` or in `git remote -v`, and that a push of the base branch is refused.
- [x] 6.2 Define the `CodeHost` protocol and implement `codehost/github.py`: find an open PR by head branch, create a PR, update its body, read `mergeable` with one lazy retry, check repo access. Write a fake GitHub server for it. Verify with contract tests, including "a second create attempt finds the existing PR".

## 7. Design phase

- [x] 7.1 Write `prompts/design.md` and `prompts/format_fix.md`. Implement `phases/design.py`:
  - prompt assembly (issue, trusted comments, previous design, comments since);
  - read-only tool allowlist;
  - the `git status` cleanliness check;
  - section validation, with one format-fix turn;
  - post the comment and record its ID;
  - swap to `factory:design-review` and assign the owner.

  Verify with tests for every `design-phase` scenario, using the fakes.

## 8. Build phase

- [x] 8.1 Implement `phases/build.py`:
  - setup command;
  - implementer turn with the build prompt (`prompts/build.md`);
  - ordered checks with per-command timeout and trimmed output capture;
  - up to N repair turns (`prompts/repair.md`) on the same live session, with a 16 KiB tail;
  - `factory:blocked` on exhaustion.

  Verify with tests for the `build-phase` setup, checks and repair scenarios.
- [x] 8.2 Add commit, push, PR create or update with the `#N: title` PR title and a body containing `Closes #N`, design summary, checks table, repair count and token usage, the swap to `factory:in-review` and comment, and the conflict note when the PR is not mergeable. Verify with tests for the "new PR", "never push to base" and "base moved on" scenarios.
- [x] 8.3 Add continuation: an existing PR plus `factory:ready-for-build` continues on the same worktree and branch, with the comments since the last build as instructions. Verify with a test for the "review feedback addressed" scenario showing a second commit on the same PR.

## 9. Scheduler and lifecycle

- [x] 9.1 Implement `scheduler.py`:
  - poll interval with exponential backoff capped at 15 minutes;
  - ignoring issues with more than one state label;
  - priority-label-then-age ordering;
  - per-repo and global slots;
  - worker threads;
  - label transitions owned by the factory only, add-then-remove.

  Verify with tests for every `work-intake` scenario and the transition scenarios in `task-lifecycle`.
- [x] 9.2 Add cancellation (re-reading active issues each poll; label removed or replaced, or issue closed, stops the worker without posting anything) and cleanup on closure (remove the worktree and branch, archive the run with the close reason, daily retention sweep). Verify with tests for the "label removed mid-build", "closed as not planned mid-design", "PR merged" and "closed by hand" scenarios.
- [x] 9.3 Add the startup check for the required `factory:*` labels in every repo and the free-disk guard (`factory:blocked` when less than 2 GB is free before creating a worktree). Verify with tests for a missing label (non-zero exit) and for low disk.

## 10. Recovery

- [x] 10.1 Implement `recovery.py`:
  - startup reconciliation before dispatch;
  - pi-turn resume via the same session ID with `prompts/resume.md`;
  - idempotent replay of setup, checks, commit, push and PR (comment marker lookup, existing-PR lookup);
  - at most one resume per phase;
  - repair of a half-finished label swap using the run record;
  - `factory:blocked` when there is no run record or the session can't be loaded.

  Verify with tests for every `run-recovery` scenario, using `fake_pi.py`'s exit-mid-turn behaviour.
- [x] 10.2 Handle SIGTERM: stop taking new work, close pi stdin, leave run records at their current step, and exit within 30 s. Verify with a test that sends SIGTERM mid-turn, restarts, and sees the session resumed.

## 11. Dashboard

- [x] 11.1 Implement `dashboard.py`:
  - `ThreadingHTTPServer` on the configured loopback address;
  - GET only (405 otherwise);
  - an overview page with repos, active tasks, queue, issues ignored for conflicting labels, recent runs (with GitHub issue and PR links) and poll errors;
  - issue pages with 200-line live tails;
  - 10-second meta refresh, HTML-escaped output, no secrets.

  Verify with tests for every `status-dashboard` scenario that can run locally: 405 on POST, active build shown, an error shown without a token, bound only to 127.0.0.1.

## 12. CLI, packaging and deployment

- [x] 12.1 Implement `pi-factory check` (preflight per design D14, including the label checks and the default-branch warning) and `pi-factory run`, which runs the same checks and then the scheduler, recovery and dashboard. Verify with a test where `check` fails on each missing prerequisite and passes when all the fakes are set up.
- [x] 12.2 Add `deploy/pi-factory.service` (systemd unit per design D12) and write `docs/operations.md`: GitHub machine account, the `factory:*` and `priority:*` labels per repo (how to create them), branch protection and the default-branch requirement for `Closes #N`, VM install with uv, secrets file, `tailscale serve`, update (`git pull && uv sync && systemctl restart`), rollback and token rotation. Verify that `systemd-analyze verify deploy/pi-factory.service` passes on the VM, and that the guide is followed end to end in 13.1.
- [x] 12.3 Write `README.md` covering the flow (state labels table), architecture summary and links to the docs. Verify that every link resolves and that the states table matches the `task-lifecycle` spec.

## 13. Integration checks

- [ ] 13.1 Deploy to the VM following `docs/operations.md`. Check that the idle RSS of the factory process is at most 60 MB (`ps -o rss`). Check that the dashboard is reachable only through the tailnet URL.
- [ ] 13.2 Run one throwaway issue end to end on a scratch GitHub repository, going through `factory:design-review`, `factory:ready-for-build`, `factory:in-review` and a merge that closes the issue. Exercise one redesign, one continuation after review, one cancellation (label removal) and one `systemctl restart` mid-build. Verify that the issue labels and comments, the PR (including `Closes #N` closing the issue on merge) and the run directory each match the specs. Also check that a comment from a non-collaborator account stays out of the prompt.

## Workflow follow-up

- Point the factory at the first real repository after 13.2 passes.
- Archive this change with `/opsx:archive` once implemented and accepted.
