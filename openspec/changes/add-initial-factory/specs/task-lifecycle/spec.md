# Spec Delta

## Purpose

Defines the GitHub issue labels the factory reads and writes as workflow states, where humans gate progress, and how runs end, are cancelled and are cleaned up.

## ADDED Requirements

### Requirement: State labels as the only control signal
The factory SHALL start, continue and stop work based only on an issue's `factory:*` state label and whether the issue is open or closed. It SHALL NOT treat comment text as a command. Comments are read only as context for agent prompts.

#### Scenario: Comment does not trigger work
- **WHEN** a human comments "go ahead" on an issue labelled `factory:design-review` without changing its labels
- **THEN** the factory does not start the build phase

#### Scenario: Label change triggers work
- **WHEN** a human replaces `factory:design-review` with `factory:ready-for-build` on an issue
- **THEN** the factory queues the build phase for that issue

### Requirement: Required state labels
Each configured repository SHALL provide these labels, which the factory finds by name: `factory:ready-for-design`, `factory:designing`, `factory:design-review`, `factory:ready-for-build`, `factory:building`, `factory:in-review` and `factory:blocked`. Done and Canceled SHALL be represented by the issue being closed, not by labels. At startup the factory SHALL check that every required label exists in every configured repository, and SHALL NOT create any.

#### Scenario: Label missing at startup
- **WHEN** the factory starts and `acme/pi-relay` has no label `factory:design-review`
- **THEN** the factory logs which labels are missing in which repository and exits with a non-zero status without polling

### Requirement: Factory-owned transitions
The factory SHALL make exactly these transitions, each by swapping the state label:
- `factory:ready-for-design` → `factory:designing`
- `factory:designing` → `factory:design-review`
- `factory:ready-for-build` → `factory:building`
- `factory:building` → `factory:in-review`
- any active state → `factory:blocked`

A swap SHALL add the new label before removing the old one. The factory SHALL NOT close or reopen issues, and SHALL NOT change the labels of issues it is not working on. It SHALL NOT remove labels other than `factory:*` state labels.

#### Scenario: Design started
- **WHEN** the factory starts the design phase for an issue
- **THEN** it replaces `factory:ready-for-design` with `factory:designing` before launching pi

#### Scenario: PR opened
- **WHEN** the build phase opens or updates the pull request
- **THEN** the factory replaces `factory:building` with `factory:in-review`

#### Scenario: Unrelated labels untouched
- **WHEN** the factory swaps state labels on an issue that also carries `bug` and `priority:high`
- **THEN** `bug` and `priority:high` are still on the issue afterwards

### Requirement: Human gate after design
After posting a design, the factory SHALL move the issue to `factory:design-review` and add the configured owner as assignee. It SHALL NOT start the build until a human moves the issue to `factory:ready-for-build`.

#### Scenario: Design posted
- **WHEN** the design phase finishes successfully
- **THEN** the issue is labelled `factory:design-review`, the owner is an assignee, and GitHub's assignment notification tells the owner

#### Scenario: Redesign requested
- **WHEN** a human comments on a design and moves the issue back to `factory:ready-for-design`
- **THEN** the factory runs a new design round that includes the previous design and the human comments posted since

### Requirement: Human gate at merge
The factory SHALL NOT merge pull requests, push to a repository's base branch, or close issues. Done SHALL be reached only when a human merges the PR and GitHub closes the issue through the PR's `Closes #N` keyword.

#### Scenario: PR ready
- **WHEN** a PR is open and the issue is labelled `factory:in-review`
- **THEN** the factory takes no further action until the issue's labels or open state change

### Requirement: Blocked state with explanation
When the factory gives up on an issue, it SHALL move it to `factory:blocked`, add the owner as assignee, and post a comment that states the reason and how to retry.

#### Scenario: Repair rounds exhausted
- **WHEN** checks still fail after the last allowed repair round
- **THEN** the issue moves to `factory:blocked` with a comment containing the failing check names and a trimmed excerpt of their output

#### Scenario: Retry after Blocked
- **WHEN** a human replaces `factory:blocked` with `factory:ready-for-design` or `factory:ready-for-build`
- **THEN** the factory treats it like any other ready issue

### Requirement: Cancellation by label change or closure
On every poll, the factory SHALL re-read each issue it is actively working on. If a human has removed or replaced `factory:designing` or `factory:building`, or closed the issue, the factory SHALL stop that issue's pi process and check commands, post no phase result, and free its slot.

#### Scenario: Label removed mid-build
- **WHEN** a human removes all `factory:*` labels from an issue while pi is building it
- **THEN** within one poll interval the pi process and any running check commands are terminated, no comment or PR update is posted, and the worktree is kept

#### Scenario: Closed as not planned mid-design
- **WHEN** a human closes an issue as "not planned" while it is labelled `factory:designing`
- **THEN** the run is stopped and the cleanup rules for closed issues apply

### Requirement: Cleanup on closure
When an issue the factory worked on is closed, whether as completed or as not planned, the factory SHALL delete its local worktree and local branch, and move its run directory to the archive. The run record SHALL note the close reason. Remote branch deletion is left to the code host's "delete head branch on merge" setting.

#### Scenario: PR merged
- **WHEN** a merged PR closes the issue through `Closes #N`
- **THEN** on the next poll the issue's worktree is removed and its run directory is moved under the archive, with the close reason recorded as completed

#### Scenario: Closed by hand
- **WHEN** a human closes an issue the factory worked on as "not planned"
- **THEN** the same cleanup happens, with the close reason recorded as not planned
