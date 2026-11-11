# Operations guide

How to set up Linear and GitHub, install pi-factory on the VM, update it, roll it back and
rotate its tokens. The configuration reference is [configuration.md](configuration.md); the
pi protocol notes are in [pi-compat.md](pi-compat.md).

## 1. Linear setup

1. **Workflow states.** Add these states to the team (names must match exactly): `Ready for
   Design`, `Designing`, `Design Review`, `Ready for Build`, `Building`, `In Review`,
   `Blocked`. The team's existing completed (`Done`) and canceled states are used as they are.
2. **Public team.** OAuth apps using the client-credentials grant only see **public** teams.
   Make the factory's team public (Team settings → General → Privacy), or use a personal API
   key instead (`linear.auth = "api_key"`, no other change in behaviour).
3. **`repo` label group.** Create a label *group* named exactly `repo`, with one label per
   target repository. The label name is the repository key in `factory.toml`
   (`[repos.<key>]`). Every issue the factory works on needs exactly one of them.
4. **OAuth app.** Settings → API → OAuth applications → new application, enable the
   *client credentials* grant, and note the client ID and secret for `secrets.env`. The
   factory requests the scopes `read,write` and acts as the application itself, so its
   comments and state changes appear as the app and not as you. (The Free plan may not allow
   client-credentials apps; if it does not, use `auth = "api_key"` with a personal API key.)
5. **GitHub integration.** Install Linear's GitHub integration for every factory repository
   and enable automatic status updates. PR titles are `THI-123: <issue title>` and branches
   use Linear's branch name, so merging a PR moves the issue to **Done**. The factory itself
   never merges and never moves an issue to Done; without the integration, issues would stay
   In Review after a merge. (`pi-factory check` cannot verify this and only reminds you.)

## 2. GitHub setup

1. Create a **machine account** (for example `yourname-factory-bot`). It authors the commits
   and the pull requests, so you can approve them under branch protection.
2. Invite it as a **collaborator with write access** to each factory repository, and accept
   the invitation. Its reach is limited to the repositories it was invited to.
3. Create a **classic personal access token** for it with the `repo` scope. This goes into
   `secrets.env` as `PI_FACTORY_GITHUB_TOKEN`.
4. On each repository: branch protection on the base branch requiring **one approval** (the
   bot cannot approve its own PR; you do), and the setting **Automatically delete head
   branches** (the factory only deletes its local branch).
5. Set `github.bot_name` / `github.bot_email` in `factory.toml` to the bot's name and its
   `<id>+<login>@users.noreply.github.com` address so commits are attributed to it.

## 3. Install on the VM

pi must already be installed for the `ubuntu` user (`~/.pi/agent/bin/pi`); the factory never
installs, updates or patches it.

```bash
# uv (installs a managed Python >= 3.12 on demand)
curl -LsSf https://astral.sh/uv/install.sh | sh

git clone <pi-factory repository> ~/git/pi-factory
cd ~/git/pi-factory && uv sync

mkdir -p ~/.config/pi-factory
$EDITOR ~/.config/pi-factory/factory.toml   # see configuration.md
install -m 0600 /dev/null ~/.config/pi-factory/secrets.env
$EDITOR ~/.config/pi-factory/secrets.env    # must stay mode 0600

uv run pi-factory check                     # preflight; fix everything marked FAIL
```

`pi-factory check` verifies the config and secrets file, the pi binary (and logs its
version), the Linear states, `repo` labels and owner, GitHub push access per repository, and
that the state, worktree and clone directories are writable. `pi-factory run` performs the
same checks first and exits non-zero if any fail.

Install the service (it runs as `ubuntu`, with low CPU priority and a memory ceiling so the
production `pi-relay` service keeps winning):

```bash
sudo cp deploy/pi-factory.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pi-factory
journalctl -u pi-factory -f
```

Expose the dashboard to the tailnet (never Funnel). It listens only on `127.0.0.1:8787`.
Pick an HTTPS port that is free next to the ones pi-relay already serves (`tailscale serve
status`):

```bash
sudo tailscale serve --bg --https=<port> http://127.0.0.1:8787
```

### Smoke test

Create a throwaway issue labelled with a scratch repository, move it to *Ready for Design*,
and follow it through Design Review → Ready for Build → In Review → Done (merge the PR).
Only then label real work. The idle factory process should use at most about 60 MB RSS:

```bash
ps -o rss,cmd -p $(systemctl show -p MainPID --value pi-factory)
```

## 4. Day-to-day

| You do | The factory does |
|---|---|
| Move an issue to **Ready for Design** | Designs it read-only, posts the design as a comment, moves it to **Design Review** and assigns you. |
| Answer the open questions in a comment, then move it to **Ready for Build** | Implements, runs the repo checks (up to 2 repair rounds), pushes the branch, opens the PR, moves it to **In Review**. |
| Comment on the design and move it back to **Ready for Design** | Runs a new design round that includes the previous design and your comments. |
| Comment on the PR's Linear issue and move it to **Ready for Build** | Continues on the same branch and updates the same PR. |
| Merge the PR | Linear moves the issue to **Done**; the factory removes the worktree and archives the run. |
| Move an issue out of Designing/Building | Stops the run within one poll; posts nothing. |

An issue in **Blocked** has a comment saying why and how to retry: fix the cause, then move
it to Ready for Design or Ready for Build. Comment text is never treated as a command; only
workflow states drive the factory.

Where to look: `journalctl -u pi-factory`, the dashboard (`https://<host>.<tailnet>.ts.net:<port>`),
and `~/factory/state/runs/<ISSUE>/` (run record, pi sessions, command outputs, posted texts).
Finished runs move to `~/factory/state/archive/` and are deleted after the retention period
(default 30 days); Blocked and active runs are never pruned.

## 5. Updating

```bash
cd ~/git/pi-factory && git pull && uv sync && sudo systemctl restart pi-factory
```

Restarting is safe at any time. On stop the factory closes pi's stdin and leaves each run at
its current step; on start it resumes each interrupted pi session once and re-runs
interrupted factory steps (setup, checks, commit, push, PR). A phase that is interrupted a
second time goes to Blocked.

## 6. Rollback

```bash
sudo systemctl disable --now pi-factory
```

Issues stay in their Linear states, and worktrees and runs stay on disk for inspection.
Nothing else on the VM depends on the factory. To remove the tailnet port:
`sudo tailscale serve --https=<port> off`.

## 7. Token rotation

Edit `~/.config/pi-factory/secrets.env` (keep mode 0600), then `sudo systemctl restart
pi-factory`.

- **GitHub:** create a new classic token for the machine account, replace
  `PI_FACTORY_GITHUB_TOKEN`, delete the old token on GitHub.
- **Linear OAuth app:** rotate the client secret in Linear, replace
  `PI_FACTORY_LINEAR_CLIENT_SECRET`. (Access tokens are fetched on demand and refreshed
  automatically.)
- **Linear API key (fallback):** replace `PI_FACTORY_LINEAR_API_KEY`.

## 8. Security notes (accepted for v1)

The agent runs as the same Unix user as the factory and pi-relay, with the user's pi
extensions enabled (including web access), so it **can read secrets on disk** (the factory's
`secrets.env`, pi's login, pi-relay's files). What limits the damage: the factory never
merges and never pushes to a base branch; branch protection needs your approval; the bot
token only reaches the repositories it was invited to; secrets are never put in child
environments, command lines, prompts or worktree files; design sessions get only read-only
tools unless you add more to `design.tools`. A dedicated Unix user or an LXD sandbox is the
planned hardening step.
