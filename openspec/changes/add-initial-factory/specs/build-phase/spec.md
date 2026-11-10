# Spec Delta

## Purpose

Turns an approved design into a pull request whose repository checks pass. This covers implementation, check runs, repair rounds, commits, push, and continuing after PR review.

## ADDED Requirements

### Requirement: Isolated worktree per issue
Each issue SHALL get its own git worktree, created from a cached clone of the repository, on the branch name Linear generates for the issue. Linear names that branch after the issue's identifier (for example `thi-123-short-title`).

#### Scenario: First build
- **WHEN** an issue's build starts and no branch exists for it
- **THEN** the factory fetches the base branch, creates a worktree on a new branch named after Linear's branch name for the issue, and runs pi only inside that worktree

#### Scenario: Two repositories in parallel
- **WHEN** issues for two repositories are building at the same time
- **THEN** each has its own worktree directory, and neither process uses the other's directory as its working directory

### Requirement: Setup before implementation
Before the first pi turn of a build, the factory SHALL run the repository's configured setup command in the worktree. If setup fails, the issue SHALL move to Blocked with the trimmed output.

#### Scenario: Dependencies installed
- **WHEN** the repository config sets `setup = "pnpm install --frozen-lockfile"`
- **THEN** that command runs in the worktree and finishes successfully before pi is launched

### Requirement: Implementation from the design
The build prompt SHALL contain the issue, the recorded design, the human comments posted after it, the repository's check commands, and an instruction not to commit or push. The implementer session SHALL be allowed to read, edit, write and run shell commands in the worktree.

#### Scenario: Implementer edits files
- **WHEN** the implementer session finishes a turn
- **THEN** its changes are present as uncommitted modifications in the worktree, ready for the factory to check

### Requirement: Factory-run checks
After each implementation or repair turn, the factory SHALL run the repository's configured check commands in order in the worktree. It SHALL record each command's exit code, duration and trimmed output. The checks pass only if every command exits with 0.

#### Scenario: All checks pass
- **WHEN** every configured check command exits with 0
- **THEN** the factory proceeds to commit and push

#### Scenario: A check fails
- **WHEN** a check command exits non-zero
- **THEN** the factory stops running the remaining checks for that round and starts a repair round if one remains

### Requirement: Bounded repair rounds
On check failure, the factory SHALL send the failing command and the tail of its output (at most 16 KiB) back to the same implementer session as a follow-up turn. It SHALL allow at most the configured number of repair rounds (default 2), then move the issue to Blocked.

#### Scenario: Fixed in a repair round
- **WHEN** the first check run fails and the checks pass after one repair turn
- **THEN** the build continues to commit and push, and the PR body records one repair round

#### Scenario: Repairs exhausted
- **WHEN** the checks still fail after the second repair round
- **THEN** the issue moves to Blocked and nothing is pushed

### Requirement: Factory-authored commits
The factory, not the agent, SHALL create the commits, authored by the configured bot identity. Each commit message SHALL start with the issue identifier and title, plus the round number. If the agent itself creates commits, the factory SHALL keep them and add its own commit on top for any remaining changes.

#### Scenario: Commit after passing checks
- **WHEN** the checks pass for issue THI-123 titled "Add retry to fetch" on build round 1
- **THEN** the branch gains a commit whose message starts with `THI-123: Add retry to fetch (round 1)` and whose author is the bot account

### Requirement: Push and pull request
After a successful commit, the factory SHALL push the issue branch to the remote, never the base branch. It SHALL open a pull request against the base branch, or update the existing one. The PR body SHALL contain the Linear issue link, the design summary, check results per command, repair round count and token usage per phase.

#### Scenario: New PR
- **WHEN** the first build of an issue passes its checks
- **THEN** a PR from the issue branch to the base branch exists, its body contains the listed sections, and the issue is In Review with a comment linking the PR

#### Scenario: Never push to base
- **WHEN** a build pushes
- **THEN** the only ref updated on the remote is the issue branch

### Requirement: Continuation after PR review
When an issue with an existing open PR is moved back to Ready for Build, the factory SHALL continue on the same branch and worktree. It SHALL use the human comments posted since the last build as additional instructions. Then it SHALL run checks, commit, push and update the PR body.

#### Scenario: Review feedback addressed
- **WHEN** the owner comments "rename the flag to --retry" on an In Review issue and moves it to Ready for Build
- **THEN** a new build round runs on the existing branch with that comment in the prompt, and the same PR receives a new commit

### Requirement: No automatic rebase
The factory SHALL NOT rebase or force-push issue branches. If the PR cannot be merged cleanly into the base branch, the factory SHALL say so in its In Review comment and leave conflict resolution to a human.

#### Scenario: Base moved on
- **WHEN** the code host reports the PR as having conflicts after a continuation push
- **THEN** the factory's comment on the issue notes the conflict, and the branch history is not rewritten
