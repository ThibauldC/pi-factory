# Operations guide

How to set up GitHub, install pi-factory on the VM, update it, roll it back and rotate its
token. The configuration reference is [configuration.md](configuration.md); the pi protocol
notes are in [pi-compat.md](pi-compat.md).

## 1. GitHub setup

1. Create a **machine account** (for example `yourname-factory-bot`). It is the factory's
   only GitHub identity: it comments, adds labels and assignees on issues, pushes branches
   and authors the pull requests (so you can approve them under branch protection).
2. Invite it as a **collaborator with write access** to each factory repository, and accept
   the invitation. Its reach is limited to the repositories it was invited to. Make sure
   **Issues** are enabled on each repository.
3. Create a **classic personal access token** for it with the `repo` scope. This goes into
   `secrets.env` as `PI_FACTORY_GITHUB_TOKEN`; the factory needs no other credentials.
4. Set `github.bot_name` / `github.bot_email` in `factory.toml` to the bot's name and its
   `<id>+<login>@users.noreply.github.com` address so commits are attributed to it, and
   `github.owner` to your own login (you are assigned issues in `factory:design-review` and
   `factory:blocked`).

### Labels (in every factory repository)

The factory never creates labels; `pi-factory check` fails if a `factory:*` label is
missing. Create them on each repository, for example with the GitHub CLI (run it as
yourself, not as the bot):

```bash
repo=acme/pi-relay
for l in ready-for-design designing design-review ready-for-build building in-review blocked; do
  gh label create "factory:$l" --repo "$repo" --force
done
# optional: issues without a priority label are queued last
for l in urgent high medium low; do
  gh label create "priority:$l" --repo "$repo" --force
done
```

`factory:ready-for-design` and `factory:ready-for-build` are the labels you apply; everything
else is set by the factory. Applying a label needs triage rights, so GitHub decides who can
start work. Done and Canceled are not labels: a closed issue is finished (*completed* is
Done, *not planned* is Canceled).

### Branch protection and closing

On each repository enable branch protection on the default branch requiring **one approval**
(the bot cannot approve its own PR; you do). Also turn on the setting
**Automatically delete head branches**, because the factory only deletes its local branch.

Pull requests contain `Closes #N`, which GitHub only honours when the PR merges into the
repository's **default branch**. So `base_branch` in `factory.toml` must be the default
branch; `pi-factory check` warns if it is not. The factory never merges and never closes
issues itself.

## 2. Install on the VM

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
version), per repository the bot's push access and that issues are enabled, that every
`factory:*` label exists and the owner can be assigned (missing `priority:*` labels only
warn), the default-branch warning described above, and that the state, worktree and clone
directories are writable. `pi-factory run` performs the same checks first and exits
non-zero if any fail.

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

Create a throwaway issue in a scratch repository, label it `factory:ready-for-design`, and
follow it through `factory:design-review` → `factory:ready-for-build` → `factory:in-review`
→ closed (merge the PR). Only then point the factory at real work. The idle factory process
should use at most about 60 MB RSS:

```bash
ps -o rss,cmd -p $(systemctl show -p MainPID --value pi-factory)
```

On a public repository, read an issue before you label it: the issue body is not filtered
(comments from people who are not the owner, a member or a collaborator are).

## 3. Day-to-day

| You do | The factory does |
|---|---|
| Label an issue `factory:ready-for-design` | Designs it read-only, posts the design as a comment, swaps the label to `factory:design-review` and assigns you. |
| Answer the open questions in a comment, then replace the label with `factory:ready-for-build` | Implements, runs the repo checks (up to 2 repair rounds), pushes the branch `factory/<number>-<slug>`, opens the PR (`#N: title`, body with `Closes #N`), swaps to `factory:in-review`. |
| Comment on the design and replace the label with `factory:ready-for-design` | Runs a new design round that includes the previous design and your comments. |
| Comment on the issue (not the PR review) and replace the label with `factory:ready-for-build` | Continues on the same branch and updates the same PR. |
| Merge the PR | GitHub closes the issue through `Closes #N`; the factory removes the worktree and archives the run. |
| Remove or replace `factory:designing` / `factory:building`, or close the issue | Stops the run within one poll; posts nothing. |

An issue in `factory:blocked` has a comment saying why and how to retry: fix the cause, then
replace the label with `factory:ready-for-design` or `factory:ready-for-build`. Comment text
is never treated as a command; only the state labels and whether the issue is open drive the
factory. Only comments from the repository owner, members and collaborators are passed to
the agent. The factory reads issue comments only, not pull request review comments.

If you give an issue two `factory:*` labels, it is ignored (and listed on the dashboard) until
you remove one.

Where to look: `journalctl -u pi-factory`, the dashboard
(`https://<host>.<tailnet>.ts.net:<port>`), and `~/factory/state/runs/<repo key>-<number>/`
(run record, pi sessions, command outputs, posted texts). Finished runs move to
`~/factory/state/archive/` and are deleted after the retention period (default 30 days);
blocked and active runs are never pruned.

## 4. Updating

```bash
cd ~/git/pi-factory && git pull && uv sync && sudo systemctl restart pi-factory
```

Restarting is safe at any time. On stop the factory closes pi's stdin and leaves each run at
its current step; on start it resumes each interrupted pi session once, finishes an
interrupted label swap and re-runs interrupted factory steps (setup, checks, commit, push,
PR). A phase that is interrupted a second time goes to `factory:blocked`.

## 5. Rollback

```bash
sudo systemctl disable --now pi-factory
```

Issues keep their `factory:*` labels, and worktrees and runs stay on disk for inspection.
Nothing else on the VM depends on the factory. To remove the tailnet port:
`sudo tailscale serve --https=<port> off`.

## 6. Token rotation

Create a new classic token for the machine account, replace `PI_FACTORY_GITHUB_TOKEN` in
`~/.config/pi-factory/secrets.env` (keep mode 0600), run `sudo systemctl restart
pi-factory`, then delete the old token on GitHub.

## 7. Rate limits

The factory sends at most `repositories × 4 + tracked issues` requests per poll, far below
GitHub's 5000 requests per hour at the default 2-minute interval. If GitHub does answer with
a rate-limit response, the poll counts as failed, the reset time is logged (never the
token) and the next poll waits with the usual backoff (up to 15 minutes). Running tasks keep
going.

## 8. Security notes (accepted for v1)

The agent runs as the same Unix user as the factory and pi-relay, with the user's pi
extensions enabled (including web access), so it **can read secrets on disk** (the factory's
`secrets.env`, pi's login, pi-relay's files). What limits the damage: the factory never
merges and never pushes to a base branch; branch protection needs your approval; the bot
token only reaches the repositories it was invited to; secrets are never put in child
environments, command lines, prompts or worktree files; design sessions get only read-only
tools unless you add more to `design.tools`; and only trusted comment authors reach the
agent. A dedicated Unix user or an LXD sandbox is the planned hardening step.
