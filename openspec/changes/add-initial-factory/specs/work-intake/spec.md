# Spec Delta

## Purpose

Decides which Linear issues the factory works on, which repository each one targets, and in what order and how many run at once.

## ADDED Requirements

### Requirement: Periodic Linear polling
The factory SHALL poll Linear on a fixed interval (default 120 seconds, configurable). Each poll SHALL fetch every issue in a factory-relevant workflow state with a single query. No LLM is used during polling.

#### Scenario: Poll on schedule
- **WHEN** the configured poll interval has passed since the previous poll started
- **THEN** the factory fetches the issues in Ready for Design, Designing, Ready for Build, Building, Done and Canceled that it tracks or can start

#### Scenario: No work available
- **WHEN** a poll returns no startable issues and nothing is running
- **THEN** the factory starts no pi process and waits for the next interval

### Requirement: Backoff on tracker errors
When a Linear poll fails, the factory SHALL retry with exponential backoff, capped at 15 minutes. It SHALL return to the normal interval after a successful poll. Tasks already running continue during the backoff.

#### Scenario: Linear unavailable
- **WHEN** three consecutive polls fail with network or 5xx errors
- **THEN** the delay before the next poll is longer than the configured interval and at most 15 minutes, and the failures are visible in the logs and on the dashboard

#### Scenario: Recovery after outage
- **WHEN** a poll succeeds after one or more failures
- **THEN** the next poll is scheduled at the normal interval

### Requirement: Repository selection by label
Each issue SHALL name its target repository with exactly one label from the Linear label group `repo`. The label name SHALL match a repository key in the factory configuration.

#### Scenario: Mapped label
- **WHEN** an issue in Ready for Design carries the label `pi-relay` from the `repo` group and `pi-relay` is configured
- **THEN** the issue is queued for the `pi-relay` repository

#### Scenario: Missing or unknown repo label
- **WHEN** an issue enters Ready for Design or Ready for Build with no `repo` label, or with a label that has no configured repository
- **THEN** the factory moves it to Blocked and comments that the repo label is missing or unknown, naming the configured repository keys

### Requirement: Concurrency limits
The factory SHALL run at most one task per repository and at most a global maximum of concurrent tasks (default 2, configurable) across all repositories.

#### Scenario: Same repository busy
- **WHEN** a task is running for `pi-relay` and another `pi-relay` issue is ready
- **THEN** the second issue stays queued until the first task leaves its active phase

#### Scenario: Global cap reached
- **WHEN** two tasks are running for two different repositories and a third repository has a ready issue
- **THEN** the third issue stays queued until a slot frees up

### Requirement: Queue ordering
Within a repository, the factory SHALL start the ready issue with the highest Linear priority first, breaking ties by oldest creation time. When a global slot frees up, repositories SHALL be served in the order their oldest waiting issue became ready.

#### Scenario: Priority wins over age
- **WHEN** issue A (priority Urgent, created yesterday) and issue B (priority Low, created last week) are both ready for the same repository
- **THEN** issue A starts first

#### Scenario: Tie on priority
- **WHEN** two ready issues for the same repository have equal priority
- **THEN** the one created earlier starts first
