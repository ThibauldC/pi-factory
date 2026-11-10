# Spec Delta

## Purpose

Defines the Linear workflow states the factory reads and writes, where humans gate progress, and how runs end, are cancelled and are cleaned up.

## ADDED Requirements

### Requirement: Workflow states as the only control signal
The factory SHALL start, continue and stop work based only on an issue's Linear workflow state. It SHALL NOT treat comment text as a command. Comments are read only as context for agent prompts.

#### Scenario: Comment does not trigger work
- **WHEN** a human comments "go ahead" on an issue in Design Review without changing its state
- **THEN** the factory does not start the build phase

#### Scenario: State change triggers work
- **WHEN** a human moves an issue from Design Review to Ready for Build
- **THEN** the factory queues the build phase for that issue

### Requirement: Required workflow states
The target Linear team SHALL provide these states, which the factory finds by name: Ready for Design, Designing, Design Review, Ready for Build, Building, In Review, Blocked, plus the team's completed (Done) and canceled states. At startup the factory SHALL check that every required state exists.

#### Scenario: State missing at startup
- **WHEN** the factory starts and the team has no state named Design Review
- **THEN** the factory logs which states are missing and exits with a non-zero status without polling

### Requirement: Factory-owned transitions
The factory SHALL make exactly these transitions:
- Ready for Design → Designing
- Designing → Design Review
- Ready for Build → Building
- Building → In Review
- any active state → Blocked

It SHALL NOT move an issue to Done or Canceled, and SHALL NOT change the state of issues it is not working on.

#### Scenario: Design started
- **WHEN** the factory starts the design phase for an issue
- **THEN** it moves the issue to Designing before launching pi

#### Scenario: PR opened
- **WHEN** the build phase opens or updates the pull request
- **THEN** the factory moves the issue to In Review

### Requirement: Human gate after design
After posting a design, the factory SHALL move the issue to Design Review and assign it to the configured owner. It SHALL NOT start the build until a human moves the issue to Ready for Build.

#### Scenario: Design posted
- **WHEN** the design phase finishes successfully
- **THEN** the issue is in Design Review, assigned to the owner, and Linear's assignment notification tells the owner

#### Scenario: Redesign requested
- **WHEN** a human comments on a design and moves the issue back to Ready for Design
- **THEN** the factory runs a new design round that includes the previous design and the human comments posted since

### Requirement: Human gate at merge
The factory SHALL NOT merge pull requests or push to a repository's base branch. Done SHALL be reached only when a human merges the PR and Linear's GitHub integration moves the issue.

#### Scenario: PR ready
- **WHEN** a PR is open and the issue is In Review
- **THEN** the factory takes no further action until the issue's state changes

### Requirement: Blocked state with explanation
When the factory gives up on an issue, it SHALL move it to Blocked, assign it to the owner, and post a comment that states the reason and how to retry.

#### Scenario: Repair rounds exhausted
- **WHEN** checks still fail after the last allowed repair round
- **THEN** the issue moves to Blocked with a comment containing the failing check names and a trimmed excerpt of their output

#### Scenario: Retry after Blocked
- **WHEN** a human moves a Blocked issue to Ready for Design or Ready for Build
- **THEN** the factory treats it like any other ready issue

### Requirement: Cancellation by state change
On every poll, the factory SHALL re-read the state of each issue it is actively working on. If a human has moved the issue out of Designing or Building, the factory SHALL stop that issue's pi process and check commands, post no phase result, and free its slot.

#### Scenario: Moved to Backlog mid-build
- **WHEN** a human moves an issue from Building to Backlog while pi is running
- **THEN** within one poll interval the pi process and any running check commands are terminated, no comment or PR update is posted, and the worktree is kept

#### Scenario: Canceled mid-design
- **WHEN** a human moves an issue from Designing to Canceled
- **THEN** the run is stopped and the cleanup rules for Canceled issues apply

### Requirement: Cleanup on completion
When an issue the factory worked on reaches Done or Canceled, the factory SHALL delete its local worktree and local branch, and move its run directory to the archive. Remote branch deletion is left to the code host's "delete head branch on merge" setting.

#### Scenario: PR merged
- **WHEN** a merged PR moves the issue to Done
- **THEN** on the next poll the issue's worktree is removed and its run directory is moved under the archive
