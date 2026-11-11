# AGENTS.md

pi-factory is a single always-on Python process (uv-managed, standard library only at
runtime) that drives the installed `pi` agent through design → build → PR flows driven by
`factory:*` labels on GitHub issues. The authoritative requirements are in
`openspec/changes/add-initial-factory/` (proposal, design, specs, tasks).

## Scope rules

- Runtime dependencies stay at zero: use the standard library (`urllib`, `tomllib`,
  `subprocess`, `http.server`, `threading`). pytest and ruff are dev-only.
- Tracker (GitHub Issues) and code host (GitHub) stay behind the `Tracker` / `CodeHost` protocols;
  phases never import the concrete adapters.
- The factory never merges PRs, never pushes to a base branch, never force-pushes, never
  deploys, and never reads comment text as a command.
- Secrets are never put in child environments, command lines, prompts, logs, run records,
  the dashboard, git config or worktree files.
- Never install, update or patch `pi`.

## Never touch production state

This code may be developed on the VM that runs the production `pi-relay` service. Do not
touch `~/factory/`, `~/.config/pi-factory/`, the `pi-relay*`/`ntfy` systemd units or
`~/.pi/agent` state while developing or testing. Tests use temporary directories, fakes
for GitHub/pi, and a temporary bare git remote. The only test that uses the real pi
binary is opt-in (`PI_FACTORY_SMOKE=1`) and runs in a scratch directory.

## Checks

Run all three before finishing a change; they must pass cleanly:

```
uv run pytest
uv run ruff check
uv run ruff format --check
```
