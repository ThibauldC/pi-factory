# pi-factory

A single always-on Python process that turns small, well-described Linear issues into
reviewed GitHub pull requests by driving the already-installed [pi](https://pi.dev) coding
agent through a **design → implement → test** flow, with humans at two gates. Linear is the
task board and the only control signal; the factory never merges, never pushes to a base
branch and never deploys.

It runs on the free-tier Oracle VM next to the production `pi-relay` service: standard
library only, one factory process plus at most two `pi` children.

## The flow

The Linear workflow state drives everything. The factory never reads comment text as a
command; comments are only context for the agent.

| State | Set by | Meaning | Next |
|---|---|---|---|
| Ready for Design | you | Queued for a design round | Designing |
| Designing | factory | Read-only pi session is writing the design | Design Review |
| Design Review | factory | Design posted as a comment, issue assigned to you | you: Ready for Build, or back to Ready for Design |
| Ready for Build | you | Design approved, queued for implementation | Building |
| Building | factory | pi implements in a per-issue worktree; the factory runs setup and checks | In Review |
| In Review | factory | PR is open; the factory does nothing until you act | Done (merge), or back to Ready for Build |
| Blocked | factory | The factory gave up; the comment says why and how to retry | you: Ready for Design / Ready for Build |
| Done | Linear's GitHub integration | PR merged; worktree removed, run archived | — |
| Canceled | you | Run stopped; worktree removed, run archived | — |

Factory-owned transitions are exactly: Ready for Design → Designing, Designing → Design
Review, Ready for Build → Building, Building → In Review, and any active state → Blocked.
Moving an issue out of Designing or Building stops its run within one poll.

## Architecture

```
Linear ──poll (2 min)──> scheduler ──> worker thread per task ──> pi --mode rpc (child)
                           │                  │                     (design: read-only tools)
                           │                  ├─ git worktree + bot commits + push (token only
                           │                  │   through a scoped credential helper)
                           │                  └─ checks run by the factory, ≤ 2 repair rounds
                           ├─ dashboard (GET only, 127.0.0.1) ── tailscale serve
                           └─ run records on disk (atomic) ── crash recovery on restart
```

- One task per repository, at most 2 in total; next by Linear priority, then age.
- `Tracker` (Linear) and `CodeHost` (GitHub) are small protocols so Azure DevOps can be added
  later without touching the phases.
- Every external effect is repeatable after a crash without duplicates (PR lookup by head
  branch, comment markers); an interrupted pi session is resumed once.
- Children get an allowlisted environment: no factory secrets reach pi, setup or checks.

## Documentation

- [Operations guide](docs/operations.md): Linear and GitHub setup, VM install, systemd,
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

The tests need no Linear, GitHub or real model: they use in-process fake servers, a fake `pi`
that speaks the RPC protocol, and a temporary bare git remote. An opt-in smoke test against
the real pi binary: `PI_FACTORY_SMOKE=1 uv run pytest tests/test_smoke_pi.py`.

```bash
uv run pi-factory check --config ~/.config/pi-factory/factory.toml   # preflight
uv run pi-factory run   --config ~/.config/pi-factory/factory.toml   # run the service
```
