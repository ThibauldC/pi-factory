# Spec Delta

## Purpose

Controls how the factory launches and supervises the existing `pi` install: which binary, session files, extensions and trust, models, timeouts, environment, and usage accounting.

## ADDED Requirements

### Requirement: Use the existing pi install
The factory SHALL launch pi from a configured binary path (default `~/.pi/agent/bin/pi`), as the same Unix user, with that user's pi configuration and login. It SHALL NOT install, update or patch pi.

#### Scenario: Binary missing
- **WHEN** the configured pi binary does not exist or is not executable at startup
- **THEN** the factory logs the path and exits with a non-zero status

### Requirement: One pi process per phase turn sequence
The factory SHALL run each design or build phase as one pi child process, with the issue's worktree as working directory. That process SHALL exit when the phase ends, so no pi process stays running between phases.

#### Scenario: Idle factory
- **WHEN** no issue is in Designing or Building
- **THEN** no pi child process of the factory is running

### Requirement: Persistent session file per phase
Each phase SHALL store its pi session inside the issue's run directory, under a session identifier derived from the issue, phase and round. The factory SHALL record that identifier and directory before the first prompt is sent, so that the same session can be reopened later.

#### Scenario: Session file location
- **WHEN** build round 2 of issue THI-123 starts
- **THEN** the pi session file is created under `runs/THI-123/`, with an identifier such as `THI-123-build-2`, and the run record contains both

### Requirement: Extensions and trust
pi SHALL run with the user's installed extensions and skills enabled. Project-local pi resources in the target repository SHALL NOT be trusted for factory runs. Context files such as AGENTS.md SHALL still load.

#### Scenario: Repo ships a .pi extension
- **WHEN** a target repository contains `.pi/extensions/` code
- **THEN** that code is not loaded by the factory's pi sessions

#### Scenario: Repo AGENTS.md
- **WHEN** the target repository has an AGENTS.md
- **THEN** the pi session receives it as context

### Requirement: Per-phase model selection
The model and thinking level SHALL be configurable per phase (design, build), resolved in this order:
1. repository-and-phase override;
2. global phase setting;
3. pi's own default.

#### Scenario: Repository override
- **WHEN** the global design model is A and repository `pi-relay` sets its design model to B
- **THEN** design sessions for `pi-relay` use model B, and design sessions for other repositories use model A

#### Scenario: Nothing configured
- **WHEN** no model is configured for the build phase
- **THEN** pi is launched without a model flag and uses its own default

### Requirement: Wall-clock timeouts
Each pi turn SHALL have a configurable time limit: design 15 minutes, build and repair turns 45 minutes. When the limit is reached, the factory SHALL abort the turn, terminate the process and move the issue to Blocked, stating which limit was hit.

#### Scenario: Design runs too long
- **WHEN** a design turn has run for 15 minutes without finishing
- **THEN** the factory aborts it, the pi process exits, and the issue is Blocked with a comment naming the design timeout

### Requirement: Failure detection from events
The factory SHALL decide whether a turn succeeded from pi's structured events, not only from its exit code. A turn that ends with an error, abort or length stop, or a process that exits before the turn finishes, SHALL count as failed.

#### Scenario: Provider error
- **WHEN** pi reports the final assistant message with an error stop reason
- **THEN** the phase fails, and the error text appears in the Blocked comment and the run record

### Requirement: Scrubbed environment
The factory SHALL launch pi and check commands with an allowlisted environment that excludes the factory's Linear and GitHub credentials. It SHALL NOT put credentials in their arguments, prompts or worktree files.

#### Scenario: Credentials hidden from agent
- **WHEN** a pi session runs `env`
- **THEN** the output contains neither the Linear credential variables nor the GitHub token variable

### Requirement: Token usage accounting
The factory SHALL sum the input, output and cache token counts reported by pi for each phase and round, and store them in the run record.

#### Scenario: Usage recorded
- **WHEN** a build round finishes
- **THEN** the run record contains that round's token totals by model, and the PR body shows them
