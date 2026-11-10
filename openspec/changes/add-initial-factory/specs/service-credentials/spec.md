# Spec Delta

## Purpose

Defines the identities the factory acts as in Linear and GitHub, and how their secrets are stored, refreshed and kept away from agent processes.

## ADDED Requirements

### Requirement: Linear app identity
By default, the factory SHALL authenticate to Linear as an OAuth application acting as itself, using the client credentials grant. Its comments and state changes SHALL then appear as the application, not as the owner.

#### Scenario: Comment attribution
- **WHEN** the factory posts a design comment using app credentials
- **THEN** Linear shows the comment as authored by the factory application

### Requirement: Linear token refresh
When the factory uses client credentials, it SHALL obtain a new access token before the current one expires, and again after any authentication error. It SHALL retry the failed request once with the new token.

#### Scenario: Expired token
- **WHEN** a Linear request returns an authentication error
- **THEN** the factory requests a new client-credentials token, retries the request once, and logs the refresh without the token value

### Requirement: Linear personal key fallback
A configuration switch SHALL let the factory use a Linear personal API key instead of app credentials, with no other change in behaviour.

#### Scenario: Fallback configured
- **WHEN** the config selects `auth = "api_key"` and the key is present in the secrets file
- **THEN** the factory talks to Linear with that key and all lifecycle behaviour is unchanged

### Requirement: GitHub machine account
The factory SHALL push and open PRs on GitHub as one dedicated machine account. It authenticates with that account's token, and its access is limited to the repositories it has been invited to as a collaborator. Commits SHALL be authored by that account.

#### Scenario: PR authored by bot
- **WHEN** the factory opens a PR
- **THEN** the PR author is the machine account, so the owner can approve it under branch protection

#### Scenario: Repository not shared with the bot
- **WHEN** a configured repository is not accessible to the machine account
- **THEN** the factory moves issues for that repository to Blocked, with a comment saying the bot lacks access

### Requirement: Secret storage
Secrets SHALL be read only from a dedicated environment file with owner-only permissions (0600), loaded by the service manager. They SHALL NOT appear in the factory's configuration file, command-line arguments, logs, run records or dashboard.

#### Scenario: Secret file too open
- **WHEN** the secrets file is readable by group or others at startup
- **THEN** the factory refuses to start and logs the file path and required mode

#### Scenario: Logs stay clean
- **WHEN** any Linear or GitHub request fails
- **THEN** the logged error contains no token, key or client secret

### Requirement: Git push credentials stay with the factory
The factory SHALL give its git push commands the GitHub token through a credential mechanism scoped to that command. It SHALL NOT store the token in the repository's git config, remote URLs or worktree files.

#### Scenario: Worktree remote inspected
- **WHEN** an agent runs `git remote -v` or reads `.git/config` in the worktree
- **THEN** no token appears
