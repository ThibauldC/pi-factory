# Spec Delta

## Purpose

Decides which GitHub issues the factory works on, which repository each one targets, and in what order and how many run at once.

## ADDED Requirements

### Requirement: Periodic GitHub polling
The factory SHALL poll GitHub on a fixed interval (default 120 seconds, configurable). Each poll SHALL list, for every configured repository, the open issues carrying one of the polled state labels, plus a read of each issue the factory tracks. The number of requests per poll SHALL be bounded by the number of repositories times the number of polled labels, plus the number of tracked issues. No LLM is used during polling.

#### Scenario: Poll on schedule
- **WHEN** the configured poll interval has passed since the previous poll started
- **THEN** the factory fetches, for each configured repository, the open issues labelled `factory:ready-for-design`, `factory:ready-for-build`, `factory:designing` and `factory:building`, and re-reads every issue it tracks

#### Scenario: No work available
- **WHEN** a poll returns no startable issues and nothing is running
- **THEN** the factory starts no pi process and waits for the next interval

#### Scenario: Pull requests are not issues
- **WHEN** the issues endpoint returns a pull request that carries a `factory:*` label
- **THEN** the factory ignores it

### Requirement: Backoff on tracker errors
When a GitHub poll fails, including a rate-limit response, the factory SHALL retry with exponential backoff, capped at 15 minutes. It SHALL return to the normal interval after a successful poll. Tasks already running continue during the backoff.

#### Scenario: GitHub unavailable
- **WHEN** three consecutive polls fail with network or 5xx errors
- **THEN** the delay before the next poll is longer than the configured interval and at most 15 minutes, and the failures are visible in the logs and on the dashboard

#### Scenario: Rate limited
- **WHEN** a poll is answered with a rate-limit response
- **THEN** it counts as a failed poll and the same backoff applies

#### Scenario: Recovery after outage
- **WHEN** a poll succeeds after one or more failures
- **THEN** the next poll is scheduled at the normal interval

### Requirement: Repository selection by location
An issue SHALL target the repository it lives in. Only issues in repositories present in the factory configuration SHALL be considered. The factory SHALL NOT need a routing label.

#### Scenario: Issue in a configured repository
- **WHEN** an open issue in `acme/pi-relay` carries `factory:ready-for-design` and `acme/pi-relay` is configured as `pi-relay`
- **THEN** the issue is queued for the `pi-relay` repository

#### Scenario: Repository not configured
- **WHEN** an issue in a repository that is not in the configuration carries a `factory:*` label
- **THEN** the factory never sees it, because it only polls configured repositories

### Requirement: Single state label
An issue SHALL be worked on only when it carries exactly one `factory:*` state label. An issue with more than one SHALL be ignored, logged and shown on the dashboard, and SHALL NOT be started.

#### Scenario: Two state labels
- **WHEN** a human adds `factory:ready-for-build` to an issue that still carries `factory:design-review`
- **THEN** the factory does not start the build, and logs that the issue has conflicting state labels

### Requirement: Concurrency limits
The factory SHALL run at most one task per repository and at most a global maximum of concurrent tasks (default 2, configurable) across all repositories.

#### Scenario: Same repository busy
- **WHEN** a task is running for `pi-relay` and another `pi-relay` issue is ready
- **THEN** the second issue stays queued until the first task leaves its active phase

#### Scenario: Global cap reached
- **WHEN** two tasks are running for two different repositories and a third repository has a ready issue
- **THEN** the third issue stays queued until a slot frees up

### Requirement: Queue ordering
Within a repository, the factory SHALL start the ready issue with the highest priority label first (`priority:urgent`, then `high`, `medium`, `low`, then no priority label), breaking ties by oldest creation time. If an issue carries several priority labels, the highest SHALL count. When a global slot frees up, repositories SHALL be served in the order their oldest waiting issue became ready.

#### Scenario: Priority wins over age
- **WHEN** issue A (`priority:urgent`, created yesterday) and issue B (`priority:low`, created last week) are both ready for the same repository
- **THEN** issue A starts first

#### Scenario: No priority label
- **WHEN** issue A has `priority:low` and issue B has no priority label, and B is older
- **THEN** issue A starts first

#### Scenario: Tie on priority
- **WHEN** two ready issues for the same repository have equal priority
- **THEN** the one created earlier starts first
