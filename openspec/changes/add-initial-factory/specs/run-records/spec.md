# Spec Delta

## Purpose

Keeps an auditable record of what the factory and its agents did for each issue, exposes service activity through the system journal, and bounds how much history stays on disk.

## ADDED Requirements

### Requirement: Run directory per issue
The factory SHALL keep one run directory per issue under the state root, named by the issue key (repository key and issue number, for example `pi-relay-123`). It SHALL contain:
- the run record (repository key, issue number, branch, phase, round, step, design comment ID, session file paths, recovery count, token usage, and the close reason once the issue is closed);
- the pi session files;
- the output of every setup and check command;
- copies of every comment and PR body the factory posted.

#### Scenario: Inspect a finished build
- **WHEN** an issue has reached `factory:in-review`
- **THEN** its run directory contains the design and build session files, the check outputs per round, and the posted texts

### Requirement: Atomic run record writes
The run record SHALL be written atomically, so that a crash at any point leaves either the previous or the new record on disk, never a partial one.

#### Scenario: Killed during write
- **WHEN** the factory is killed while updating a run record
- **THEN** the run record on disk is valid and parseable on the next start

### Requirement: Journal logging
The factory SHALL log one line to standard output or standard error for each poll error, phase start and end, state transition, check result, push and PR action. Each line SHALL include the issue reference (for example `pi-relay#123`) when there is one. Under systemd, these lines SHALL end up in the journal.

#### Scenario: Follow activity
- **WHEN** the owner runs `journalctl -u pi-factory -f` while an issue builds
- **THEN** they see the build start, each check result and the PR action for that issue reference

### Requirement: Archive and retention
When an issue is cleaned up, its run directory SHALL move to an archive directory. Archived runs older than the retention period (default 30 days) SHALL be deleted. Active and unarchived runs SHALL never be deleted by retention.

#### Scenario: Old archive pruned
- **WHEN** an archived run directory was archived 31 days ago and retention is 30 days
- **THEN** the factory deletes it during its next retention sweep

#### Scenario: Blocked run kept
- **WHEN** an issue has been labelled `factory:blocked` for 60 days
- **THEN** its run directory and worktree still exist
