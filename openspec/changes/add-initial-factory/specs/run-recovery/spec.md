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
At startup, before taking new work, the factory SHALL find every issue labelled `factory:designing` or `factory:building` that has a run record, and recover each one. It SHALL count recovered issues toward the concurrency limits.

#### Scenario: Restart with two interrupted runs
- **WHEN** the factory starts and two issues are labelled `factory:building` with run records
- **THEN** both are recovered before any Ready issue is started

#### Scenario: Active state without run record
- **WHEN** an issue is labelled `factory:designing` or `factory:building` but has no run record on this host
- **THEN** the factory moves it to `factory:blocked` with a comment saying no local run was found

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
Each phase SHALL be recovered at most once. If the session file is missing or unreadable, or a resumed phase is interrupted again, the factory SHALL move the issue to `factory:blocked` and keep the worktree and run directory.

#### Scenario: Second interruption
- **WHEN** a phase that was already resumed once is interrupted again
- **THEN** on the next startup the issue moves to `factory:blocked` with a comment saying recovery was already used

#### Scenario: Corrupt session file
- **WHEN** pi cannot load the recorded session file
- **THEN** the issue moves to `factory:blocked` with pi's error in the comment

### Requirement: Repair an interrupted label swap
If the factory was interrupted between adding a new state label and removing the old one, leaving an issue with two state labels, recovery SHALL use the run record to decide which label is current and remove the stale one. It SHALL NOT remove a label the factory did not add.

#### Scenario: Crash during a label swap
- **WHEN** the factory crashed after adding `factory:building` but before removing `factory:ready-for-build`, and restarts
- **THEN** recovery removes `factory:ready-for-build` and continues the build as for any interrupted build

#### Scenario: Human-made conflict
- **WHEN** an issue carries two state labels and the run record shows no swap in progress
- **THEN** recovery leaves the labels alone and the issue stays ignored until a human fixes them
