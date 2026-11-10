# Spec Delta

## Purpose

Defines the single configuration file that tells the factory which repositories it serves, how to set up and check each one, and its global limits.

## ADDED Requirements

### Requirement: Single configuration file
All factory settings except secrets SHALL live in one TOML file on the host, at a path given on the command line (default `~/.config/pi-factory/factory.toml`). Target repositories SHALL NOT need any factory-specific files.

#### Scenario: Default path
- **WHEN** the factory starts without a config argument
- **THEN** it reads `~/.config/pi-factory/factory.toml`

### Requirement: Global settings
The configuration SHALL cover:
- the Linear team and owner;
- the poll interval and global concurrency cap;
- the pi binary path;
- per-phase timeouts, the repair round limit, models and thinking levels;
- the state and worktree root directories;
- the dashboard listen address;
- the run retention period.

Each setting SHALL have the default given in the specs.

#### Scenario: Minimal config
- **WHEN** the config file sets only the Linear team, the owner and one repository
- **THEN** the factory starts with a 120-second poll interval, a global cap of 2, 2 repair rounds, a 15-minute design timeout, a 45-minute build turn timeout and 30-day retention

### Requirement: Per-repository settings
Each repository entry SHALL be keyed by its `repo` label name. It SHALL define the git clone URL, the base branch, an optional setup command and an ordered list of check commands, and it SHALL accept optional per-phase model and thinking-level overrides.

#### Scenario: Repository entry
- **WHEN** the config contains `[repos.pi-relay]` with `url`, `base_branch = "main"`, `setup`, and `checks = ["pnpm typecheck", "pnpm lint", "pnpm test", "pnpm build"]`
- **THEN** issues labelled `pi-relay` build against `main`, run that setup, and run those four checks in order

### Requirement: Validation at startup
The factory SHALL validate the configuration at startup. If any key is unknown, any required field is missing, any type is wrong or the repository list is empty, it SHALL exit with a non-zero status. The error message SHALL name the offending key.

#### Scenario: Typo in key
- **WHEN** a repository entry contains `chekcs` instead of `checks`
- **THEN** the factory exits and the error names `repos.pi-relay.chekcs` as an unknown key

#### Scenario: No checks defined
- **WHEN** a repository entry has an empty `checks` list
- **THEN** the factory exits with an error saying that repository needs at least one check command
