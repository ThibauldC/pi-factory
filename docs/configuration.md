# Configuration

The factory reads one TOML file (default `~/.config/pi-factory/factory.toml`, override with
`pi-factory run --config <path>`) and one secrets file (default
`~/.config/pi-factory/secrets.env`, mode 0600). Unknown keys, missing required keys, wrong
types and an empty repository list stop the factory at startup, and the error names the key
by its full dotted path (for example `repos.pi-relay.chekcs`).

Target repositories need no factory-specific files.

## factory.toml

Only `[linear]` and at least one `[repos.<key>]` are required. Everything else shows its
default below.

```toml
[linear]
team = "THI"                  # Linear team key; the team must be public for OAuth apps
owner = "owner@example.com"   # Linear user (email, display name or id) that gets assigned
                              # issues in Design Review and Blocked
auth = "oauth"                # "oauth" (client credentials, default) or "api_key"

[github]
bot_name = "pi-factory-bot"   # commit author name for factory commits
bot_email = "123456+pi-factory-bot@users.noreply.github.com"

[factory]
poll_interval_seconds = 120   # Linear poll interval
max_concurrent = 2            # global cap on running tasks (one per repo at most)
pi_path = "~/.pi/agent/bin/pi"
state_root = "~/factory/state"        # runs/ and archive/ live here
worktree_root = "~/factory/worktrees"
repos_root = "~/factory/repos"        # cached bare clones
retention_days = 30                   # archived runs older than this are deleted
dashboard_listen = "127.0.0.1:8787"   # must be a loopback address

[design]
timeout_minutes = 15
tools = ["read", "grep", "find", "ls"]  # add read-only extension tools (e.g. web search) here
# model = "..."                         # unset: pi's own default
# thinking = "medium"

[build]
timeout_minutes = 45          # per implementer or repair turn
command_timeout_minutes = 20  # per setup or check command
repair_rounds = 2
# model = "..."
# thinking = "high"

[repos.pi-relay]              # key = the Linear `repo` label name
url = "https://github.com/owner/pi-relay.git"
base_branch = "main"
setup = "pnpm install --frozen-lockfile"
checks = ["pnpm typecheck", "pnpm lint", "pnpm test", "pnpm build"]  # run in order
env_passthrough = []          # extra environment variable names setup/check/pi may see

[repos.pi-relay.design]       # optional per-repo overrides
# model = "..."
[repos.pi-relay.build]
# thinking = "high"
```

Model resolution for each phase is: repository override, then the global phase setting,
then pi's own default (no `--model` flag is passed).

## secrets.env

Loaded by systemd as the unit's `EnvironmentFile=` and also read (and permission-checked) by
the factory. It must be mode 0600 or the factory refuses to start. These values never reach
pi or check commands.

```env
# Linear OAuth app (client credentials), used when linear.auth = "oauth"
PI_FACTORY_LINEAR_CLIENT_ID=your-client-id
PI_FACTORY_LINEAR_CLIENT_SECRET=your-client-secret
# Linear personal API key, used when linear.auth = "api_key"
# PI_FACTORY_LINEAR_API_KEY=lin_api_xxx
# Classic `repo` token of the GitHub machine account
PI_FACTORY_GITHUB_TOKEN=ghp_xxx
```
