# Spec Delta

## Purpose

Gives the owner an at-a-glance, read-only view of what the factory is doing, reachable only from the owner's tailnet, at negligible memory cost on the shared VM.

## ADDED Requirements

### Requirement: Served by the factory process
The dashboard SHALL be served by the factory process itself, with no separate service. It SHALL listen only on a loopback address (default `127.0.0.1:8787`). It SHALL be reachable from the tailnet through `tailscale serve` over HTTPS, and SHALL NOT be exposed publicly.

#### Scenario: Not reachable externally
- **WHEN** a host outside the VM connects to port 8787 on the VM's public or tailnet IP
- **THEN** the connection is refused

#### Scenario: Reachable on tailnet
- **WHEN** the owner opens the configured `tailscale serve` HTTPS URL from a tailnet device
- **THEN** the dashboard page loads

### Requirement: Read-only
The dashboard SHALL NOT change any factory or GitHub state. It SHALL accept only GET requests.

#### Scenario: Non-GET request
- **WHEN** any client sends a POST to any dashboard path
- **THEN** the response is 405 and no state changes

### Requirement: Dashboard content
The overview page SHALL show:
- each configured repository with its active issue, phase, step, round and elapsed time;
- the queued issues in start order;
- recent runs with outcome, GitHub issue and PR links and token totals;
- issues ignored because they carry more than one state label;
- the last poll time and any recent poll errors.

#### Scenario: Active build shown
- **WHEN** issue `pi-relay#123` is running checks
- **THEN** the overview shows `pi-relay#123` under `pi-relay`, with phase build, step checks, its round number and elapsed time

### Requirement: Live output tail
For each active issue, the dashboard SHALL show the most recent lines (at most 200) of the agent's output and of the running check command. The page SHALL refresh itself every 10 seconds without client-side frameworks.

#### Scenario: Watching a build
- **WHEN** the owner keeps the issue page open during a check run
- **THEN** the page refreshes every 10 seconds and shows the newest check output lines

### Requirement: No secrets on the dashboard
The dashboard SHALL NOT display credentials, environment variables or secrets-file contents.

#### Scenario: Error page
- **WHEN** a GitHub authentication error is shown on the dashboard
- **THEN** it shows the error type and time but not the token
