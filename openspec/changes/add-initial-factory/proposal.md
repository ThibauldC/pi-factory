# Proposal

## Why

Turning small, well-described tasks into reviewed pull requests is repetitive work that a coding agent can do, provided it runs inside a predictable loop with human gates. The existing examples are either too heavy: the AE talk's demo has three LLM agent layers, terminal-pane scraping and a container per feature. Or they don't fit: `data-software-factory` has no design phase, uses one loop per repo and only supports Azure DevOps. pi-factory is a single lightweight dispatcher that runs on an always-on free-tier VM and drives the existing `pi` install through a design → implement → test flow, with Linear as the task board.

## What Changes

- **One always-on factory process for all repos.** It polls Linear every 2 minutes, runs at most one task per repo and at most 2 tasks in total, and picks the next task by priority, then age.
- **Which repo a task targets** comes from a Linear `repo` label group.
- **The Linear workflow state drives everything:** Ready for Design → Designing → Design Review → Ready for Build → Building → In Review → Done, plus Blocked. The factory never interprets comment text as a command.
- **Design phase.** A read-only pi session posts a design to Linear as a comment: the approach, the files it expects to touch, the acceptance checks, and numbered open questions, each with a recommended answer. A human approves it by moving the issue to Ready for Build.
- **Build phase.** A pi session implements the approved design in a per-task git worktree. The factory runs the repo's configured setup and check commands, feeds failures back for up to 2 repair rounds, commits as a bot account, pushes the branch and opens a GitHub PR.
- **Changes after PR review.** You move the issue back to Ready for Build, and the factory continues on the same branch and updates the PR.
- **Done.** Merging the PR moves the issue to Done through Linear's native GitHub integration. The factory never merges.
- **Cancellation, cleanup and limits.**
  - Moving an issue out of an active state stops its run.
  - Done or Canceled issues get their worktree removed and their run logs archived.
  - Each phase has a wall-clock timeout.
- **Crash recovery.** After a restart, an interrupted pi session is resumed once from its session file. Interrupted factory-owned steps are re-run.
- **Per-phase configuration.** Model and thinking level can be set per phase, with per-repo overrides. Token usage is recorded per phase.
- **Identities.** Linear access uses an OAuth app acting as itself, with a personal API key as fallback. GitHub access uses one dedicated machine account.
- **Dashboard.** A read-only HTML dashboard is served by the factory process itself on localhost and exposed on the tailnet with `tailscale serve`.
- **Deployment.** A systemd service on the VM. Updates are done by hand with `git pull && uv sync && systemctl restart`.

## Capabilities

### New Capabilities

- `work-intake`: polling Linear, mapping issues to repos via labels, queue ordering and concurrency limits.
- `task-lifecycle`: the Linear workflow states the factory reads and writes, human gates, cancellation and cleanup.
- `design-phase`: producing and posting a read-only design for an issue, and redesigning after feedback.
- `build-phase`: implementing an approved design, running checks, repair rounds, commits, pushing and opening or updating the PR.
- `agent-sessions`: how pi is invoked: session files, extensions and trust, per-phase models, timeouts, token usage and environment scrubbing.
- `run-recovery`: resuming interrupted work after the factory restarts.
- `service-credentials`: Linear and GitHub identities, and how their secrets are stored and kept away from agents.
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
- **Linear:**
  - new workflow states and a `repo` label group;
  - an OAuth app with client credentials, which needs a public team.
- **GitHub:**
  - a machine account with a classic `repo` token, invited as a collaborator on the factory repos;
  - branch protection on `main` and "automatically delete head branches" on those repos.
- **Security trade-off accepted for v1:** the agent runs as the same user as the factory and pi-relay, with user extensions enabled, including web access. It can therefore read secrets on disk. Worktree isolation is the only boundary.
