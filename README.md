# pi-factory

A single always-on Python process that turns small, well-described GitHub issues into
reviewed pull requests by driving the already-installed [pi](https://pi.dev) coding agent
through a **design → implement → test** flow, with humans at two gates. GitHub Issues is the
task board and the only control signal: the state of an issue is one `factory:*` label. The
factory never merges, never pushes to a base branch, never closes issues and never deploys.

It runs on the free-tier Oracle VM next to the production `pi-relay` service: standard
library only, one factory process plus at most two `pi` children.

## The flow

The `factory:*` label on an issue drives everything. The factory never reads comment text as a
command; comments are only context for the agent, and only comments from the repository
owner, members and collaborators reach a prompt.

| State | Set by | Meaning | Next |
|---|---|---|---|
| `factory:ready-for-design` | you | Queued for a design round | `factory:designing` |
| `factory:designing` | factory | Read-only pi session is writing the design | `factory:design-review` |
| `factory:design-review` | factory | Design posted as a comment, issue assigned to you | you: `factory:ready-for-build`, or back to `factory:ready-for-design` |
| `factory:ready-for-build` | you | Design approved, queued for implementation | `factory:building` |
| `factory:building` | factory | pi implements in a per-issue worktree; the factory runs setup and checks | `factory:in-review` |
| `factory:in-review` | factory | PR is open; the factory does nothing until you act | Done (merge), or back to `factory:ready-for-build` |
| `factory:blocked` | factory | The factory gave up; the comment says why and how to retry | you: `factory:ready-for-design` / `factory:ready-for-build` |
| Done | GitHub | The issue is closed as *completed*, because the PR's `Closes #N` fired on merge; worktree removed, run archived | — |
| Canceled | you | The issue is closed as *not planned*; the run is stopped, worktree removed, run archived | — |

Factory-owned transitions are exactly: `factory:ready-for-design` → `factory:designing`,
`factory:designing` → `factory:design-review`, `factory:ready-for-build` → `factory:building`,
`factory:building` → `factory:in-review`, and any active state → `factory:blocked`. Each is
a label swap: the new label is added first, then the old one removed. Other labels (`bug`,
`priority:*`, ...) are never touched. An issue with more than one `factory:*` label is ignored
until you fix it. Removing or replacing `factory:designing` / `factory:building`, or closing
the issue, stops its run within one poll.

Priority is a `priority:urgent|high|medium|low` label (highest wins, no label ranks last).
The repository an issue targets is the one it lives in.

## Architecture

```
GitHub ──poll (2 min)──> scheduler ──> worker thread per task ──> pi --mode rpc (child)
 Issues                    │                  │                     (design: read-only tools)
                           │                  ├─ git worktree + bot commits + push (token only
                           │                  │   through a scoped credential helper)
                           │                  └─ checks run by the factory, ≤ 2 repair rounds
                           ├─ dashboard (GET only, 127.0.0.1) ── tailscale serve
                           └─ run records on disk (atomic) ── crash recovery on restart
```

- One task per repository, at most 2 in total; next by priority label, then age.
- One GitHub machine account is both the tracker identity (labels, comments, assignee) and
  the code host identity (push, pull requests). `Tracker` (GitHub Issues) and `CodeHost`
  (GitHub) are small protocols so other trackers or Azure DevOps can be added later without
  touching the phases.
- Every external effect is repeatable after a crash without duplicates (PR lookup by head
  branch, comment markers, a recorded label swap); an interrupted pi session is resumed once.
- Children get an allowlisted environment: no factory secrets reach pi, setup or checks.

## Documentation

- [Operations guide](docs/operations.md): GitHub setup, labels, VM install, systemd,
  `tailscale serve`, updating, rollback, token rotation, security notes.
- [Configuration reference](docs/configuration.md): `factory.toml` and `secrets.env`.
- [pi compatibility notes](docs/pi-compat.md): what was verified against the installed pi.
- [Agent instructions](AGENTS.md): scope rules and the check commands.
- [systemd unit](deploy/pi-factory.service).
- The requirements live in `openspec/` (proposal, design, per-capability specs, tasks).

## Development

```bash
uv sync
uv run pytest
uv run ruff check
uv run ruff format --check
```

The tests need no GitHub account or real model: they use an in-process fake GitHub server
(issues, labels, comments and pull requests), a fake `pi` that speaks the RPC protocol, and a
temporary bare git remote. An opt-in smoke test against the real pi binary:
`PI_FACTORY_SMOKE=1 uv run pytest tests/test_smoke_pi.py`.

```bash
uv run pi-factory check --config ~/.config/pi-factory/factory.toml   # preflight
uv run pi-factory run   --config ~/.config/pi-factory/factory.toml   # run the service
```
