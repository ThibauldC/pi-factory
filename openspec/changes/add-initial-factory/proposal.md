# Proposal

## Why

Turning small, well-described tasks into reviewed pull requests is repetitive work that a coding agent can do, provided it runs inside a predictable loop with human gates. The existing examples are either too heavy: the AE talk's demo has three LLM agent layers, terminal-pane scraping and a container per feature. Or they don't fit: `data-software-factory` has no design phase, uses one loop per repo and only supports Azure DevOps. pi-factory is a single lightweight dispatcher that runs on an always-on free-tier VM and drives the existing `pi` install through a design → implement → test flow, with GitHub Issues as the task board.

## What Changes

- **One always-on factory process for all repos.** It polls the GitHub Issues of every configured repo every 2 minutes, runs at most one task per repo and at most 2 tasks in total, and picks the next task by priority label, then age.
- **Which repo a task targets** is the repo the issue lives in. There is no routing label.
- **Issue labels drive everything.** One `factory:*` state label per issue: `factory:ready-for-design` → `factory:designing` → `factory:design-review` → `factory:ready-for-build` → `factory:building` → `factory:in-review`, plus `factory:blocked`. Done and Canceled are the issue being closed (completed or not planned). The factory never interprets comment text as a command.
- **Priority** is a `priority:urgent|high|medium|low` label.
- **Design phase.** A read-only pi session posts a design to the issue as a comment: the approach, the files it expects to touch, the acceptance checks, and numbered open questions, each with a recommended answer. A human approves it by switching the label to `factory:ready-for-build`.
- **Build phase.** A pi session implements the approved design in a per-task git worktree. The factory runs the repo's configured setup and check commands, feeds failures back for up to 2 repair rounds, commits as a bot account, pushes the branch and opens a GitHub PR whose body contains `Closes #N`.
- **Changes after PR review.** You switch the label back to `factory:ready-for-build`, and the factory continues on the same branch and updates the PR.
- **Done.** Merging the PR closes the issue through GitHub's closing keyword. The factory never merges and never closes issues.
- **Cancellation, cleanup and limits.**
  - Switching an issue's label out of `factory:designing` or `factory:building`, or closing the issue, stops its run.
  - Closed issues get their worktree removed and their run logs archived.
  - Each phase has a wall-clock timeout.
- **Trusted input only.** Only comments from the owner, org members and collaborators reach the agent prompts.
- **Crash recovery.** After a restart, an interrupted pi session is resumed once from its session file. Interrupted factory-owned steps are re-run.
- **Per-phase configuration.** Model and thinking level can be set per phase, with per-repo overrides. Token usage is recorded per phase.
- **Identity.** One dedicated GitHub machine account serves both as tracker (labels, comments, assignment) and as code host (push, PRs).
- **Dashboard.** A read-only HTML dashboard is served by the factory process itself on localhost and exposed on the tailnet with `tailscale serve`.
- **Deployment.** A systemd service on the VM. Updates are done by hand with `git pull && uv sync && systemctl restart`.

## Capabilities

### New Capabilities

- `work-intake`: polling GitHub Issues, mapping issues to repos by location, queue ordering by priority label and concurrency limits.
- `task-lifecycle`: the state labels the factory reads and writes, closed issues as Done or Canceled, human gates, cancellation and cleanup.
- `design-phase`: producing and posting a read-only design for an issue, and redesigning after feedback.
- `build-phase`: implementing an approved design, running checks, repair rounds, commits, pushing and opening or updating the PR.
- `agent-sessions`: how pi is invoked: session files, extensions and trust, per-phase models, timeouts, token usage and environment scrubbing.
- `run-recovery`: resuming interrupted work after the factory restarts.
- `service-credentials`: the GitHub identity used for both tracker and code host, trusted comment authors, and how the secret is stored and kept away from agents.
- `factory-config`: the factory configuration file, the per-repo settings and validation of both.
- `run-records`: per-run audit files, service logging and retention.
- `status-dashboard`: the read-only status web page.

### Modified Capabilities

None. This is a new project.

## Impact

- **New repository** `pi-factory`: a Python package managed with uv, depending on little beyond an HTTP client.
- **Oracle VM** (shared with production pi-relay):
  - a new `pi-factory` systemd service running as `ubuntu`;
  - a new state and worktree tree under `~/factory/`;
  - one new `tailscale serve` HTTPS port;
  - up to 2 concurrent `pi` processes and check runs.
- **GitHub:**
  - a machine account with a classic `repo` token, invited as a collaborator on the factory repos;
  - the `factory:*` state labels and the `priority:*` labels created in every factory repo;
  - branch protection on the default branch, "automatically delete head branches", and `base_branch` equal to the default branch so that `Closes #N` fires on merge.
- **Security trade-off accepted for v1:** the agent runs as the same user as the factory and pi-relay, with user extensions enabled, including web access. It can therefore read secrets on disk. Worktree isolation is the only boundary.
- **Trust boundary:** applying a `factory:*` label needs triage rights on the repo, so GitHub itself decides who can start work. Issue bodies are not filtered, so on a public repo a human should read an issue before labelling it.
