# Spec Delta

## Purpose

Lets the factory pick up work that a restart, crash or VM reboot interrupted, without starting the phase over or losing the agent's progress.

## ADDED Requirements

### Requirement: Step tracking in the run record
Before each step of a phase, the factory SHALL atomically record the current step in the issue's run record. The steps are `setup`, `pi_turn`, `checks`, `commit`, `push` and `pr`. Each record SHALL include the phase, the round and the session file path.

#### Scenario: Crash during checks
- **WHEN** the factory process is killed while check commands are running
- **THEN** the run record on disk names `checks` as the current step

### Requirement: Startup reconciliation
At startup, before taking new work, the factory SHALL find every issue in Designing or Building that has a run record, and recover each one. It SHALL count recovered issues toward the concurrency limits.

#### Scenario: Restart with two interrupted runs
- **WHEN** the factory starts and two issues are in Building with run records
- **THEN** both are recovered before any Ready issue is started

#### Scenario: Active state without run record
- **WHEN** an issue is in Designing or Building but has no run record on this host
- **THEN** the factory moves it to Blocked with a comment saying no local run was found

### Requirement: Resume interrupted pi sessions
When the interrupted step is `pi_turn` and the session file exists, the factory SHALL relaunch pi on that session file. It SHALL send a follow-up telling the agent the turn was interrupted, to inspect the worktree's git status and diff, and to continue the original task. Uncommitted worktree changes SHALL be kept.

#### Scenario: Build turn interrupted
- **WHEN** the VM reboots while a build turn is running, and the factory starts again
- **THEN** pi resumes from the existing session file in the existing worktree, and the build continues to checks after the resumed turn

### Requirement: Re-run factory-owned steps
When the interrupted step is `setup`, `checks`, `commit`, `push` or `pr`, the factory SHALL re-run that step without involving pi. These steps SHALL be safe to repeat: no duplicate commits, no second PR, and no duplicate comment where the API lets the factory detect one.

#### Scenario: Interrupted after push
- **WHEN** the factory crashed during `pr` after the branch was pushed
- **THEN** on restart it finds the existing PR for the branch, or creates it, and does not create a second PR

### Requirement: One resume per phase
Each phase SHALL be recovered at most once. If the session file is missing or unreadable, or a resumed phase is interrupted again, the factory SHALL move the issue to Blocked and keep the worktree and run directory.

#### Scenario: Second interruption
- **WHEN** a phase that was already resumed once is interrupted again
- **THEN** on the next startup the issue moves to Blocked with a comment saying recovery was already used

#### Scenario: Corrupt session file
- **WHEN** pi cannot load the recorded session file
- **THEN** the issue moves to Blocked with pi's error in the comment
